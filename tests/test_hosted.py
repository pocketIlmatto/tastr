"""Hosted mode (tastr/web/hosted.py): Access logins, turned-off features, upload limits, security headers."""

import os
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from tastr import db, stems
from tastr.web import hosted
from tastr.web.hosted import AccessVerifier, AnalysisSlots, ConfigError, Settings
from tastr.web.server import STATIC_DIR, create_app
from test_foundation import full_card

TEAM, AUD = "friends.cloudflareaccess.com", "aud-tag"
GOOD = {"cf-access-jwt-assertion": "good"}


def stub_verifier(token):
    if token != "good":
        raise ValueError("bad token")
    return "friend@example.com"


def make_client(db_path, **kw):
    settings = Settings(hosted=True, access_team_domain=TEAM, access_aud=AUD, **kw)
    return TestClient(create_app(db_path, settings, verifier=stub_verifier))


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("TASTR_HOME", str(h))
    return h


@pytest.fixture
def client(db_path, home):
    return make_client(db_path)


def test_hosted_without_access_settings_refuses_to_start(monkeypatch):
    monkeypatch.setenv("TASTR_HOSTED", "1")
    monkeypatch.delenv("TASTR_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("TASTR_ACCESS_AUD", raising=False)
    with pytest.raises(ConfigError):
        Settings.from_env()
    monkeypatch.setenv("TASTR_ACCESS_TEAM_DOMAIN", f"https://{TEAM}/")
    monkeypatch.setenv("TASTR_ACCESS_AUD", AUD)
    monkeypatch.setenv("TASTR_DAILY_TRACKS", "5")
    s = Settings.from_env()
    assert s.hosted and s.access_team_domain == TEAM and s.daily_tracks == 5


def test_local_mode_stays_open_and_unchanged(db_path, home, monkeypatch):
    monkeypatch.delenv("TASTR_HOSTED", raising=False)
    monkeypatch.delenv("TASTR_BULK_AT_ONCE", raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 8)
    c = TestClient(create_app(db_path))
    assert c.get("/api/tracks").status_code == 200
    assert c.get("/api/config").json() == {"hosted": False, "links": True, "query": True, "query_scope": "all", "user": None, "admin": False,
                                          "limits": None, "browser_engine": None, "bulk_at_once": 4, "query_browser": None,
                                          "direct_upload": False}
    assert "content-security-policy" not in c.get("/").headers



@pytest.mark.parametrize("cores, lanes", [(None, 1), (1, 1), (2, 1), (6, 3), (32, 4)])
def test_bulk_lanes_at_home_follow_the_cores(monkeypatch, cores, lanes):
    monkeypatch.setattr("os.cpu_count", lambda: cores)
    assert Settings().bulk_lanes() == lanes
    assert Settings(bulk_at_once=6).bulk_lanes() == 6  # TASTR_BULK_AT_ONCE wins


def test_bulk_lanes_hosted_follow_max_analyses(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 32)
    assert Settings(hosted=True, max_analyses=3).bulk_lanes() == 3
    assert Settings(hosted=True, max_analyses=3, bulk_at_once=1).bulk_lanes() == 1
    monkeypatch.setenv("TASTR_BULK_AT_ONCE", "2")
    assert Settings.from_env().bulk_at_once == 2


@pytest.mark.parametrize("hosted_mode", [False, True])
def test_link_analysis_never_reads_server_paths(db_path, home, tmp_path, hosted_mode):
    (tmp_path / "music").mkdir()
    (tmp_path / "music" / "secret.wav").write_bytes(b"RIFF")
    c = make_client(db_path) if hosted_mode else TestClient(create_app(db_path, Settings()))
    for target in [str(tmp_path / "music"), str(tmp_path / "music" / "secret.wav"), "~", "file:///etc/passwd"]:
        r = c.post("/api/analyze-url", json={"url": target}, headers=GOOD)
        assert r.status_code == (403 if hosted_mode else 400), target
    assert c.get("/api/tracks", headers=GOOD).json() == []


def test_requests_without_a_valid_login_are_refused(client):
    for path in ["/", "/api/tracks", "/static/app/main.js", "/api/config", "/docs", "/openapi.json"]:
        r = client.get(path)
        assert r.status_code == 401, path
        assert client.get(path, headers={"cf-access-jwt-assertion": "forged"}).status_code == 401
    assert client.post("/api/analyze-url", json={"url": "https://x.test"}).status_code == 401
    assert client.get("/api/health").status_code == 200  # the host's health check has no login


def test_logged_in_requests_work_and_carry_security_headers(client):
    r = client.get("/", headers=GOOD)
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert r.headers["x-content-type-options"] == "nosniff"
    client.cookies.set("CF_Authorization", "good")  # the cookie Access sets in the browser works too
    assert client.get("/api/tracks").status_code == 200
    client.cookies.clear()
    cfg = client.get("/api/config", headers=GOOD).json()
    assert cfg["hosted"] and not cfg["links"] and cfg["query_scope"] == "mine" and cfg["user"] == "friend@example.com"
    assert cfg["bulk_at_once"] == cfg["limits"]["max_analyses"]
    assert client.get("/docs", headers=GOOD).status_code == 404


def test_csp_allows_the_pages_own_inline_script():
    hashes = hosted.inline_script_hashes((STATIC_DIR / "index.html").read_text())
    assert len(hashes) == 1 and hashes[0].startswith("'sha256-")


def test_hosted_searches_run_in_the_browser_not_on_the_server(db_path, home, tmp_path, monkeypatch):
    """TAS-49: no SQL endpoint on a hosted server; the page gets sql.js and a copy of the library instead."""
    save(db_path, "friend@example.com", sha="a1")
    folder = tmp_path / "sqljs"
    folder.mkdir()
    (folder / "sql-wasm.js").write_text("var initSqlJs;")
    (folder / "sql-wasm.wasm").write_bytes(b"\0asm")
    monkeypatch.setenv("TASTR_SQLJS_DIR", str(folder))
    c = make_client(db_path)
    r = c.post("/api/query", json={"sql": "SELECT id FROM tracks"}, headers=GOOD)
    assert r.status_code == 404 and "browser" in r.json()["detail"]
    cfg = c.get("/api/config", headers=GOOD).json()
    assert cfg["query_scope"] == "mine" and cfg["query_browser"]["base"] == "/sqljs/"
    wasm = c.get("/sqljs/sql-wasm.wasm", headers=GOOD)
    assert wasm.status_code == 200 and wasm.headers["content-type"] == "application/wasm" and "immutable" in wasm.headers["cache-control"]
    assert c.get("/sqljs/sql-wasm.js", headers=GOOD).status_code == 200
    assert c.get("/sqljs/LICENSE", headers=GOOD).status_code == 404  # only the two files
    assert c.get("/sqljs/sql-wasm.js").status_code in (401, 403)  # behind the login like the rest
    csp = c.get("/", headers=GOOD).headers["content-security-policy"]
    assert "'wasm-unsafe-eval'" in csp.split("script-src")[1].split(";")[0]  # workers may compile WebAssembly


def upload(client, *files):
    return client.post("/api/analyze", files=[("files", (n, data, "audio/wav")) for n, data in files], headers=GOOD)


def test_upload_type_and_count_limits(db_path, home):
    c = make_client(db_path, max_files=2)
    assert upload(c, ("notes.txt", b"hi")).status_code == 415
    assert upload(c, *[(f"{i}.wav", b"RIFF") for i in range(3)]).status_code == 400


def test_oversized_upload_leaves_nothing_on_disk(db_path, home):
    c = make_client(db_path, max_upload_mb=0.001)  # about 1 KB
    r = upload(c, ("small.wav", b"x" * 100), ("big.wav", b"x" * 5000))
    assert r.status_code == 413 and "big.wav" in r.json()["detail"]
    assert [p for p in (home / "uploads").rglob("*") if p.is_file()] == []


def _wav(seconds: float) -> bytes:
    import io

    import numpy as np
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, np.zeros(int(seconds * 8000), dtype=np.float32), 8000, format="WAV")
    return buf.getvalue()


