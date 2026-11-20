"""The capture address (tastr/web/routes/capture.py): a key that can only add moments to one person's bank."""

import io
import logging

import wave

import pytest
from fastapi.testclient import TestClient

from tastr import db, deposits
from tastr.web import hosted
from tastr.web.hosted import Settings
from tastr.web.server import create_app
from test_hosted import AUD, GOOD, TEAM, stub_verifier

KEY = "k" * 40
OTHER = "o" * 40
AUTH = {"authorization": f"Bearer {KEY}"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("TASTR_HOME", str(h))
    return h


def make(db_path, keys=f"friend@example.com:{KEY}, other@example.com:{OTHER}", **kw):
    s = Settings(hosted=True, access_team_domain=TEAM, access_aud=AUD,
                 capture_keys=hosted.capture_keys(keys, True), **kw)
    return TestClient(create_app(db_path, s, verifier=stub_verifier))


@pytest.fixture
def client(db_path, home):
    return make(db_path)


def wav(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * int(8000 * seconds))
    return buf.getvalue()


def bank(db_path, owner="friend@example.com"):
    with db.connect(db_path) as c:
        return deposits.find(c, owner)["deposits"]


def test_keys_parse_and_skip_weak_or_ownerless(caplog):
    with caplog.at_level(logging.WARNING):
        keys = hosted.capture_keys(f"A@x.com:{KEY}\nb@x.com:tiny123,{OTHER}", hosted=True)
    assert [k[0] for k in keys] == ["a@x.com"]
    assert KEY not in caplog.text and "tiny123" not in caplog.text and OTHER not in caplog.text
    assert "@" not in caplog.text and "entry 2" in caplog.text and "entry 3" in caplog.text
    assert hosted.capture_keys(OTHER, hosted=False)[0][0] is None  # your own machine: one library
    assert hosted.capture_owner(keys, KEY) == (True, "a@x.com")
    assert hosted.capture_owner(keys, KEY + "x") == (False, None)
    assert KEY not in repr(Settings(capture_keys=keys))


def test_wrong_missing_or_access_token_is_refused(client, db_path):
    form = {"heard_on": "shazam", "title": "Song"}
    assert client.post("/pub/capture", data=form).status_code == 401
    assert client.post("/pub/capture", data=form, headers={"authorization": "Bearer " + "x" * 40}).status_code == 401
    assert client.post("/pub/capture", data=form, headers={"authorization": KEY}).status_code == 401
    assert client.post("/pub/capture", data=form, headers=GOOD).status_code == 401  # a login isn't a capture key
    assert client.get("/pub/capture/now").status_code == 401
    assert bank(db_path) == []


def test_no_keys_set_means_the_address_is_shut(db_path, home):
    c = make(db_path, keys="")
    assert c.post("/pub/capture", data={"heard_on": "shazam"}, headers={"authorization": "Bearer "}).status_code == 401


def test_the_key_opens_nothing_else(client):
    for method, path in (("get", "/api/deposits"), ("get", "/api/tracks"), ("delete", "/api/deposits/1"),
                         ("get", "/pub/capture"), ("get", "/api/config")):
        assert getattr(client, method)(path, headers=AUTH).status_code in (401, 405)


def test_now_playing_says_nothing_until_spotify_is_connected(client):
    assert client.get("/pub/capture/now", headers=AUTH).json() == {"playing": False}


def test_a_spotify_match_becomes_a_moment_that_needs_its_source(client, db_path):
    r = client.post("/pub/capture", headers=AUTH, data={
        "heard_on": "spotify", "title": "Pili Pili", "artist": "Polo & Pan", "position_s": "95.4",
        "link": "https://open.spotify.com/track/abc", "note": "that drop", "marked_at": "2026-11-20T14:03:00-08:00",
        "client": "ios-shortcut/1", "audio": ""})
    assert r.status_code == 201, r.text
    assert r.json() == {"id": r.json()["id"], "title": "Polo & Pan - Pili Pili", "status": "needs-source"}
    [d] = bank(db_path)
    assert d["size"] == "moment" and d["note"] == "that drop" and d["source_kind"] == "link"
    assert d["source_uri"] == "https://open.spotify.com/track/abc"
    assert (d["start_s"], d["end_s"]) == (75.4, 105.4)
    ctx = d["context"]
    assert ctx["via"] == "spotify" and ctx["title"] == "Pili Pili" and ctx["artist"] == "Polo & Pan"
    assert ctx["at_s"] == 95.4 and ctx["site"] == "open.spotify.com" and "recording" not in ctx
    assert ctx["capture"] == {"marked_at": "2026-11-20T14:03:00-08:00", "heard_on": "spotify", "title": "Pili Pili",
                              "artist": "Polo & Pan", "position_s": 95.4, "link": "https://open.spotify.com/track/abc",
                              "client": "ios-shortcut/1"}
    assert bank(db_path, "other@example.com") == []  # only the key owner's bank


def test_a_recording_alone_is_ready_and_kept(client, db_path, home):
    r = client.post("/pub/capture", headers={"authorization": f"Bearer {OTHER}"},
                    data={"heard_on": "recording", "marked_at": "2026-11-20T14:03:00+00:00"},
                    files={"audio": ("Recording.wav", wav(2), "audio/wav")})
    assert r.status_code == 201, r.text
    assert r.json()["title"] == "Recording, 20 Nov 2026, 14:03" and r.json()["status"] == "ready"
    [d] = bank(db_path, "other@example.com")
    name = d["context"]["recording"]
    assert name.startswith("captures/") and name.endswith(".wav") and (home / name).is_file()
    assert d["source_kind"] == "recording" and d["start_s"] is None


def test_a_failed_capture_keeps_a_recording_an_earlier_one_uses(client, db_path, home):
    clip = wav(2)
    first = client.post("/pub/capture", headers=AUTH, data={"heard_on": "recording"},
                        files={"audio": ("r.wav", clip, "audio/wav")})
    assert first.status_code == 201
    [d] = bank(db_path)
    kept = home / d["context"]["recording"]
    bad = client.post("/pub/capture", headers=AUTH, data={"heard_on": "recording", "position_s": "5"},
                      files={"audio": ("r.wav", clip, "audio/wav")})
    assert bad.status_code == 201 and kept.is_file()  # the same audio again: shared, not stored twice
    import tastr.deposits as dep

    def fail(*a, **k):
        raise dep.DepositError("no")
    orig, dep.add = dep.add, fail
    try:
        assert client.post("/pub/capture", headers=AUTH, data={"heard_on": "recording"},
                           files={"audio": ("r.wav", clip, "audio/wav")}).status_code == 422
    finally:
        dep.add = orig
    assert kept.is_file()


def test_a_long_artist_and_title_are_shortened_not_refused(client):
    r = client.post("/pub/capture", headers=AUTH, data={"heard_on": "shazam", "artist": "a" * 200, "title": "t" * 200})
    assert r.status_code == 201 and len(r.json()["title"]) == deposits.MAX_TITLE


def test_a_full_server_refuses_the_recording_not_the_capture(db_path, home, monkeypatch):
    from tastr.web.routes import capture
    monkeypatch.setattr(capture, "stored_bytes", lambda _home: 11 * 1024 ** 3)
    c = make(db_path, storage_gb=10)
    r = c.post("/pub/capture", headers=AUTH, data={"heard_on": "recording"},
               files={"audio": ("r.wav", wav(2), "audio/wav")})
    assert r.status_code == 507 and r.json()["detail"]
    assert c.post("/pub/capture", headers=AUTH, data={"heard_on": "shazam", "title": "x"}).status_code == 201


def test_limits(db_path, home):
    c = make(db_path, capture_mb=0.5, capture_seconds=5, daily_captures=2)
    big = c.post("/pub/capture", headers=AUTH, data={"heard_on": "recording"},
                 files={"audio": ("r.wav", wav(40), "audio/wav")})
    assert big.status_code == 413 and "MB" in big.json()["detail"]
    long = c.post("/pub/capture", headers=AUTH, data={"heard_on": "recording"},
                  files={"audio": ("r.wav", wav(10), "audio/wav")})
    assert long.status_code == 413 and "seconds" in long.json()["detail"]
    assert not list((home / "captures").rglob("*.wav"))  # refused recordings aren't kept
    odd = c.post("/pub/capture", headers=AUTH, data={"heard_on": "recording"},
                 files={"audio": ("r.txt", b"hello", "text/plain")})
    assert odd.status_code == 422
    for _ in range(2):
        assert c.post("/pub/capture", headers=AUTH, data={"heard_on": "shazam", "title": "x"}).status_code == 201
    capped = c.post("/pub/capture", headers=AUTH, data={"heard_on": "shazam", "title": "x"})
    assert capped.status_code == 429 and capped.json()["detail"]
    assert c.post("/pub/capture", headers={"authorization": f"Bearer {OTHER}"},
                  data={"heard_on": "shazam"}).status_code == 201  # each owner has their own cap


@pytest.mark.parametrize("form", [{"heard_on": "radio"}, {}, {"heard_on": "spotify", "position_s": "soon"},
                                  {"heard_on": "spotify", "marked_at": "yesterday"},
                                  {"heard_on": "spotify", "link": "javascript:alert(1)"},
                                  {"heard_on": "spotify", "title": "x" * 301}])
def test_fields_tastr_cant_read(client, db_path, form):
    r = client.post("/pub/capture", headers=AUTH, data=form)
    assert r.status_code == 422 and r.json()["detail"]
    assert bank(db_path) == []


def test_without_a_match_or_audio_the_title_names_the_time(client):
    r = client.post("/pub/capture", headers=AUTH, data={"heard_on": "shazam", "marked_at": "2026-01-02T03:04:05Z"})
    assert r.json()["title"] == "Capture, 2 Jan 2026, 03:04" and r.json()["status"] == "needs-source"


def test_local_server_takes_a_bare_key(db_path, home):
    c = TestClient(create_app(db_path, Settings(capture_keys=hosted.capture_keys(KEY, False))))
    assert c.post("/pub/capture", headers=AUTH, data={"heard_on": "shazam", "title": "x"}).status_code == 201
    assert len(bank(db_path, None)) == 1


def test_settings_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("TASTR_CAPTURE_KEYS", f"Friend@example.com:{KEY}")
    monkeypatch.setenv("TASTR_DAILY_CAPTURES", "7")
    monkeypatch.setenv("TASTR_CAPTURE_MB", "2")
    monkeypatch.setenv("TASTR_CAPTURE_SECONDS", "30")
    s = Settings.from_env()
    assert s.capture_keys[0][0] == "friend@example.com" and s.daily_captures == 7
    assert (s.capture_mb, s.capture_seconds) == (2, 30)
