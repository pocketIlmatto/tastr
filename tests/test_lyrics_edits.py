"""The person's own lyrics edits (tastr/lyrics.py: edit_lines): checking what the page sends, keeping edits over new
LRCLIB or Whisper answers, undoing them, and listening timing edited words. Every lyric here is made up."""

import time

import pytest
from fastapi.testclient import TestClient

from tastr import db, listen, lyrics, stems
from tastr.web.server import create_app
from test_foundation import full_card
from test_listen import heard
from test_lookup import canned

LRC = "[00:01.50]Paper boats on a parking lot\n[00:04.20]Hum it back to me\n"
HIT = {"id": 7, "trackName": "Paper Boats", "artistName": "The Testers", "albumName": "Demo", "duration": 180.0,
       "instrumental": False, "plainLyrics": "Paper boats on a parking lot\nHum it back to me", "syncedLyrics": LRC}


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setenv("TASTR_LYRICS", "lrclib")
    monkeypatch.setattr(lyrics.LRCLIB_PROVIDER, "_guard", None)
    monkeypatch.setattr(lyrics.LRCLIB_PROVIDER, "min_interval", 0.0)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def tagged(db_path, tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"RIFF" + bytes(100))
    card = full_card()
    card.source.artist = "The Testers"
    card.source.file_tags = {"artist": "The Testers", "title": "Paper Boats"}
    card.source.local_path = str(audio)
    card.source.sha256 = "beef" * 16
    conn = db.connect(db_path)
    tid = db.save_card(conn, card)
    conn.close()
    return tid


# ---- clean_edit ----

def test_timed_lines_are_sorted_tidied_and_trailing_blanks_dropped():
    got = lyrics.clean_edit({"synced": [{"t": 9.123, "line": "  second   line "}, {"t": 2, "line": "first\nline"},
                                        {"t": 5, "line": ""}, {"t": 12, "line": ""}]})
    assert got["synced"] == [{"t": 2.0, "line": "first line"}, {"t": 5.0, "line": ""}, {"t": 9.12, "line": "second line"}]
    assert got["plain"] == "first line\nsecond line"


def test_lines_without_a_time_keep_their_place_as_guesses():
    got = lyrics.clean_edit({"synced": [{"t": 8, "line": "a"}, {"t": None, "line": "b"}, {"t": 3, "line": "c"}]})
    assert [(x["t"], x["line"], bool(x.get("guess"))) for x in got["synced"]] == [(8.0, "a", False), (None, "b", True), (3.0, "c", False)]


def test_no_times_at_all_is_plain_and_plain_is_tidied():
    assert lyrics.clean_edit({"synced": [{"t": None, "line": "a"}, {"line": "b"}]}) == {"synced": [], "plain": "a\nb"}
    assert lyrics.clean_edit({"plain": "\n  one  two \n\nthree\n"}) == {"synced": [], "plain": "one two\n\nthree"}


@pytest.mark.parametrize("body", [
    None, [], {}, {"plain": 3}, {"plain": "   \n "}, {"synced": "x"}, {"synced": [{"t": None, "line": ""}]},
    {"synced": [{"t": -1, "line": "a"}]}, {"synced": [{"t": float("nan"), "line": "a"}]}, {"synced": [{"t": True, "line": "a"}]},
    {"synced": [{"t": "1", "line": "a"}]}, {"synced": [{"t": 1, "line": 5}]}, {"synced": ["a"]},
    {"synced": [{"t": 1, "line": "x" * (lyrics.MAX_LINE + 1)}]}, {"plain": "x" * (lyrics.MAX_PLAIN + 1)},
    {"synced": [{"t": 1, "line": "a"}] * (lyrics.MAX_LINES + 1)},
])
def test_bad_edits_are_refused(body):
    with pytest.raises(ValueError):
        lyrics.clean_edit(body)


# ---- describe ----

def test_edits_show_over_the_stored_answer_and_say_when_it_changes():
    stored = lyrics.result(HIT, lyrics.LRCLIB_PROVIDER) | {"fetched_at": "2026-11-18 01:00:00"}
    edits = {"base": "lrclib", "base_sig": lyrics.signature(stored), "synced": [{"t": 1.0, "line": "Paper boats"}],
             "plain": "Paper boats", "timed_by": None, "edited_at": "2026-11-18 02:00:00"}
    got = lyrics.describe(stored, edits)
    assert got["edited"] and got["state"] == "found" and got["synced"] == edits["synced"] and got["timed"]
    assert got["source"] == "lrclib" and got["base"] == "lrclib" and got["newer"] is False
    changed = stored | {"synced": [{"t": 1.6, "line": "Paper boats on a parking lot"}]}
    assert lyrics.describe(changed, edits)["newer"] is True
    assert lyrics.describe(lyrics.result(HIT | {"instrumental": True}, lyrics.LRCLIB_PROVIDER), edits)["newer"] is False


