"""The Files view's server side (web/routes/files.py, web/downloads.py): sizes, downloads, zips, deleting stems."""

import io
import threading
import zipfile

import pytest
from fastapi.testclient import TestClient

from tastr import db, stems
from tastr.web import downloads
from tastr.web.hosted import Settings
from tastr.web.server import create_app
from test_foundation import full_card
from ui_src import app_js

GOOD = {"cf-access-jwt-assertion": "good"}


def verifier(token):
    return {"good": "friend@example.com", "other": "other@example.com"}[token]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def save(db_path, tmp_path, owner=None, sha="ab" * 32, title="Song", artist="Band", ext=".mp3", size=1000):
    audio = tmp_path / f"{sha[:6]}-{owner or 'local'}{ext}"
    audio.write_bytes(b"A" * size)
    card = full_card()
    card.source.local_path, card.source.sha256, card.source.uri = str(audio), sha, str(audio)
    card.source.title, card.source.artist = title, artist
    conn = db.connect(db_path)
    tid = db.save_card(conn, card, owner)
    conn.close()
    return tid


def make_stems(sha, tid, size=100):
    folder = stems.stems_dir(sha, tid)
    folder.mkdir(parents=True, exist_ok=True)
    for n in stems.STEM_NAMES:
        stems.stem_path(folder, n).write_bytes(n[0].encode() * size)
    return folder


def hosted(db_path, **kw):
    s = Settings(hosted=True, access_team_domain="t.cloudflareaccess.com", access_aud="aud", **kw)
    return TestClient(create_app(db_path, s, verifier=verifier))


def test_files_lists_sizes_and_stems(db_path, home, tmp_path):
    a = save(db_path, tmp_path, sha="aa" * 32, size=1000)
    b = save(db_path, tmp_path, sha="bb" * 32, title="Other", artist=None, ext=".wav", size=500)
    make_stems("aa" * 32, a, size=100)
    body = TestClient(create_app(db_path)).get("/api/files").json()
    assert body["used"] == {"audio_bytes": 1500, "stem_bytes": 400}
    assert body["cap_bytes"] is None and body["free_bytes"] is None  # your own machine has no cap
    by = {t["id"]: t for t in body["tracks"]}
    assert by[a]["audio"] == {"name": "Band - Song.mp3", "format": "MP3", "bytes": 1000}
    assert by[a]["stems"] == {"state": "ready", "bytes": {"vocals": 100, "bass": 100, "other": 100, "drums": 100}}
    assert by[b]["audio"]["name"] == "Other.wav" and by[b]["stems"] is None


def test_files_are_scoped_to_the_owner_with_server_free_space(db_path, home, tmp_path):
    mine = save(db_path, tmp_path, "friend@example.com", sha="aa" * 32, size=1000)
    save(db_path, tmp_path, "other@example.com", sha="bb" * 32, size=3000)
    (home / "uploads").mkdir(parents=True)
    (home / "uploads" / "x.mp3").write_bytes(b"x" * 2048)  # the server's storage counts everyone's files
    body = hosted(db_path, storage_gb=1).get("/api/files", headers=GOOD).json()
    assert [t["id"] for t in body["tracks"]] == [mine]
    assert body["used"]["audio_bytes"] == 1000
    assert body["cap_bytes"] == 1024 ** 3 and body["free_bytes"] == 1024 ** 3 - 2048


def test_download_flag_names_the_file_and_plain_requests_stay_inline(db_path, home, tmp_path):
    tid = save(db_path, tmp_path, title='AC/DC: "Live"?', artist="Bänd")
    make_stems("ab" * 32, tid)
    c = TestClient(create_app(db_path))
    plain = c.get(f"/api/tracks/{tid}/audio")
    assert plain.status_code == 200 and "content-disposition" not in plain.headers
    assert c.get(f"/api/tracks/{tid}/audio", headers={"Range": "bytes=0-3"}).status_code == 206
    cd = c.get(f"/api/tracks/{tid}/audio?download=1").headers["content-disposition"]
    assert cd.startswith('attachment; filename="Bnd - AC DC Live.mp3"') and "filename*=utf-8''B%C3%A4nd%20-%20AC%20DC%20Live.mp3" in cd
    cd = c.get(f"/api/tracks/{tid}/stems/vocals?download=1").headers["content-disposition"]
    assert "B%C3%A4nd%20-%20AC%20DC%20Live%20%28vocals%29.flac" in cd
    assert "content-disposition" not in c.get(f"/api/tracks/{tid}/stems/vocals").headers


def test_stems_zip_holds_the_four_stems(db_path, home, tmp_path):
    tid = save(db_path, tmp_path)
    c = TestClient(create_app(db_path))
    assert c.get(f"/api/tracks/{tid}/stems.zip").status_code == 404
    make_stems("ab" * 32, tid, size=50)
    r = c.get(f"/api/tracks/{tid}/stems.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert r.headers["x-zip-bytes"] == "200" and "Band%20-%20Song%20stems.zip" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == sorted(f"Band - Song ({n}).flac" for n in stems.STEM_NAMES)
    assert z.read("Band - Song (bass).flac") == b"b" * 50
    assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())


