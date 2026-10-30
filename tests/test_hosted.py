"""Hosted mode (tastr/web/hosted.py): Access logins, turned-off features, upload limits, security headers."""

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
    assert c.get("/api/config").json() == {"hosted": False, "links": True, "query": True, "user": None, "limits": None}
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
    assert cfg["hosted"] and not cfg["links"] and not cfg["query"] and cfg["user"] == "friend@example.com"
    assert client.get("/docs", headers=GOOD).status_code == 404


def test_csp_allows_the_pages_own_inline_script():
    hashes = hosted.inline_script_hashes((STATIC_DIR / "index.html").read_text())
    assert len(hashes) == 1 and hashes[0].startswith("'sha256-")


def test_sql_box_is_off(client):
    assert client.post("/api/query", json={"sql": "SELECT 1"}, headers=GOOD).status_code == 403


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
    tid = db.save_card(conn, card)
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
        with pytest.raises(TimeoutError):
            with slots:
                pass
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