def test_too_long_upload_is_refused_before_analysis(db_path, home):
    c = make_client(db_path, max_minutes=0.05)  # 3 seconds
    r = upload(c, ("short.wav", _wav(1)), ("mix.wav", _wav(6)))
    assert r.status_code == 413 and "mix.wav" in r.json()["detail"] and "minutes" in r.json()["detail"]
    assert [p for p in (home / "uploads").rglob("*") if p.is_file()] == []
    conn = db.connect(db_path)
    assert db.recent_events(conn)[0]["detail"] == "Hit the track length limit (0.05 min): mix.wav, 0 min"
    assert db.usage_today(conn, "friend@example.com", "tracks", hosted.today()) == 0


def test_length_unknown_is_left_to_the_analysis(tmp_path):
    p = tmp_path / "odd.mp3"
    p.write_bytes(b"not audio")
    assert hosted.audio_minutes(p) is None
    p = tmp_path / "ok.wav"
    p.write_bytes(_wav(3))
    assert hosted.audio_minutes(p) == pytest.approx(0.05)


def test_out_of_memory_says_why():
    from tastr.web.server import _failure_reason, _hosted_error

    s = Settings(max_minutes=12)
    for err in ("FunctionTimeoutError: Task's current input hit its timeout of 600s",
                "RemoteError: Task exited with OOM (out of memory)"):
        assert "ran out of" in _hosted_error(err, s) and "12 minutes" in _hosted_error(err, s)
        assert "ran out of" in _failure_reason(err)
    assert _hosted_error("DecodeError: /data/uploads/x", s) == "Couldn't analyze this file."


def test_daily_track_limit_per_person(db_path, home):
    c = make_client(db_path, daily_tracks=3)
    conn = db.connect(db_path)
    db.add_usage(conn, "friend@example.com", "tracks", hosted.today(), 3)
    db.add_usage(conn, "other@example.com", "tracks", hosted.today(), 1)
    assert db.usage_today(conn, "friend@example.com", "tracks", hosted.today()) == 3
    conn.close()
    r = upload(c, ("a.wav", b"RIFF"))
    assert r.status_code == 429 and "0 left" in r.json()["detail"]


def test_failed_analyses_dont_count_toward_daily_limit(db_path, home):
    c = make_client(db_path, daily_tracks=1)
    for _ in range(3):  # e.g. the analysis backend was misconfigured; each retry is still allowed
        assert upload(c, ("broken.wav", b"not audio at all")).status_code == 200
    conn = db.connect(db_path)
    assert db.usage_today(conn, "friend@example.com", "tracks", hosted.today()) == 0
    conn.close()


def test_storage_cap(db_path, home):
    (home / "uploads" / "old").mkdir(parents=True)
    (home / "uploads" / "old" / "a.wav").write_bytes(b"x" * 2048)
    c = make_client(db_path, storage_gb=1 / 1024 ** 2)  # 1 KB
    assert upload(c, ("a.wav", b"RIFF")).status_code == 507


def test_analysis_errors_dont_leak_paths(db_path, home):
    c = make_client(db_path)
    r = upload(c, ("broken.wav", b"not audio at all"))
    assert r.status_code == 200
    assert r.json()["results"][0]["error"] == "Couldn't analyze this file."


def test_daily_stem_limit(db_path, home, tmp_path, monkeypatch):
    audio = tmp_path / "x.wav"
    audio.write_bytes(b"RIFF")
    card = full_card()
    card.source.local_path = str(audio)
    conn = db.connect(db_path)
    tid = db.save_card(conn, card, "friend@example.com")
    conn.close()
    started = []
    monkeypatch.setattr(stems, "available", lambda: True)
    monkeypatch.setattr(stems.Jobs, "start", lambda self, path, folder: started.append(folder))
    c = make_client(db_path, daily_stems=1)
    assert c.post(f"/api/tracks/{tid}/stems", headers=GOOD).status_code == 202
    r = c.post(f"/api/tracks/{tid}/stems", headers=GOOD)
    assert r.status_code == 429 and len(started) == 1


def test_analysis_slots_queue_then_give_up():
    slots = AnalysisSlots(1, timeout=0.05)
    with slots:
        assert slots.status() == {"running": 1, "waiting": 0, "max": 1}
        with pytest.raises(TimeoutError):
            with slots:
                pass
    assert slots.status() == {"running": 0, "waiting": 0, "max": 1}
    with slots:  # released again
        pass