def test_bulk_zip_of_audio_and_of_stems(db_path, home, tmp_path):
    a = save(db_path, tmp_path, sha="aa" * 32, size=10)
    b = save(db_path, tmp_path, sha="bb" * 32, size=20)  # same title and artist: the zip numbers the second
    make_stems("aa" * 32, a, size=5)
    c = TestClient(create_app(db_path))
    r = c.post("/api/files/zip", json={"ids": [a, b], "kind": "audio"})
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.namelist() == ["Band - Song.mp3", "Band - Song (2).mp3"] and z.read("Band - Song (2).mp3") == b"A" * 20
    assert "Tastr%20audio%2C%202%20tracks.zip" in r.headers["content-disposition"]
    r = c.post("/api/files/zip", json={"ids": [a, b], "kind": "stems"})  # only a has stems
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == sorted(f"Band - Song/Band - Song ({n}).flac" for n in stems.STEM_NAMES)
    assert c.post("/api/files/zip", json={"ids": [b], "kind": "stems"}).status_code == 404
    assert c.post("/api/files/zip", json={"ids": [a, 999], "kind": "audio"}).status_code == 404


def test_bulk_zip_caps(db_path, home, tmp_path, monkeypatch):
    a = save(db_path, tmp_path, size=100)
    c = TestClient(create_app(db_path))
    r = c.post("/api/files/zip", json={"ids": list(range(1, 202)), "kind": "audio"})
    assert r.status_code == 413 and "up to 200 tracks" in r.json()["detail"]
    monkeypatch.setattr("tastr.web.routes.files.ZIP_MAX_BYTES", 50)
    r = c.post("/api/files/zip", json={"ids": [a], "kind": "audio"})
    assert r.status_code == 413 and "smaller groups" in r.json()["detail"]


def test_bulk_zip_only_reaches_your_own_tracks(db_path, home, tmp_path):
    mine = save(db_path, tmp_path, "friend@example.com", sha="aa" * 32)
    theirs = save(db_path, tmp_path, "other@example.com", sha="bb" * 32)
    c = hosted(db_path)
    assert c.post("/api/files/zip", json={"ids": [mine, theirs], "kind": "audio"}, headers=GOOD).status_code == 404
    assert c.post("/api/files/zip", json={"ids": [mine], "kind": "audio"}, headers=GOOD).status_code == 200
    make_stems("bb" * 32, theirs)
    for method, path in [("get", "/stems.zip"), ("delete", "/stems"), ("get", "/audio?download=1")]:
        assert getattr(c, method)(f"/api/tracks/{theirs}{path}", headers=GOOD).status_code == 404


def test_delete_stems_frees_space_and_logs(db_path, home, tmp_path):
    tid = save(db_path, tmp_path, "friend@example.com")
    folder = make_stems("ab" * 32, tid, size=100)
    c = hosted(db_path)
    r = c.delete(f"/api/tracks/{tid}/stems", headers=GOOD)
    assert r.status_code == 200 and r.json() == {"state": "none", "freed_bytes": 400}
    assert not folder.exists()
    assert c.delete(f"/api/tracks/{tid}/stems", headers=GOOD).status_code == 404
    conn = db.connect(db_path)
    assert conn.execute("SELECT kind, detail FROM events WHERE kind = 'stems'").fetchall()[-1][1] == "Deleted stems for 1 track"
    conn.close()


def test_delete_stems_keeps_a_folder_another_track_shares(db_path, home, tmp_path):
    mine = save(db_path, tmp_path, "friend@example.com", sha="cd" * 32)
    save(db_path, tmp_path, "other@example.com", sha="cd" * 32)  # the same audio in another library
    folder = make_stems("cd" * 32, mine)
    r = hosted(db_path).delete(f"/api/tracks/{mine}/stems", headers=GOOD)
    assert r.status_code == 409 and "same stems" in r.json()["detail"]
    assert stems.is_ready(folder)


def test_delete_stems_refused_while_a_job_runs(db_path, home, tmp_path, monkeypatch):
    tid = save(db_path, tmp_path)
    gate, started = threading.Event(), threading.Event()

    def slow(audio_path, out_dir, progress=None):
        started.set()
        assert gate.wait(5)
        out_dir.mkdir(parents=True, exist_ok=True)
        for n in stems.STEM_NAMES:
            stems.stem_path(out_dir, n).write_bytes(b"x")

    monkeypatch.setattr(stems, "separate", slow)
    monkeypatch.setattr(stems, "available", lambda: True)
    c = TestClient(create_app(db_path))
    assert c.post(f"/api/tracks/{tid}/stems").status_code == 202
    assert started.wait(5)
    r = c.delete(f"/api/tracks/{tid}/stems")
    assert r.status_code == 409 and "being made" in r.json()["detail"]
    assert c.get("/api/files").json()["tracks"][0]["stems"]["state"] == "running"
    gate.set()


def test_clean_name_and_unique():
    assert downloads.clean_name('  ..a/b\\c:d*e?"f<g>h|i\x00 ') == "a b c d e f g h i"
    assert downloads.clean_name("") == "Untitled"
    taken = set()
    assert [downloads.unique(n, taken) for n in ["x.mp3", "X.mp3", "x.mp3", "Band - Song"]] == ["x.mp3", "X (2).mp3", "x (3).mp3", "Band - Song"]


def test_ui_has_files_view():
    js = app_js()
    for needle in ("/api/files", "matchTracks", "stems.zip", "download=1", "Delete stems", "All your files"):
        assert needle in js, needle
