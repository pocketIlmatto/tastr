"""Lyrics by listening (tastr/listen.py): what gets stored, LRCLIB answers that leave it, and the API job. Whisper is
stubbed; every lyric here is made up."""

import time

import pytest
from fastapi.testclient import TestClient

from tastr import db, listen, lyrics, stems
from tastr.web.server import create_app
from test_foundation import full_card


def heard(*lines: tuple[float, str]) -> dict:
    words = []
    for t, line in lines:
        for i, w in enumerate(line.split()):
            words.append({"t": round(t + 0.4 * i, 2), "end": round(t + 0.4 * i + 0.3, 2), "w": w, "p": 0.9})
    return {"words": words, "lines": [{"t": t, "end": t + 2, "line": line} for t, line in lines],
            "language": "en", "model": "large-v3-turbo"}


PLAIN = "Paper lanterns drift away\nOver the quiet harbour\nNobody saw them go\nRound the silver wheel"


def test_plain_lrclib_words_get_times_and_keep_their_words():
    stored = {"provider": "lrclib", "status": "found", "plain": PLAIN, "synced": [], "id": 7, "fetched_at": "x"}
    got = listen.apply(stored, heard((10.0, "paper lanterns drift away"), (14.0, "over the quiet harbor"),
                                     (30.0, "round the silver wheel")))
    assert got["provider"] == "lrclib" and got["timed_by"] == "whisper" and got["plain"] == PLAIN
    assert [x["line"] for x in got["synced"]] == PLAIN.splitlines()
    assert [x["t"] for x in got["synced"]] == [10.0, 14.0, 22.0, 30.0]
    assert [bool(x.get("guess")) for x in got["synced"]] == [False, False, True, False]
    assert "fetched_at" not in got


def test_what_was_heard_becomes_the_lyrics_without_lrclib():
    got = listen.apply({"provider": "lrclib", "status": "not_found"}, heard((5.0, "Paper lanterns drift away")))
    assert got["provider"] == "whisper" and got["status"] == "found"
    assert got["synced"] == [{"t": 5.0, "line": "Paper lanterns drift away"}]
    assert lyrics.describe(got)["label"] == "Whisper" and lyrics.describe(got)["timed"]


def test_nothing_heard():
    assert listen.apply(None, heard())["status"] == "not_found"


def test_fill_times_before_and_after_the_placed_lines():
    got = listen.fill_times([{"t": None, "line": "a"}, {"t": 10.0, "line": "b"}, {"t": None, "line": "c"}])
    assert [x["t"] for x in got] == [8.0, 10.0, 12.0]


@pytest.mark.parametrize("stored, res, kept", [
    ({"provider": "whisper", "status": "found"}, {"status": "not_found"}, True),
    ({"provider": "whisper", "status": "found"}, {"status": "found", "plain": "x"}, False),
    ({"provider": "lrclib", "status": "found", "timed_by": "whisper", "plain": "x"}, {"status": "found", "plain": "x"}, True),
    ({"provider": "lrclib", "status": "found", "timed_by": "whisper", "plain": "x"},
     {"status": "found", "plain": "x", "synced": [{"t": 1, "line": "x"}]}, False),
    ({"provider": "lrclib", "status": "found", "plain": "x"}, {"status": "not_found"}, False),
])
def test_lrclib_answers_keep_lyrics_from_listening(stored, res, kept):
    assert lyrics.keep_stored(stored, res) is kept


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def track(db_path, tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"RIFF" + bytes(100))
    card = full_card()
    card.source.local_path = str(audio)
    card.source.sha256 = "beef" * 16
    conn = db.connect(db_path)
    tid = db.save_card(conn, card)
    conn.close()
    return tid


def test_api_listens_and_stores_heard_lyrics(db_path, home, track, monkeypatch):
    f = stems.stems_dir("beef" * 16, 0)
    f.mkdir(parents=True, exist_ok=True)
    for n in stems.STEM_NAMES:
        stems.stem_path(f, n).write_bytes(b"fLaC" + n.encode())
    monkeypatch.setattr(listen, "available", lambda: True)
    monkeypatch.setattr(listen, "hear", lambda folder: heard((3.0, "Paper lanterns drift away")))
    client = TestClient(create_app(db_path))
    r = client.post(f"/api/tracks/{track}/lyrics/listen")
    assert r.status_code == 202
    end = time.monotonic() + 5
    while time.monotonic() < end:
        got = client.get(f"/api/tracks/{track}/lyrics").json()
        if got["state"] == "found":
            break
        time.sleep(0.05)
    assert got["state"] == "found" and got["source"] == "whisper" and got["listen"] == {"state": "none"}
    assert got["synced"][0]["line"] == "Paper lanterns drift away"
    assert client.get(f"/api/tracks/{track}").json()["lyrics"]["source"] == "whisper"


def test_api_listen_errors(db_path, home, track, monkeypatch):
    client = TestClient(create_app(db_path))
    monkeypatch.setattr(listen, "available", lambda: False)
    assert client.post(f"/api/tracks/{track}/lyrics/listen").status_code == 501
    monkeypatch.setattr(listen, "available", lambda: True)
    monkeypatch.setattr(stems, "available", lambda: False)
    assert client.post(f"/api/tracks/{track}/lyrics/listen").status_code == 409
    assert client.post("/api/tracks/9999/lyrics/listen").status_code == 404


def test_api_separates_the_stems_first(db_path, home, track, monkeypatch):
    def fake_separate(audio, folder, progress=None):
        folder.mkdir(parents=True, exist_ok=True)
        for n in stems.STEM_NAMES:
            stems.stem_path(folder, n).write_bytes(b"fLaC")

    monkeypatch.setattr(stems, "separate_track", fake_separate)
    monkeypatch.setattr(stems, "available", lambda: True)
    monkeypatch.setattr(listen, "available", lambda: True)
    monkeypatch.setattr(listen, "WAIT_SECONDS", 0.05)
    monkeypatch.setattr(listen, "hear", lambda folder: heard((3.0, "Over the quiet harbour")))
    client = TestClient(create_app(db_path))
    assert client.post(f"/api/tracks/{track}/lyrics/listen").json()["state"] in ("queued", "separating", "listening")
    end = time.monotonic() + 5
    while time.monotonic() < end and client.get(f"/api/tracks/{track}/lyrics").json()["state"] != "found":
        time.sleep(0.05)
    assert client.get(f"/api/tracks/{track}/lyrics").json()["synced"][0]["line"] == "Over the quiet harbour"
    assert client.get(f"/api/tracks/{track}/stems").json()["state"] == "ready"