def test_access_verifier_checks_signature_audience_and_expiry():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    v = AccessVerifier(TEAM, AUD)
    v.keys = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key()))
    now = int(time.time())
    claims = {"email": "Friend@Example.com", "aud": [AUD], "iss": f"https://{TEAM}", "iat": now, "exp": now + 600}
    assert v(jwt.encode(claims, key, algorithm="RS256")) == "friend@example.com"
    for bad in [{"aud": ["other"]}, {"iss": "https://evil.cloudflareaccess.com"}, {"exp": now - 10}, {"email": None}]:
        with pytest.raises((jwt.PyJWTError, ValueError)):
            v(jwt.encode({**claims, **bad}, key, algorithm="RS256"))
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises((jwt.PyJWTError, ValueError)):
        v(jwt.encode(claims, other, algorithm="RS256"))


def save(db_path, owner, sha="abc", path="/x.wav", uri=None):
    card = full_card()
    card.source.sha256, card.source.local_path = sha, path
    card.source.uri = uri or card.source.uri
    conn = db.connect(db_path)
    tid = db.save_card(conn, card, owner)
    conn.close()
    return tid


def test_each_person_sees_only_their_own_tracks(db_path, home):
    mine = save(db_path, "friend@example.com")
    theirs = save(db_path, "other@example.com")  # the same file in another library is its own track
    local = save(db_path, None)
    assert len({mine, theirs, local}) == 3
    c = make_client(db_path)
    assert [t["id"] for t in c.get("/api/tracks", headers=GOOD).json()] == [mine]
    assert c.get(f"/api/tracks/{mine}", headers=GOOD).status_code == 200
    for tid in (theirs, local, 9999):
        for method, path in [("get", ""), ("get", "/audio"), ("get", "/edits"), ("put", "/edits"),
                             ("get", "/stems/drums"), ("post", "/reanalyze"), ("delete", "")]:
            kw = {"json": {}} if method == "put" else {}
            r = getattr(c, method)(f"/api/tracks/{tid}{path}", headers=GOOD, **kw)
            assert r.status_code == 404, (tid, method, path)
    conn = db.connect(db_path)
    assert db.track_owner(conn, theirs) == (True, "other@example.com")
    conn.close()


@pytest.mark.rust
def test_uploads_go_to_the_uploaders_library(db_path, home, synth_wav):
    c = make_client(db_path)
    path, _ = synth_wav
    with open(path, "rb") as f:
        r = c.post("/api/analyze", files=[("files", (path.name, f, "audio/wav"))], headers=GOOD)
    tid = r.json()["results"][0]["track_id"]
    conn = db.connect(db_path)
    assert db.track_owner(conn, tid) == (True, "friend@example.com")
    conn.close()
    upload = next(p for p in (home / "uploads").rglob("*") if p.is_file())
    assert c.delete(f"/api/tracks/{tid}", headers=GOOD).status_code == 204
    assert not upload.exists() and not upload.parent.exists()  # hosted deletes the uploaded audio too


def test_deleting_keeps_stems_another_library_shares(db_path, home, tmp_path):
    mine, theirs = save(db_path, "friend@example.com"), save(db_path, "other@example.com")
    folder = stems.stems_dir("abc", mine)
    folder.mkdir(parents=True)
    (folder / "drums.flac").write_bytes(b"x")
    c = make_client(db_path)
    assert c.delete(f"/api/tracks/{mine}", headers=GOOD).status_code == 204
    assert folder.is_dir()
    conn = db.connect(db_path)
    db.delete_track(conn, theirs)
    conn.close()