def test_edits_without_any_answer_and_timed_by_listening():
    edits = {"base": "none", "base_sig": "", "synced": [], "plain": "Typed by hand", "timed_by": None, "edited_at": "x"}
    got = lyrics.describe(None, edits)
    assert got["state"] == "found" and got["plain"] == "Typed by hand" and not got["timed"] and not got["newer"]
    assert "timed_by" not in lyrics.describe({"provider": "lrclib", "status": "found", "timed_by": "whisper", "synced": []}, edits)
    assert lyrics.describe(None, edits | {"timed_by": "whisper"})["timed_by"] == "whisper"


# ---- the API ----

def test_api_edit_keep_over_a_new_answer_and_undo(db_path, tagged, monkeypatch):
    client = TestClient(create_app(db_path))
    canned(monkeypatch, lyrics.LRCLIB_PROVIDER, (200, HIT), (200, HIT | {"syncedLyrics": "[00:02.00]Paper boats, new\n"}))
    client.post(f"/api/tracks/{tagged}/lyrics")
    mine = [{"t": 1.4, "line": "Paper boats in a parking lot"}, {"t": 4.2, "line": "Hum it back to me"},
            {"t": 7.0, "line": "Hum it back again"}]
    r = client.put(f"/api/tracks/{tagged}/lyrics/edits", json={"synced": mine})
    assert r.status_code == 200
    got = r.json()
    assert got["edited"] and got["synced"] == mine and got["base"] == "lrclib" and not got["newer"]
    assert got["available"] is True and got["listen"] == {"state": "none"}
    assert client.get(f"/api/tracks/{tagged}").json()["lyrics"]["synced"] == mine

    again = client.post(f"/api/tracks/{tagged}/lyrics").json()  # LRCLIB has changed its lines: edits stay
    assert again["synced"] == mine and again["newer"] is True
    kept = client.post(f"/api/tracks/{tagged}/lyrics/edits/keep").json()
    assert kept["synced"] == mine and kept["newer"] is False

    undone = client.delete(f"/api/tracks/{tagged}/lyrics/edits").json()
    assert "edited" not in undone and undone["synced"] == [{"t": 2.0, "line": "Paper boats, new"}]


def test_api_type_lyrics_with_none_found_and_bad_edits(db_path, tagged):
    client = TestClient(create_app(db_path))
    got = client.put(f"/api/tracks/{tagged}/lyrics/edits", json={"plain": "Made up line one\nMade up line two"}).json()
    assert got["state"] == "found" and got["base"] == "none" and got["plain"].endswith("two") and not got["timed"]
    assert client.put(f"/api/tracks/{tagged}/lyrics/edits", json={"plain": ""}).status_code == 422
    assert client.put("/api/tracks/9999/lyrics/edits", json={"plain": "a"}).status_code == 404
    assert client.delete(f"/api/tracks/{tagged}/lyrics/edits").json() == {
        "state": "none", "available": True, "listen": {"state": "none"}, "listen_available": False}


def test_listening_times_edited_words_and_leaves_lrclib_alone(db_path, home, tagged, monkeypatch):
    f = stems.stems_dir("beef" * 16, 0)
    f.mkdir(parents=True, exist_ok=True)
    for n in stems.STEM_NAMES:
        stems.stem_path(f, n).write_bytes(b"fLaC" + n.encode())
    monkeypatch.setattr(listen, "available", lambda: True)
    monkeypatch.setattr(listen, "hear", lambda folder: heard((3.0, "paper lanterns drift away"), (9.0, "over the quiet harbour")))
    client = TestClient(create_app(db_path))
    client.put(f"/api/tracks/{tagged}/lyrics/edits", json={"plain": "Paper lanterns drift away\nOver the quiet harbour"})
    assert client.post(f"/api/tracks/{tagged}/lyrics/listen").status_code == 202
    end = time.monotonic() + 5
    while time.monotonic() < end:
        got = client.get(f"/api/tracks/{tagged}/lyrics").json()
        if got["timed"]:
            break
        time.sleep(0.05)
    assert got["edited"] and got["timed_by"] == "whisper"
    assert [(x["t"], x["line"]) for x in got["synced"]] == [(3.0, "Paper lanterns drift away"), (9.0, "Over the quiet harbour")]
    conn = db.connect(db_path)
    assert db.get_lyrics(conn, tagged) is None  # nothing heard was stored over LRCLIB's (absent) answer
    conn.close()


def test_time_edits_without_a_match_leaves_them():
    edits = {"base": "none", "base_sig": "", "synced": [], "plain": "Nothing like it", "timed_by": None, "edited_at": "x"}
    assert listen.time_edits(edits, heard((1.0, "completely different words here"))) == edits


def test_lyrics_edits_stay_out_of_the_downloaded_library():
    assert "lyrics_edits" not in db.LIBRARY_TABLES
