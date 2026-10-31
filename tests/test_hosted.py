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
    c = TestClient(create_app(db_path))
    assert c.get("/api/tracks").status_code == 200
    assert c.get("/api/config").json() == {"hosted": False, "links": True, "query": True, "query_scope": "all", "user": None, "admin": False,
                                          "limits": None}
    assert "content-security-policy" not in c.get("/").headers


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
    for path in ["/", "/api/tracks", "/static/app.js", "/api/config", "/docs", "/openapi.json"]:
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
    assert client.get("/docs", headers=GOOD).status_code == 404


def test_csp_allows_the_pages_own_inline_script():
    hashes = hosted.inline_script_hashes((STATIC_DIR / "index.html").read_text())
    assert len(hashes) == 1 and hashes[0].startswith("'sha256-")


def test_sql_reads_only_your_own_library(db_path, home):
    mine, theirs = save(db_path, "friend@example.com", sha="a1"), save(db_path, "other@example.com", sha="b2")
    c = make_client(db_path)
    q = lambda sql: c.post("/api/query", json={"sql": sql}, headers=GOOD)
    assert q("SELECT id FROM tracks").json()["rows"] == [[mine]]
    assert q("SELECT DISTINCT track_id FROM tags").json()["rows"] == [[mine]]
    assert q("SELECT count(*) FROM sections WHERE track_id = %d" % theirs).json()["rows"] == [[0]]
    cols = q("SELECT * FROM tracks").json()["columns"]
    assert "owner" not in cols and "local_path" not in cols and "source_uri" not in cols and "title" in cols
    for sql in ["SELECT * FROM events", "SELECT * FROM user_usage", "SELECT * FROM track_edits", f"ATTACH '{db_path}' AS d"]:
        assert q(sql).status_code == 400, sql
    assert c.get("/api/config", headers=GOOD).json()["query_scope"] == "mine"


def test_admins_query_the_whole_database(db_path, home):
    save(db_path, "friend@example.com", sha="a1"), save(db_path, "other@example.com", sha="b2")
    c = make_client(db_path, admins=("friend@example.com",))
    r = c.post("/api/query", json={"sql": "SELECT owner FROM tracks ORDER BY owner"}, headers=GOOD).json()
    assert r["rows"] == [["friend@example.com"], ["other@example.com"]]
    assert c.get("/api/config", headers=GOOD).json()["query_scope"] == "all"


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
        with pytest.raises(Exception):
            v(jwt.encode({**claims, **bad}, key, algorithm="RS256"))
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(Exception):
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


def test_uploads_go_to_the_uploaders_library(db_path, home, synth_wav, truth_core):
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
    assert f["today"] == {"tracks": 2, "stems": 0, "visits": 2}  # two /api/config page loads above
    assert by[None]["tracks"] == 1 and by["pam@example.com"]["tracks"] == 0
    assert body["limits"]["daily_tracks"] == 30 and body["storage"]["cap_bytes"] == 10 * 1024 ** 3
    assert body["limits"]["storage_gb"] == 10 and body["limits"]["max_analyses"] == 2
    assert body["analyses"] == {"running": 0, "waiting": 0, "max": 2}
    assert {h["kind"] for h in body["history"]} == {"tracks", "visits"}
    local = TestClient(create_app(db_path, Settings()))
    assert local.get("/api/admin/usage").status_code == 404
    assert body["recent"] == [] and body["blocked_today"] == 0  # friend had usage before their first page load here


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
                   ("upload", True, "Uploaded 1 track"),
                   ("signin", True, "Signed in for the first time")]
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


def test_admins_can_query_only_their_own_library(db_path, home):
    mine = save(db_path, "friend@example.com", sha="a1")
    save(db_path, "other@example.com", sha="b2")
    c = make_client(db_path, admins=("friend@example.com",))
    r = c.post("/api/query", json={"sql": "SELECT id FROM tracks", "mine": True}, headers=GOOD).json()
    assert r["rows"] == [[mine]]  # the Simple search: its rows open cards, and other people's tracks are 404s