def test_admin_usage(db_path, home, tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x" * 1000)
    save(db_path, "friend@example.com", path=str(audio))
    save(db_path, None, sha="def")
    conn = db.connect(db_path)
    db.add_usage(conn, "friend@example.com", "tracks", hosted.today(), 2)
    conn.close()
    plain = make_client(db_path)
    assert plain.get("/api/admin/usage", headers=GOOD).status_code == 404
    assert plain.get("/api/config", headers=GOOD).json()["admin"] is False
    admin = make_client(db_path, admins=("friend@example.com", "pam@example.com"))
    assert admin.get("/api/config", headers=GOOD).json()["admin"] is True
    body = admin.get("/api/admin/usage", headers=GOOD).json()
    by = {u["email"]: u for u in body["users"]}
    assert set(by) == {"friend@example.com", "pam@example.com", None}
    f = by["friend@example.com"]
    assert f["tracks"] == 1 and f["audio_bytes"] == 1000 and f["last_seen"]
    assert f["today"] == {"tracks": 2, "stems": 0, "drums": 0, "visits": 2, "asks": 0}  # two /api/config page loads above
    assert by[None]["tracks"] == 1 and by["pam@example.com"]["tracks"] == 0
    assert body["limits"]["daily_tracks"] == 30 and body["storage"]["cap_bytes"] == 10 * 1024 ** 3
    assert body["limits"]["storage_gb"] == 10 and body["limits"]["max_analyses"] == 2
    assert body["analyses"] == {"running": 0, "waiting": 0, "max": 2}
    assert {h["kind"] for h in body["history"]} == {"tracks", "visits"}
    local = TestClient(create_app(db_path, Settings()))
    assert local.get("/api/admin/usage").status_code == 404
    assert body["recent"] == [] and body["blocked_today"] == 0  # friend had usage before their first page load here


@pytest.mark.rust
def test_admin_activity_log_and_blocked_requests(db_path, home):
    c = make_client(db_path, admins=("friend@example.com",), daily_tracks=1)
    c.get("/api/config", headers=GOOD)
    c.get("/api/config", headers=GOOD)  # only the first visit counts as a first sign-in
    assert upload(c, ("broken.wav", b"not audio at all")).status_code == 200
    assert upload(c, ("a.wav", b"RIFF"), ("b.wav", b"RIFF")).status_code == 429
    for _ in range(3):
        assert c.get("/api/tracks").status_code == 401
    assert c.get("/api/tracks", headers={"cf-access-jwt-assertion": "forged"}).status_code == 401
    body = c.get("/api/admin/usage", headers=GOOD).json()
    assert body["blocked_today"] == 4
    log = [(e["kind"], e["ok"], e["detail"]) for e in body["recent"]]  # newest first
    assert log == [("limit", False, "Hit the daily track limit (1)"),
                   ("failed", False, "Analysis failed: broken.wav, it couldn't be decoded as audio"),
                   ("upload", True, "Uploaded broken.wav"),
                   ("signin", True, "Signed in for the first time")]
    assert [e["kind"] for e in body["problems"]] == ["limit", "failed"]
    assert body["log_links"]["modal"].startswith("https://modal.com/")
    assert all(e["user"] == "friend@example.com" and e["at"] for e in body["recent"])
    assert str(home) not in str(body["recent"])  # no server paths


def test_local_mode_keeps_no_activity_log(db_path, home):
    c = TestClient(create_app(db_path, Settings()))
    c.get("/api/config")
    c.post("/api/analyze", files=[("files", ("broken.wav", b"nope", "audio/wav"))])
    assert db.recent_events(db.connect(db_path)) == []


def test_old_events_are_pruned(db_path):
    conn = db.connect(db_path)
    conn.execute("INSERT INTO events (at, user, kind, detail) VALUES ('2000-01-01T00:00:00+00:00', 'a', 'upload', 'old')")
    db.add_event(conn, "b", "upload", "new")
    assert [e["detail"] for e in db.recent_events(conn)] == ["new"]


def test_old_databases_get_the_owner_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = db.connect(path)
    tid = db.save_card(conn, full_card())
    conn.close()
    # Turn it back into a database from before owners: no owner column, sha256 UNIQUE on its own.
    old = sqlite3.connect(path)
    cols = [r[1] for r in old.execute("PRAGMA table_info(tracks)") if r[1] != "owner"]
    ddl = old.execute("SELECT sql FROM sqlite_master WHERE name = 'tracks'").fetchone()[0]
    ddl = (ddl.replace("CREATE TABLE tracks", "CREATE TABLE tracks_old").replace("sha256          TEXT,", "sha256 TEXT UNIQUE,")
           .split("    owner ")[0].rstrip().rstrip(",") + "\n)")
    assert "sha256 TEXT UNIQUE" in ddl
    old.executescript(f"""PRAGMA foreign_keys = OFF; DROP INDEX idx_tracks_owner; {ddl};
        INSERT INTO tracks_old SELECT {', '.join(cols)} FROM tracks; DROP TABLE tracks;
        ALTER TABLE tracks_old RENAME TO tracks;""")
    assert "owner" not in [r[1] for r in old.execute("PRAGMA table_info(tracks)")]
    old.close()

    conn = db.connect(path)
    assert db.track_owner(conn, tid) == (True, None)
    assert db.load_card(conn, tid).core.bpm == 124.0  # sections, cards and the rest still attached
    assert db.save_card(conn, full_card(), "friend@example.com") != tid  # same file, another library
    assert db.set_owner(conn, "pam@example.com") == 1
    assert db.track_owner(conn, tid) == (True, "pam@example.com")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_link_signer_tokens_are_per_track_and_expire():
    s = hosted.LinkSigner("secret", days=7)
    t = s.token(5, now=1000)
    assert s.check(t, 5, now=1000 + 7 * 86400 - 1)
    assert not s.check(t, 5, now=1000 + 7 * 86400 + 1)  # expired
    assert not s.check(t, 6, now=1000)  # another track
    exp, sig = t.split(".")
    assert not s.check(f"{int(exp) + 86400}.{sig}", 5, now=1000)  # extended
    assert not hosted.LinkSigner("other").check(t, 5, now=1000)
    assert not s.check("junk", 5) and not s.check("", 5)


def test_strudel_links_work_without_the_login(db_path, home, tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"RIFF....")
    mine = save(db_path, "friend@example.com", path=str(audio))
    theirs = save(db_path, "other@example.com", path=str(audio))
    c = make_client(db_path, link_secret="s3cret")
    r = c.get(f"/api/tracks/{mine}/strudel-link", headers=GOOD).json()
    assert r["days"] == hosted.LINK_DAYS and r["audio"].startswith("/pub/") and r["audio"].endswith(f"/tracks/{mine}/audio")
    assert c.get(f"/api/tracks/{theirs}/strudel-link", headers=GOOD).status_code == 404  # not yours, no link
    got = c.get(r["audio"], headers={"origin": "https://strudel.cc"})  # no Access token, like strudel.cc
    assert got.status_code == 200 and got.content == b"RIFF...."
    assert got.headers["access-control-allow-origin"] == "https://strudel.cc"
    assert c.options(r["audio"], headers={"origin": "https://strudel.cc"}).status_code == 204
    stem = r["audio"].removesuffix("/audio") + "/stems/drums"
    assert c.get(stem).status_code == 404  # not separated yet
    folder = stems.stems_dir("abc", mine)
    folder.mkdir(parents=True)
    stems.stem_path(folder, "drums").write_bytes(b"fLaC")
    assert c.get(stem).content == b"fLaC"
    # The token is for one track: moving it to another, or tampering with it, finds nothing.
    assert c.get(r["audio"].replace(f"/tracks/{mine}/", f"/tracks/{theirs}/")).status_code == 404
    assert c.get(r["audio"].replace("/pub/", "/pub/1").replace(".", "x", 1)).status_code == 404
    # The same link from a server with another secret (or after a restart without one) has expired.
    assert make_client(db_path, link_secret="new").get(r["audio"]).status_code == 404


def test_strudel_links_on_your_own_machine(db_path, home):
    tid = save(db_path, None)
    c = TestClient(create_app(db_path, Settings()))
    assert c.get(f"/api/tracks/{tid}/strudel-link").json() == {"audio": f"/api/tracks/{tid}/audio", "days": None}
    assert c.get(f"/pub/1.x/tracks/{tid}/audio").status_code == 404


def test_old_uploaded_audio_expires_but_cards_stay(db_path, home):
    up = home / "uploads"

    def upload_file(name, age_days):
        p = up / name / "song.wav"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"x")
        t = time.time() - age_days * 86400
        os.utime(p, (t, t))
        return p

    old, fresh, admins, shared = (upload_file(n, d) for n, d in [("a", 40), ("b", 5), ("c", 40), ("d", 40)])
    outside = home.parent / "elsewhere.wav"  # not one of Tastr's uploads: never touched
    outside.write_bytes(b"x")
    os.utime(outside, (0, 0))
    ids = {
        "old": save(db_path, "friend@example.com", sha="s1", path=str(old), uri=str(old)),
        "fresh": save(db_path, "friend@example.com", sha="s2", path=str(fresh), uri=str(fresh)),
        "admin": save(db_path, "pam@example.com", sha="s3", path=str(admins), uri=str(admins)),
        "shared": save(db_path, "friend@example.com", sha="s3", path=str(shared), uri=str(shared)),
        "outside": save(db_path, "friend@example.com", sha="s4", path=str(outside), uri=str(outside)),
    }
    for sha, key in [("s1", "old"), ("s3", "admin")]:
        folder = stems.stems_dir(sha, ids[key])
        folder.mkdir(parents=True)
        (folder / "drums.flac").write_bytes(b"x")
    conn = db.connect(db_path)
    gone = hosted.expire_audio(conn, up, 30, keep=("pam@example.com",))
    assert sorted(gone) == sorted([ids["old"], ids["shared"]])
    assert not old.exists() and not old.parent.exists() and not shared.exists()
    assert fresh.exists() and admins.exists() and outside.exists()
    assert not stems.stems_dir("s1", ids["old"]).exists()
    assert stems.stems_dir("s3", ids["admin"]).exists()  # the admin's copy of the same file keeps its stems
    assert db.load_card(conn, ids["old"]) is not None  # the card stays
    conn.close()
    c = make_client(db_path)
    assert c.get(f"/api/tracks/{ids['old']}", headers=GOOD).json()["has_audio"] is False


