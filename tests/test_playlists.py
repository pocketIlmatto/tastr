"""Saved lists as playlists (Library › List › Export, TAS-79): M3U8, XSPF and PLS files, and the zip with the audio."""

import io
import xml.etree.ElementTree as ET
import zipfile

import pytest
from fastapi.testclient import TestClient

from tastr.web import playlists
from tastr.web.server import create_app
from test_files import GOOD, hosted, save
from ui_src import app_js

OTHER = {"cf-access-jwt-assertion": "other"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def three(db_path, tmp_path, owner=None):
    """Three tracks; the middle one's audio is gone from the server."""
    a = save(db_path, tmp_path, owner, sha="aa" * 32, title="First", artist="Band", size=10)
    b = save(db_path, tmp_path, owner, sha="bb" * 32, title="Gone", artist=None, size=10)
    c = save(db_path, tmp_path, owner, sha="cc" * 32, title='Third & "Last" <mix>', artist="DJ", ext=".flac", size=20)
    (tmp_path / f"{'bb' * 3}-{owner or 'local'}.mp3").unlink()
    return a, b, c


def test_creating_a_list_keeps_the_order_of_track_ids(db_path, home, tmp_path):
    a, b, c = three(db_path, tmp_path)
    cl = TestClient(create_app(db_path))
    r = cl.post("/api/lists", json={"name": "Set", "track_ids": [c, a, b]}).json()
    assert r["track_ids"] == [c, a, b]
    # Undo after a delete recreates the list the same way: same name, same order
    assert cl.delete(f"/api/lists/{r['id']}").status_code == 204
    again = cl.post("/api/lists", json={"name": "Set", "track_ids": [c, a, b]}).json()
    assert again["created"] and again["track_ids"] == [c, a, b]


def test_m3u8_xspf_and_pls_on_your_own_machine(db_path, home, tmp_path):
    a, b, c = three(db_path, tmp_path)
    cl = TestClient(create_app(db_path))
    lid = cl.post("/api/lists", json={"name": "Warm/up: night?", "track_ids": [c, b, a]}).json()["id"]
    r = cl.get(f"/api/lists/{lid}/playlist.m3u8")
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/x-mpegurl")
    assert "Warm_up_ night_.m3u8" in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[:2] == ["#EXTM3U", "#PLAYLIST:Warm/up: night?"]
    assert lines[2].startswith("#EXTINF:") and lines[2].endswith(',DJ - Third & "Last" <mix>')
    assert lines[3] == f"http://testserver/api/tracks/{c}/audio" and lines[5] == f"http://testserver/api/tracks/{a}/audio"
    assert len(lines) == 6  # the track without audio is left out
    root = ET.fromstring(cl.get(f"/api/lists/{lid}/playlist.xspf").content)
    ns = {"x": "http://xspf.org/ns/0/"}
    assert root.find("x:title", ns).text == "Warm/up: night?"
    tracks = root.findall("x:trackList/x:track", ns)
    assert [t.find("x:title", ns).text for t in tracks] == ['Third & "Last" <mix>', "First"]
    assert tracks[0].find("x:creator", ns).text == "DJ" and int(tracks[0].find("x:duration", ns).text) > 0
    pls = cl.get(f"/api/lists/{lid}/playlist.pls").text.splitlines()
    assert pls[0] == "[playlist]" and "NumberOfEntries=2" in pls and pls[-1] == "Version=2"
    assert f"File2=http://testserver/api/tracks/{a}/audio" in pls and "Title2=Band - First" in pls
    assert cl.get(f"/api/lists/{lid}/playlist.wpl").status_code == 404
    assert cl.get("/api/lists/999/playlist.m3u8").status_code == 404


def test_hosted_playlists_use_signed_links_and_only_your_lists(db_path, home, tmp_path):
    a, _, c = three(db_path, tmp_path, "friend@example.com")
    cl = hosted(db_path, link_secret="s3cret")
    lid = cl.post("/api/lists", json={"name": "Mine", "track_ids": [a, c]}, headers=GOOD).json()["id"]
    lines = cl.get(f"/api/lists/{lid}/playlist.m3u8", headers=GOOD).text.splitlines()
    url = lines[3]
    assert url.startswith("https://testserver/pub/") and url.endswith(f"/tracks/{a}/audio")
    assert cl.get(f"/api/lists/{lid}/playlist.pls", headers=GOOD).headers["x-link-days"] == "7"
    got = cl.get(url.removeprefix("https://testserver"))  # no login, like an outside player
    assert got.status_code == 200 and got.content == b"A" * 10
    for fmt in ("m3u8", "xspf", "pls", "zip"):
        assert cl.get(f"/api/lists/{lid}/playlist.{fmt}", headers=OTHER).status_code == 404
    assert cl.get(f"/api/lists/{lid}/playlist.m3u8").status_code == 401


def test_zip_holds_the_audio_and_a_relative_m3u8(db_path, home, tmp_path):
    a, b, c = three(db_path, tmp_path)
    cl = TestClient(create_app(db_path))
    lid = cl.post("/api/lists", json={"name": "Peak", "track_ids": [c, b, a]}).json()["id"]
    r = cl.get(f"/api/lists/{lid}/playlist.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "Peak.zip" in r.headers["content-disposition"] and int(r.headers["x-zip-bytes"]) > 30
    z = zipfile.ZipFile(io.BytesIO(r.content))
    first = 'audio/01 DJ - Third & Last mix.flac'
    assert z.namelist() == [first, "audio/02 Band - First.mp3", "Peak.m3u8"]
    assert z.read(first) == b"A" * 20
    m3u = z.read("Peak.m3u8").decode().splitlines()
    assert m3u[0] == "#EXTM3U" and m3u[3] == first and m3u[5] == "audio/02 Band - First.mp3" and len(m3u) == 6


def test_zip_limits_and_lists_without_audio(db_path, home, tmp_path, monkeypatch):
    a, b, _ = three(db_path, tmp_path)
    cl = TestClient(create_app(db_path))
    lid = cl.post("/api/lists", json={"name": "L", "track_ids": [a]}).json()["id"]
    gone = cl.post("/api/lists", json={"name": "G", "track_ids": [b]}).json()["id"]
    assert cl.get(f"/api/lists/{gone}/playlist.zip").status_code == 404
    monkeypatch.setattr("tastr.web.routes.files.ZIP_MAX_BYTES", 5)
    r = cl.get(f"/api/lists/{lid}/playlist.zip")
    assert r.status_code == 413 and "smaller groups" in r.json()["detail"]
    monkeypatch.setattr("tastr.web.routes.lists.ZIP_MAX_TRACKS", 0)
    r = cl.get(f"/api/lists/{lid}/playlist.zip")
    assert r.status_code == 413 and "up to 0 tracks" in r.json()["detail"]


def test_playlist_helpers():
    assert playlists.file_name('a/b\\c:d*e?"f<g>h|i' + "x" * 80) == "a_b_c_d_e__f_g_h_i" + "x" * 42
    assert playlists.zip_audio_name(3, 12, "Band - Song", ".mp3") == "audio/03 Band - Song.mp3"
    assert playlists.zip_audio_name(3, 120, "Band - Song", ".mp3") == "audio/003 Band - Song.mp3"
    one = [{"location": "x", "title": "Line\nbreak", "artist": None, "duration": 61.4}]
    assert playlists.m3u8("N\nM", one) == "#EXTM3U\n#PLAYLIST:N M\n#EXTINF:61,Line break\nx\n"


def test_ui_has_playlists():
    js = app_js()
    for needle in ("trackCombo", "askListName", "fitName", "pinPop", "playlist.zip", "mediaSession", "lib-list", "pl-bar",
                   "Undo", "The tracks stay in your library."):
        assert needle in js, needle
