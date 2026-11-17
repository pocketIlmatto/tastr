"""Lyrics from LRCLIB (tastr/lyrics.py): LRC parsing, matching, storage and the API, on canned responses.

The lyrics here are made up: real ones belong to their publishers and don't go in this repository.
"""

import pytest
from fastapi.testclient import TestClient

from tastr import db, lyrics
from tastr.lookup.base import ProviderError, TrackQuery
from tastr.web.server import create_app
from test_foundation import full_card
from test_lookup import canned

LRC = "[ar: Nobody]\n[00:01.50]Paper boats on a parking lot\n[00:04.20][00:20.00]Hum it back to me\n\n[00:07]\n"
HIT = {"id": 7, "trackName": "Paper Boats", "artistName": "The Testers", "albumName": "Demo", "duration": 180.0,
       "instrumental": False, "plainLyrics": "Paper boats on a parking lot\nHum it back to me", "syncedLyrics": LRC}
Q = TrackQuery(artist="The Testers", title="Paper Boats", album="Demo", duration=180.4)


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setenv("TASTR_LYRICS", "lrclib")
    monkeypatch.setattr(lyrics.LRCLIB_PROVIDER, "_guard", None)
    monkeypatch.setattr(lyrics.LRCLIB_PROVIDER, "min_interval", 0.0)


def test_parse_lrc_reads_every_stamp_in_time_order():
    assert lyrics.parse_lrc(LRC) == [
        {"t": 1.5, "line": "Paper boats on a parking lot"}, {"t": 4.2, "line": "Hum it back to me"},
        {"t": 7.0, "line": ""}, {"t": 20.0, "line": "Hum it back to me"}]
    assert lyrics.parse_lrc("no stamps here") == []


def test_off_unless_named(monkeypatch):
    monkeypatch.delenv("TASTR_LYRICS")
    assert lyrics.enabled() is None
    monkeypatch.setenv("TASTR_LYRICS", "LRCLIB")
    assert lyrics.enabled() is lyrics.LRCLIB_PROVIDER


def test_get_matches_by_artist_title_album_and_length(monkeypatch):
    calls = canned(monkeypatch, lyrics.LRCLIB_PROVIDER, (200, HIT))
    got = lyrics.LRCLIB_PROVIDER.lookup(Q)
    assert calls[0][0].endswith("/api/get")
    assert calls[0][1] == {"track_name": "Paper Boats", "artist_name": "The Testers", "duration": "180", "album_name": "Demo"}
    assert got["status"] == "found" and got["synced"][0] == {"t": 1.5, "line": "Paper boats on a parking lot"}
    assert got["url"] == "https://lrclib.net/lyrics/7"


def test_search_when_get_misses_picks_the_matching_timed_hit(monkeypatch):
    other = HIT | {"id": 1, "artistName": "Someone Else"}
    long = HIT | {"id": 2, "duration": 240.0}
    plain = HIT | {"id": 3, "syncedLyrics": None}
    calls = canned(monkeypatch, lyrics.LRCLIB_PROVIDER, ProviderError("404", 404), (200, [other, long, plain, HIT]))
    got = lyrics.LRCLIB_PROVIDER.lookup(Q)
    assert calls[1][0].endswith("/api/search") and got["id"] == 7


def test_search_with_no_match_is_not_found(monkeypatch):
    canned(monkeypatch, lyrics.LRCLIB_PROVIDER, ProviderError("404", 404), (200, [HIT | {"trackName": "Other"}]))
    assert lyrics.LRCLIB_PROVIDER.lookup(Q)["status"] == "not_found"


def test_server_error_on_get_is_an_error(monkeypatch):
    canned(monkeypatch, lyrics.LRCLIB_PROVIDER, ProviderError("LRCLIB answered HTTP 500", 500))
    with pytest.raises(ProviderError):
        lyrics.LRCLIB_PROVIDER.lookup(Q)


def test_plain_only_hit_has_no_timed_lines():
    got = lyrics.result(HIT | {"syncedLyrics": None}, lyrics.LRCLIB_PROVIDER)
    assert got["synced"] == [] and got["plain"].startswith("Paper boats")
    assert lyrics.describe(got | {"fetched_at": "x"})["timed"] is False


@pytest.fixture
def tagged(db_path):
    card = full_card()
    card.source.artist = "The Testers"
    card.source.file_tags = {"artist": "The Testers", "title": "Paper Boats"}
    conn = db.connect(db_path)
    tid = db.save_card(conn, card)
    conn.close()
    return tid


def test_api_finds_stores_and_keeps_found_lyrics_after_an_error(db_path, tagged, monkeypatch):
    client = TestClient(create_app(db_path))
    assert client.get(f"/api/tracks/{tagged}/lyrics").json() == {"state": "none", "available": True}
    canned(monkeypatch, lyrics.LRCLIB_PROVIDER, (200, HIT), ProviderError("LRCLIB answered HTTP 503", 503))
    r = client.post(f"/api/tracks/{tagged}/lyrics").json()
    assert r["state"] == "found" and r["timed"] and r["label"] == "LRCLIB"
    again = client.post(f"/api/tracks/{tagged}/lyrics").json()
    assert again["state"] == "found" and "503" in again["last_error"]
    card = client.get(f"/api/tracks/{tagged}").json()
    assert card["lyrics"]["state"] == "found" and card["lyrics_available"] is True


def test_api_without_tags_or_when_off(db_path, monkeypatch):
    conn = db.connect(db_path)
    tid = db.save_card(conn, full_card())
    conn.close()
    client = TestClient(create_app(db_path))
    assert client.post(f"/api/tracks/{tid}/lyrics").json()["state"] == "no_tags"
    monkeypatch.delenv("TASTR_LYRICS")
    assert client.post(f"/api/tracks/{tid}/lyrics").status_code == 400
    assert client.get(f"/api/tracks/{tid}/lyrics").json()["available"] is False


def test_lyrics_stay_out_of_the_downloaded_library():
    assert "lyrics" not in db.LIBRARY_TABLES