def test_saved_searches_are_each_persons_own(db_path, home):
    def verifier(token):
        return {"good": "friend@example.com", "other": "other@example.com"}[token]
    c = TestClient(create_app(db_path, Settings(hosted=True, access_team_domain=TEAM, access_aud=AUD), verifier=verifier))
    OTHER = {"cf-access-jwt-assertion": "other"}
    mine = c.put("/api/searches", json={"mode": "sql", "name": "Mine", "data": {"sql": "SELECT 1"}}, headers=GOOD).json()
    c.put("/api/searches", json={"mode": "sql", "name": "Theirs", "data": {"sql": "SELECT 2"}}, headers=OTHER)
    assert [s["name"] for s in c.get("/api/searches", headers=GOOD).json()] == ["Mine"]
    assert [s["name"] for s in c.get("/api/searches", headers=OTHER).json()] == ["Theirs"]
    assert c.delete(f"/api/searches/{mine['id']}", headers=OTHER).status_code == 404  # can't delete someone else's
    assert c.get("/api/searches").status_code == 401


def test_lists_are_each_persons_own(db_path, home):
    def verifier(token):
        return {"good": "friend@example.com", "other": "other@example.com"}[token]
    c = TestClient(create_app(db_path, Settings(hosted=True, access_team_domain=TEAM, access_aud=AUD), verifier=verifier))
    OTHER = {"cf-access-jwt-assertion": "other"}
    mine, theirs = save(db_path, "friend@example.com", sha="a1"), save(db_path, "other@example.com", sha="b2")
    lst = c.post("/api/lists", json={"name": "Mine", "track_ids": [mine, theirs]}, headers=GOOD).json()
    assert lst["track_ids"] == [mine]  # someone else's track can't go in your list
    c.post("/api/lists", json={"name": "Mine", "track_ids": [theirs]}, headers=OTHER)  # same name, separate list
    assert [x["track_ids"] for x in c.get("/api/lists", headers=GOOD).json()] == [[mine]]
    assert [x["track_ids"] for x in c.get("/api/lists", headers=OTHER).json()] == [[theirs]]
    assert c.delete(f"/api/lists/{lst['id']}", headers=OTHER).status_code == 404
    assert c.put(f"/api/lists/{lst['id']}", json={"name": "x"}, headers=OTHER).status_code == 404
    assert c.post(f"/api/lists/{lst['id']}/tracks", json={"track_ids": [theirs]}, headers=OTHER).status_code == 404
    assert list(c.get("/api/vibe-scores", headers=GOOD).json()) == [str(mine)]
    assert c.get("/api/lists").status_code == 401


def test_background_jobs_are_each_persons_own_and_count_only_successes(db_path, home):
    from test_web import wait_job

    def verifier(token):
        return {"good": "friend@example.com", "other": "other@example.com"}[token]
    c = TestClient(create_app(db_path, Settings(hosted=True, access_team_domain=TEAM, access_aud=AUD, daily_tracks=1),
                              verifier=verifier))
    r = c.post("/api/analyze?background=1", files=[("files", ("broken.wav", b"not audio at all", "audio/wav"))], headers=GOOD)
    assert r.status_code == 202
    job = wait_job(c, r.json()["id"], headers=GOOD)
    assert job["state"] == "done" and job["results"][0]["error"] == "Couldn't analyze this file."
    assert c.get(f"/api/analyze/jobs/{job['id']}", headers={"cf-access-jwt-assertion": "other"}).status_code == 404
    conn = db.connect(db_path)
    assert db.usage_today(conn, "friend@example.com", "tracks", hosted.today()) == 0  # a failure doesn't count
    tag = f"(ref {job['id'][:8]})"  # the same ref in the upload and failure lines, and in the server log
    upload_line, failed_line = [e["detail"] for e in reversed(db.recent_events(conn)) if e["kind"] != "signin"]
    assert upload_line == f"Uploaded broken.wav {tag}"
    assert failed_line.startswith("Analysis failed: broken.wav at step ") and failed_line.endswith(tag)
    conn.close()


def test_restart_logs_cut_off_analyses_and_deploys(db_path, home):
    import json

    folder = home / "analysis-jobs"
    folder.mkdir(parents=True)
    (folder / "abc123ff00.json").write_text(json.dumps({"id": "abc123ff00", "owner": "friend@example.com", "state": "running",
                                                        "stage": "sections", "files": ["mix.mp3"]}))
    s = dict(hosted=True, access_team_domain=TEAM, access_aud=AUD)
    with TestClient(create_app(db_path, Settings(**s, fly_image="img:1"), verifier=stub_verifier)):
        pass
    conn = db.connect(db_path)
    log = [(e["kind"], e["ok"], e["detail"], e["user"]) for e in db.recent_events(conn)]
    assert log == [("failed", False, "Cut off by the restart: mix.mp3, while at step sections (ref abc123ff)", "friend@example.com"),
                   ("restart", False, "The server restarted in the middle of an analysis (it crashed or its machine was stopped)", None)]
    for image in ("img:1", "img:2"):  # a wake-up with the same version logs nothing; a new one logs the deploy
        with TestClient(create_app(db_path, Settings(**s, fly_image=image), verifier=stub_verifier)):
            pass
    assert [e["detail"] for e in db.recent_events(conn)][0] == "Deployed a new version (the server restarted)"
    assert len(db.recent_events(conn)) == 3
    run = "https://github.com/o/r/actions/runs/42"
    with TestClient(create_app(db_path, Settings(**s, fly_image="img:3", commit="49eeecf0123", commit_subject="Rust engine P0",
                                                 deploy_run=run), verifier=stub_verifier)):
        pass
    e = db.recent_events(conn)[0]
    assert (e["kind"], e["detail"], e["link"]) == ("deploy", "Deployed 49eeecf: Rust engine P0 (the server restarted)", run)


def test_events_table_gains_link_column(tmp_path):
    import sqlite3

    p = tmp_path / "old.db"
    old = sqlite3.connect(p)
    old.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, user TEXT, kind TEXT NOT NULL, "
                "ok INTEGER NOT NULL DEFAULT 1, detail TEXT)")
    old.execute("INSERT INTO events (at, kind, detail) VALUES ('2099-01-01T00:00:00', 'upload', 'before')")
    old.commit()
    old.close()
    conn = db.connect(p)
    db.add_event(conn, None, "deploy", "after", link="https://github.com/x")
    assert {e["detail"]: e["link"] for e in db.recent_events(conn)} == {"before": None, "after": "https://github.com/x"}


def test_log_links():
    s = Settings(fly_app="tastr-x", modal_env="friends", modal_workspace="me")
    assert s.log_links("tastr-web") == {"fly": "https://fly.io/apps/tastr-x/monitoring",
                                        "modal": "https://modal.com/apps/me/friends/deployed/tastr-web"}
    assert Settings().log_links("tastr-web") == {"modal": "https://modal.com/apps"}


def test_admin_routes_are_admins_only(db_path, home):
    plain = make_client(db_path)
    for method, path in [("get", "/api/admin/usage?range=1"), ("get", "/api/admin/events"),
                         ("put", "/api/admin/limits/a@example.com"), ("delete", "/api/admin/limits/a@example.com")]:
        kw = {"json": {"daily_tracks": 5}} if method == "put" else {}
        assert getattr(plain, method)(path, headers=GOOD, **kw).status_code == 404, path
        assert getattr(plain, method)(path, **kw).status_code == 401, path
    assert "cf_links" not in plain.get("/api/config", headers=GOOD).json()
    local = TestClient(create_app(db_path, Settings()))
    assert local.get("/api/admin/events").status_code == 404
    assert local.put("/api/admin/limits/a@example.com", json={}).status_code == 404


def test_limit_overrides_end_at_utc_midnight(db_path):
    conn = db.connect(db_path)
    db.set_override(conn, "a@example.com", 45, None, "2026-10-31", "pam@example.com")
    db.set_override(conn, "b@example.com", None, 2, None, "pam@example.com")
    assert db.daily_cap(conn, "a@example.com", "tracks", "2026-10-31", 30) == 45
    assert db.daily_cap(conn, "a@example.com", "stems", "2026-10-31", 10) == 10  # not changed: the default
    assert db.daily_cap(conn, "a@example.com", "tracks", "2026-11-01", 30) == 30  # today only: gone the next UTC day
    assert db.daily_cap(conn, "b@example.com", "stems", "2030-01-01", 10) == 2  # until changed
    assert set(db.overrides(conn, "2026-11-01")) == {"b@example.com"}
    db.set_override(conn, "b@example.com", None, None, None, "pam@example.com")  # back to the defaults
    assert db.get_override(conn, "b@example.com", "2026-11-01") is None


def test_upload_and_stem_limits_use_the_override(db_path, home, tmp_path, monkeypatch):
    admin = make_client(db_path, admins=("friend@example.com",), daily_tracks=1, daily_stems=0)
    conn = db.connect(db_path)
    db.add_usage(conn, "friend@example.com", "tracks", hosted.today())  # at the default cap already
    conn.close()
    assert upload(admin, ("a.wav", b"RIFF")).status_code == 429
    assert admin.put("/api/admin/limits/Friend@Example.com", json={"daily_tracks": 3, "daily_stems": 1, "scope": "today"},
                     headers=GOOD).json()["override"]["until_day"] == hosted.today()
    assert admin.get("/api/config", headers=GOOD).json()["limits"]["daily_tracks"] == 3
    assert upload(admin, ("a.wav", b"RIFF"), ("b.wav", b"RIFF")).status_code == 200  # 3 of 3, over the default of 1
    body = admin.get("/api/admin/usage", headers=GOOD).json()
    me = next(u for u in body["users"] if u["email"] == "friend@example.com")
    assert me["caps"] == {"tracks": 3, "stems": 1, "asks": 20} and me["override"]["set_by"] == "friend@example.com"
    log = [e["detail"] for e in body["recent"] if e["kind"] == "admin"]
    assert all(e["type"] == "admin" and e["source"] == "app" for e in body["recent"] if e["kind"] == "admin")
    assert log == ["Set friend's daily limits to 3 tracks, 1 stem jobs, 20 Claude asks (today only)"]
    assert admin.put("/api/admin/limits/friend@example.com", json={"daily_tracks": -1}, headers=GOOD).status_code == 422
    assert admin.put("/api/admin/limits/not-an-email", json={"daily_tracks": 1}, headers=GOOD).status_code == 400
    assert admin.delete("/api/admin/limits/friend@example.com", headers=GOOD).json()["override"] is None
    assert upload(admin, ("c.wav", b"RIFF")).status_code == 429  # back on the default
    # stems: the override lets one stem job through where the default allows none
    track = save(db_path, "friend@example.com", path=str(tmp_path / "x.wav"))
    (tmp_path / "x.wav").write_bytes(b"RIFF")
    monkeypatch.setattr(stems, "available", lambda: True)
    monkeypatch.setattr(stems.Jobs, "start", lambda self, path, folder: None)
    assert admin.post(f"/api/tracks/{track}/stems", headers=GOOD).status_code == 429
    admin.put("/api/admin/limits/friend@example.com", json={"daily_stems": 1, "scope": "standing"}, headers=GOOD)
    assert admin.post(f"/api/tracks/{track}/stems", headers=GOOD).status_code == 202


def test_admin_events_filters(db_path, home):
    c = make_client(db_path, admins=("friend@example.com",), daily_tracks=1)
    conn = db.connect(db_path)
    db.add_event(conn, None, "deploy", "Deployed abc", source="flyio")
    db.add_event(conn, "sam@example.com", "stems", "Started separating stems for 1 track", source="modal")
    db.add_blocked(conn, hosted.today())
    db.add_blocked(conn, hosted.today())
    conn.close()
    c.get("/api/config", headers=GOOD)  # first sign-in
    assert upload(c, ("a.wav", b"RIFF"), ("b.wav", b"RIFF")).status_code == 429

    def get(**q):
        r = c.get("/api/admin/events", params=q, headers=GOOD)
        assert r.status_code == 200, r.text
        return r.json()
    every = get()
    assert every["total"] == every["matched"] == 5
    assert {e["type"] for e in every["events"]} == {"deploy", "stems", "blocked", "signin", "limit"}
    assert every["people"] == ["friend@example.com", "sam@example.com"]
    assert [e["detail"] for e in get(type="blocked")["events"]] == ["2 requests blocked without a valid Access token"]
    assert [e["kind"] for e in get(source="modal")["events"]] == ["stems"]
    assert sorted(e["kind"] for e in get(who="-")["events"]) == ["blocked", "deploy"]
    both = get(who="friend@example.com", type="limit")
    assert both["matched"] == 1 and both["total"] == 5
    assert get(who="friend@example.com", source="cloudflare")["events"][0]["kind"] == "signin"
    assert get(**{"from": "2000-01-01", "to": "2000-01-02"})["matched"] == 0
    assert get(**{"from": hosted.today(), "to": hosted.today()})["matched"] == 5
    assert get(problems=True)["matched"] == 2  # the limit hit and the blocked requests
    for bad in ({"type": "nope"}, {"source": "aws"}, {"from": "yesterday"}):
        assert c.get("/api/admin/events", params=bad, headers=GOOD).status_code == 400


def test_old_events_get_type_and_source(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, user TEXT, kind TEXT NOT NULL, "
                "ok INTEGER NOT NULL DEFAULT 1, detail TEXT, link TEXT)")
    raw.execute("CREATE TABLE blocked_requests (day TEXT PRIMARY KEY, n INTEGER NOT NULL)")
    raw.execute("INSERT INTO events (at, kind, detail) VALUES ('2026-10-30T00:00:00+00:00', 'failed', 'x'), "
                "('2026-10-30T00:00:01+00:00', 'deploy', 'y')")
    raw.execute("INSERT INTO blocked_requests VALUES ('2026-10-30', 3)")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    assert [(e["type"], e["source"]) for e in db.recent_events(conn)] == [("deploy", "flyio"), ("error", "app")]
    db.add_blocked(conn, "2026-10-30")
    assert db.blocked_on(conn, "2026-10-30") == 4


def test_visits_count_after_half_an_hour_idle(db_path, home, monkeypatch):
    from tastr.web import server
    c = make_client(db_path, admins=("friend@example.com",))
    for _ in range(3):
        c.get("/api/config", headers=GOOD)
    conn = db.connect(db_path)
    assert db.usage_today(conn, "friend@example.com", "visits", hosted.today()) == 1
    monkeypatch.setattr(server, "VISIT_IDLE", -1)  # every request now comes "after a break"
    c.get("/api/tracks", headers=GOOD)
    assert db.usage_today(conn, "friend@example.com", "visits", hosted.today()) == 2
    assert [e["kind"] for e in db.recent_events(conn)] == ["signin"]  # only the very first visit is a sign-in


def test_admin_usage_ranges(db_path, home, tmp_path):
    from datetime import date, timedelta
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x" * 1000)
    save(db_path, "friend@example.com", path=str(audio))
    day = hosted.today()
    back = lambda n: (date.fromisoformat(day) - timedelta(days=n)).isoformat()  # noqa: E731
    conn = db.connect(db_path)
    db.add_usage(conn, "friend@example.com", "tracks", day, 2, hour=f"{day}T00")
    db.add_usage(conn, "friend@example.com", "tracks", back(3), 4)
    db.add_usage(conn, "sam@example.com", "stems", back(40), 1)
    db.snapshot_storage(conn, back(5), {"friend@example.com": 500, None: 9})
    conn.close()
    c = make_client(db_path, admins=("friend@example.com",))

    def series(r):
        body = c.get(f"/api/admin/usage?range={r}", headers=GOOD)
        assert body.status_code == 200, body.text
        return body.json()["series"]
    s1 = series("1")
    assert s1["hourly"] and len(s1["labels"]) == 24
    f = s1["people"]["friend@example.com"]
    assert f["tracks"][0] == 2 and f["visits"][s1["now_hour"]] == 1
    assert f["storage"][s1["now_hour"]] == 1000 and f["storage"][23] in (0, 1000)
    s7 = series("7")
    assert len(s7["labels"]) == 7 and s7["labels"][-1] == day
    f = s7["people"]["friend@example.com"]
    assert f["tracks"][-1] == 2 and f["tracks"][-4] == 4
    assert f["storage"][1] == 500 and f["storage"][-1] == 1000  # carried forward, then today's snapshot
    assert "sam@example.com" not in s7["people"]
    s30 = series("30")
    assert len(s30["labels"]) == 30 and s30["people"]["friend@example.com"]["storage"][0] == 0  # before the first snapshot
    sall = series("all")
    assert sall["labels"][0] == back(40) and sall["people"]["sam@example.com"]["stems"][0] == 1
    assert c.get("/api/admin/usage?range=2", headers=GOOD).status_code == 400


def test_cloudflare_links_for_admins_only(db_path, home):
    acct = "0123456789abcdef0123456789abcdef"
    admin = make_client(db_path, admins=("friend@example.com",), cf_account_id=acct)
    links = admin.get("/api/config", headers=GOOD).json()["cf_links"]
    assert links == {"apps": f"https://one.dash.cloudflare.com/{acct}/access/apps",
                     "access_logs": f"https://one.dash.cloudflare.com/{acct}/logs/access"}
    assert "cf_links" not in make_client(db_path, cf_account_id=acct).get("/api/config", headers=GOOD).json()
    assert make_client(db_path, admins=("friend@example.com",)).get("/api/config", headers=GOOD).json()["cf_links"] == {}
    assert Settings(cf_account_id="../evil").cf_links() == {}


def _run(i, conclusion="success", ago_h=1, name="tests", msg="Admin upgrades (#57)\n\nbody"):
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc) - timedelta(hours=ago_h)
    return {"id": i, "name": name, "conclusion": conclusion, "html_url": f"https://github.com/o/r/actions/runs/{i}",
            "run_started_at": (end - timedelta(minutes=3)).isoformat(), "updated_at": end.isoformat(),
            "head_commit": {"message": msg}}


def test_github_runs_become_log_lines(db_path, home, monkeypatch):
    monkeypatch.delenv("LITESTREAM_BUCKET", raising=False)
    c = make_client(db_path, admins=("friend@example.com",), github_repo="o/r", github_workflows=("tests", "deploy.yml"))
    runs = [_run(1, ago_h=48), _run(2), _run(3, "failure", name="deploy"), _run(5, name="readme-shots")]
    runs[2]["path"] = ".github/workflows/deploy.yml"
    runs[2]["name"] = "Deploy to Fly"  # matched by its file name
    c.app.state.watch_once(fetch_runs=lambda repo, token: runs)
    body = c.get("/api/admin/events?source=github", headers=GOOD).json()
    assert [(e["detail"], e["ok"], e["type"]) for e in body["events"]] == [
        ("Deploy to Fly failed on main (3 min): Admin upgrades (#57)", False, "deploy"),
        ("tests passed on main (3 min): Admin upgrades (#57)", True, "deploy")]  # run 1 is older than a day, readme-shots not listed
    assert body["events"][0]["link"] == "https://github.com/o/r/actions/runs/3"
    c.app.state.watch_once(fetch_runs=lambda repo, token: runs + [_run(6, ago_h=0)])  # only what's new
    assert c.get("/api/admin/events?source=github", headers=GOOD).json()["matched"] == 3

    def offline(repo, token):
        raise OSError("no network")
    c.app.state.watch_once(fetch_runs=offline)  # logged to the server log, not the activity log
    assert c.get("/api/admin/events?source=github", headers=GOOD).json()["matched"] == 3


def test_litestream_snapshots_become_log_lines(db_path, home, monkeypatch):
    from datetime import datetime, timedelta, timezone
    monkeypatch.setenv("LITESTREAM_BUCKET", "tastr-app")
    c = make_client(db_path, admins=("friend@example.com",))
    iso = lambda h: (datetime.now(timezone.utc) - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    snaps = [{"replica": "s3", "generation": "a295b16a796689f3", "index": "1", "size": "2097152", "created": iso(30)},
             {"replica": "s3", "generation": "a295b16a796689f3", "index": "9", "size": "3145728", "created": iso(5)}]
    c.app.state.watch_once(snapshots=lambda path: snaps)
    events = lambda: c.get("/api/admin/events?type=backup", headers=GOOD).json()["events"]  # noqa: E731
    assert [(e["detail"], e["source"]) for e in events()] == [("Litestream snapshot to R2 (3.0 MB, generation a295b16a)", "r2")]
    c.app.state.watch_once(snapshots=lambda path: snaps)
    assert len(events()) == 1  # nothing new

    def broken(path):
        raise RuntimeError("access denied")
    c.app.state.watch_once(snapshots=broken)
    c.app.state.watch_once(snapshots=broken)  # one problem line a day
    assert [(e["detail"], e["ok"]) for e in events()][0] == ("Couldn't check the R2 backup: access denied", False)
    assert len(events()) == 2


def test_backup_goes_stale():
    from datetime import datetime, timezone
    from tastr.web import watch
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    old = [{"generation": "g", "size": "10", "created": "2026-11-01T00:00:00Z"}]
    lines, newest, day = watch.backup_events(old, None, "2026-11-01T00:00:00Z", None, now)
    assert [e["detail"] for e in lines] == ["No Litestream snapshot in R2 for 36 hours"] and day == "2026-11-02"
    assert watch.backup_events(old, None, newest, day, now)[0] == []
    assert watch._table("replica  generation  index  size  created\ns3  g  1  5  2026-11-01T00:00:00Z\n") == [
        {"replica": "s3", "generation": "g", "index": "1", "size": "5", "created": "2026-11-01T00:00:00Z"}]


def _snapshot(resp):
    import sqlite3
    import tempfile

    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.write(resp.content)  # the client undoes the gzip
    f.close()
    return sqlite3.connect(f.name)


def test_library_snapshot_for_the_browser_is_your_own_library(db_path, home):
    """TAS-49: the page runs SQL in the browser (sql.js) on this file; it holds only what /api/query would show."""
    mine = save(db_path, "friend@example.com", sha="a1")
    theirs = save(db_path, "other@example.com", sha="b2")
    c = make_client(db_path)
    r = c.get("/api/query/library", headers={**GOOD, "accept-encoding": "gzip"})
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
    assert r.headers["cache-control"] == "private, no-store" and int(r.headers["x-library-bytes"]) == len(r.content)
    snap = _snapshot(r)
    assert snap.execute("SELECT id FROM tracks").fetchall() == [(mine,)]
    assert snap.execute("SELECT DISTINCT track_id FROM tags").fetchall() == [(mine,)]
    assert snap.execute("SELECT count(*) FROM sections WHERE track_id = ?", (theirs,)).fetchone() == (0,)
    cols = {r[1] for r in snap.execute("PRAGMA table_info(tracks)")}
    assert not cols & {"owner", "local_path", "source_uri", "stages_json", "errors_json"} and "title" in cols
    tables = {r[0] for r in snap.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == set(db.LIBRARY_TABLES)  # no events, usage, edits or cards
    assert c.get("/api/query/library").status_code in (401, 403)  # needs the login like everything else


def test_admins_snapshot_holds_every_library_unless_mine(db_path, home):
    save(db_path, "friend@example.com", sha="a1"), save(db_path, "other@example.com", sha="b2")
    c = make_client(db_path, admins=("friend@example.com",))
    snap = _snapshot(c.get("/api/query/library", headers=GOOD))
    assert snap.execute("SELECT owner FROM tracks ORDER BY owner").fetchall() == [("friend@example.com",), ("other@example.com",)]
    assert _snapshot(c.get("/api/query/library?mine=1", headers=GOOD)).execute("SELECT count(*) FROM tracks").fetchone() == (1,)
