"""The audio bucket (tastr/blobs.py): uploads and stems kept in S3-compatible storage (R2), the disk as a cache.

Runs against moto's S3 server on this machine (tastr/s3.py speaks HTTP to it), so no network or credentials are
needed.
"""

import os
from contextlib import closing
import time
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from tastr import blobs, db, service, stems
from tastr.cloud import jobs, remote
from tastr.web import hosted
from tastr.web.hosted import Settings
from tastr.web.server import create_app
from test_foundation import full_card

moto = pytest.importorskip("moto")
moto_server = pytest.importorskip("moto.server")

TEAM, AUD = "friends.cloudflareaccess.com", "aud-tag"
GOOD = {"cf-access-jwt-assertion": "good"}
BUCKET = "tastr-audio-test"


def stub_verifier(token):
    if token != "good":
        raise ValueError("bad token")
    return "friend@example.com"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("TASTR_HOME", str(h))
    return h


@pytest.fixture(scope="session")
def s3_server():
    server = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


def use_bucket(monkeypatch, url):
    """Point blobs at a fresh, empty bucket on the moto server."""
    import urllib.request

    for k, v in {"TASTR_AUDIO_BUCKET": BUCKET, "TASTR_AUDIO_REGION": "us-east-1", "TASTR_AUDIO_ENDPOINT": url,
                 "TASTR_AUDIO_ACCESS_KEY_ID": "test", "TASTR_AUDIO_SECRET_ACCESS_KEY": "test"}.items():
        monkeypatch.setenv(k, v)
    urllib.request.urlopen(urllib.request.Request(url + "/moto-api/reset", method="POST")).close()
    blobs.reset()
    blobs.client().create_bucket()


@pytest.fixture
def bucket(home, monkeypatch, s3_server):
    use_bucket(monkeypatch, s3_server)
    yield
    blobs.reset()


def headers(k):
    with blobs.client().get(k) as resp:
        return resp.headers


def objects(prefix=""):
    blobs.reset_listings()
    return blobs.listing(prefix, fresh=True)


def write(path, data=b"RIFFdata"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_off_without_a_bucket(home, monkeypatch):
    monkeypatch.delenv("TASTR_AUDIO_BUCKET", raising=False)
    f = write(home / "uploads" / "ab" / "song.wav")
    assert not blobs.enabled() and blobs.origin() is None
    assert blobs.push(f) is False and blobs.signed_url(f) is None and blobs.listing("uploads/") == {}
    assert blobs.exists(f) and blobs.ensure(f) == f
    assert blobs.evict(older_than=0) == 0 and f.exists()


def test_keys_follow_the_path_under_tastr_home(home):
    assert blobs.key(home / "uploads" / "ab12" / "song.mp3") == "uploads/ab12/song.mp3"
    assert blobs.key(home / "stems" / "abc" / "drums.flac") == "stems/abc/drums.flac"
    # Only uploads and stems live in the bucket: not the database, not files elsewhere.
    assert blobs.key(home / "tastr.db") is None
    assert blobs.key(home / "uploads") is None
    assert blobs.key(home.parent / "elsewhere.wav") is None


def test_push_ensure_remove(bucket, home):
    f = write(home / "uploads" / "ab" / "song.mp3", b"ID3 audio")
    assert blobs.push(f) and blobs.in_bucket(f)
    assert headers("uploads/ab/song.mp3")["content-type"] == "audio/mpeg"  # so browsers can play it from a signed link
    f.unlink()
    assert blobs.exists(f) and not f.exists()
    assert blobs.ensure(f) == f and f.read_bytes() == b"ID3 audio"  # brought back down
    url = blobs.signed_url(f)
    assert BUCKET in url and "X-Amz-Signature" in url and "X-Amz-Expires=3600" in url
    blobs.remove(f)
    assert not f.exists() and not blobs.exists(f) and objects() == {}


def test_evict_keeps_what_the_bucket_lacks_or_was_just_written(bucket, home):
    old_kept = write(home / "uploads" / "a" / "kept.wav")
    old_only_here = write(home / "uploads" / "b" / "only-here.wav")
    fresh = write(home / "uploads" / "c" / "fresh.wav")
    changed = write(home / "stems" / "s" / "drums.flac")
    for f in (old_kept, fresh, changed):
        blobs.push(f)
    changed.write_bytes(b"different length now")
    t = time.time() - 7200
    for f in (old_kept, old_only_here, changed, old_kept.parent):
        os.utime(f, (t, t))
    assert blobs.evict(older_than=3600) == 1
    assert not old_kept.exists() and not old_kept.parent.exists()
    assert old_only_here.exists() and fresh.exists() and changed.exists()


def test_stored_bytes_and_expiry_read_the_bucket(bucket, home, db_path):
    a = write(home / "uploads" / "a" / "old.wav", b"x" * 10)
    b = write(home / "uploads" / "b" / "new.wav", b"x" * 5)
    for f in (a, b):
        blobs.push(f)
        f.unlink()  # evicted: only the bucket has them
    assert hosted.stored_bytes(home) == 15
    conn = db.connect(db_path)
    ids = []
    for f in (a, b):
        card = full_card()
        card.source.sha256, card.source.local_path, card.source.uri = f.parent.name, str(f), str(f)
        ids.append(db.save_card(conn, card, "friend@example.com"))
    later = time.time() + 20 * 86400  # both uploads are 20 days old then
    assert hosted.expire_audio(conn, home / "uploads", 30, now=later) == []
    assert hosted.expire_audio(conn, home / "uploads", 10, now=later) == ids
    assert objects("uploads/") == {}
    conn.close()


def test_upload_goes_to_the_bucket_and_plays_from_a_signed_link(bucket, home, db_path, monkeypatch):
    seen = []

    def fake(source, options, on_stage=None, browser=None):
        seen.append(blobs.in_bucket(source.local_path))  # already in the bucket when the analysis runs
        card = full_card()
        card.source = source
        return card

    monkeypatch.setattr(service, "_analyze", fake)
    c = TestClient(create_app(db_path, Settings(hosted=True, access_team_domain=TEAM, access_aud=AUD),
                              verifier=stub_verifier))
    r = c.post("/api/analyze", files=[("files", ("song.wav", b"RIFFsong", "audio/wav"))], headers=GOOD)
    tid = r.json()["results"][0]["track_id"]
    assert seen == [True]
    keys = list(objects("uploads/"))
    assert len(keys) == 1 and keys[0].endswith("/song.wav")
    assert [p for p in (home / "uploads").rglob("*") if p.is_file()] == []  # nothing left on the server's disk
    assert c.get(f"/api/tracks/{tid}", headers=GOOD).json()["has_audio"] is True
    got = c.get(f"/api/tracks/{tid}/audio", headers={**GOOD, "origin": "https://strudel.cc"}, follow_redirects=False)
    assert got.status_code == 302 and BUCKET in got.headers["location"]
    assert got.headers["access-control-allow-origin"] == "https://strudel.cc"
    assert c.delete(f"/api/tracks/{tid}", headers=GOOD).status_code == 204
    assert objects("uploads/") == {}  # deleting the track deletes the bucket's copy


def test_csp_lets_the_page_play_from_the_bucket(bucket, db_path, monkeypatch):
    monkeypatch.setenv("TASTR_AUDIO_ENDPOINT", "https://acct.r2.cloudflarestorage.com/")
    c = TestClient(create_app(db_path, Settings(hosted=True, access_team_domain=TEAM, access_aud=AUD),
                              verifier=stub_verifier))
    csp = c.get("/api/health").headers["content-security-policy"]
    assert "media-src 'self' blob: https://acct.r2.cloudflarestorage.com" in csp
    assert "connect-src 'self' https://acct.r2.cloudflarestorage.com" in csp


def test_modal_gets_a_signed_link_instead_of_the_bytes(bucket, home, monkeypatch):
    f = write(home / "uploads" / "a" / "song.wav")
    calls = []
    monkeypatch.setattr(remote, "_call", lambda name, *args: calls.append((name, args)) or full_card().to_dict())
    remote.analyze(f, {"drum_model": True})
    assert calls[-1][1][0] == b"RIFFdata"  # not in the bucket: the bytes
    blobs.push(f)
    f.unlink()
    remote.analyze(f, {"drum_model": True})
    assert calls[-1][1][0].startswith(blobs.endpoint()) and BUCKET in calls[-1][1][0]


def test_modal_writes_stems_straight_to_the_bucket(bucket, home, monkeypatch):
    audio = write(home / "uploads" / "a" / "song.wav")
    blobs.push(audio)
    folder = stems.stems_dir("abc123", 1)
    monkeypatch.setenv("TASTR_STEMS_BACKEND", "modal")

    def fake_stems(path, put_urls=None):
        assert set(put_urls) == set(stems.STEM_NAMES)
        for name in put_urls:  # what Modal's run_stems does with each link
            blobs.client().put(blobs.key(stems.stem_path(folder, name)), b"fLaC", "audio/flac")
        return {n: 4 for n in put_urls}

    monkeypatch.setattr(remote, "stems", fake_stems)
    assert not stems.is_ready(folder)
    stems.separate_track(audio, folder)
    assert stems.is_ready(folder) and not folder.exists()  # ready, and nothing on the server's disk
    assert len(objects("stems/")) == 4
    stems.remove(folder)
    assert objects("stems/") == {} and not stems.is_ready(folder)


def test_modal_jobs_take_bytes_or_an_https_link(tmp_path):
    dest = tmp_path / "a.wav"
    jobs._write_audio(b"RIFF", dest)
    assert dest.read_bytes() == b"RIFF"
    with pytest.raises(ValueError):
        jobs._write_audio("file:///etc/passwd", dest)


VIEWS = {"waveform": {"data": "AAEC"}, "spectrogram": {"png": "data:image/png;base64,AA=="}}


def test_views_data_goes_to_the_bucket_and_the_card_keeps_a_marker(bucket, home, db_path):
    card = full_card()
    card.extras["views"] = VIEWS
    conn = db.connect(db_path)
    tid = db.save_card(conn, card)
    assert card.extras["views"] == VIEWS  # the caller's card is left alone
    assert db.load_card(conn, tid).extras["views"] == blobs.IN_BUCKET
    head = headers(f"views/{tid}.json")
    assert head["content-encoding"] == "gzip" and head["content-type"] == "application/json"
    assert blobs.get_views(tid) == VIEWS
    db.retag_track(conn, tid)  # rewrites the card: the marker stays
    assert db.load_card(conn, tid).extras["views"] == blobs.IN_BUCKET

    c = TestClient(create_app(db_path))
    assert c.get(f"/api/tracks/{tid}").json()["has_views"] is True
    got = c.get(f"/api/tracks/{tid}/views", follow_redirects=False)
    assert got.status_code == 302 and f"views/{tid}.json" in got.headers["location"]
    assert db.delete_track(conn, tid)
    assert objects("views/") == {}
    conn.close()


def test_views_stay_in_the_card_when_the_bucket_cant_take_them(bucket, db_path, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("bucket down")

    monkeypatch.setattr(blobs, "put_views", broken)
    card = full_card()
    card.extras["views"] = VIEWS
    conn = db.connect(db_path)
    tid = db.save_card(conn, card)
    assert db.load_card(conn, tid).extras["views"] == VIEWS
    assert TestClient(create_app(db_path)).get(f"/api/tracks/{tid}/views").json() == VIEWS
    conn.close()


def test_views_go_to_the_bucket_after_the_transaction_on_a_durable_object(bucket, home, tmp_path, monkeypatch):
    # On the Worker a transaction is transactionSync, a synchronous call from JavaScript: a fetch can't be waited
    # on inside one ("Cannot stack switch", T31), so the upload has to happen after it.
    from tastr import do_sqlite

    st = do_sqlite.FakeStorage(tmp_path / "do.db")
    depth = [0]
    run = st._transaction

    def counted(fn):
        depth[0] += 1
        try:
            return run(fn)
        finally:
            depth[0] -= 1

    monkeypatch.setattr(st, "_transaction", counted)
    put = blobs.put_views

    def outside(track_id, views):
        assert depth[0] == 0, "uploaded inside a transaction"
        return put(track_id, views)

    monkeypatch.setattr(blobs, "put_views", outside)
    db.use_storage(lambda path: st)
    try:
        conn = db.connect(tmp_path / "do.db")
        card = full_card()
        card.extras["views"] = VIEWS
        tid = db.save_card(conn, card)
        assert db.load_card(conn, tid).extras["views"] == blobs.IN_BUCKET and blobs.get_views(tid) == VIEWS
    finally:
        db.use_storage(None)


def test_the_sweep_moves_views_a_card_kept(bucket, home, db_path, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("bucket down")

    card = full_card()
    card.extras["views"] = VIEWS
    with monkeypatch.context() as m:
        m.setattr(blobs, "put_views", broken)
        with closing(db.connect(db_path)) as conn:
            tid = db.save_card(conn, card)
    assert db.size_report(db_path)["inline_views"] == 1
    create_app(db_path, Settings(hosted=True, access_team_domain=TEAM, access_aud=AUD),
               verifier=stub_verifier).state.expire_once()
    assert db.size_report(db_path)["inline_views"] == 0 and blobs.get_views(tid) == VIEWS


def test_unpacked_libraries_go_into_the_bucket(bucket, home, db_path, tmp_path):
    import sqlite3
    import tarfile

    from tastr import library

    src = tmp_path / "src.db"
    conn = db.connect(src)
    card = full_card()
    card.source.local_path = "uploads/ab/song.wav"
    db.save_card(conn, card)
    conn.close()
    with closing(sqlite3.connect(src)) as c:
        c.execute("UPDATE tracks SET local_path = 'uploads/ab/song.wav'")
        c.commit()
    song = write(tmp_path / "song.wav")
    with tarfile.open(tmp_path / "lib.tar", "w") as tar:
        tar.add(src, library.DB_NAME)
        tar.add(song, "uploads/ab/song.wav")
    assert library.unpack(tmp_path / "lib.tar", home, db_path)["files"] == 1
    assert list(objects("uploads/")) == ["uploads/ab/song.wav"]


def test_db_size_reports_cards_and_inline_views(db_path, capsys):
    from tastr import cli

    card = full_card()
    card.extras["views"] = VIEWS
    conn = db.connect(db_path)
    db.save_card(conn, card)  # no bucket: the data stays in the card
    conn.close()
    r = db.size_report(db_path)
    assert r["cards"] == 1 and r["inline_views"] == 1 and r["inline_view_bytes"] > 0
    assert r["cards_over_d1_row_limit"] == 0 and r["file_bytes"] > 0
    assert cli.main(["--db", str(db_path), "db-size"]) == 0
    assert "with waveform data inline: 1" in capsys.readouterr().out


def test_files_view_reads_sizes_zips_and_downloads_from_the_bucket(bucket, home, db_path):
    import io
    import zipfile

    audio = write(home / "uploads" / "ab" / "song.wav", b"RIFF" + b"x" * 96)
    card = full_card()
    card.source.local_path, card.source.uri, card.source.title, card.source.sha256 = str(audio), str(audio), "Song", "ab" * 32
    conn = db.connect(db_path)
    tid = db.save_card(conn, card)
    conn.close()
    folder = stems.stems_dir(card.source.sha256, tid)
    for n in stems.STEM_NAMES:
        blobs.push(write(stems.stem_path(folder, n), b"fLaC" + n.encode()))
    blobs.push(audio)
    blobs.evict(older_than=-10)  # only the bucket has them now
    assert not audio.exists() and not folder.exists()

    c = TestClient(create_app(db_path))
    row = c.get("/api/files").json()["tracks"][0]
    assert row["audio"]["bytes"] == 100 and row["stems"]["state"] == "ready"
    assert row["stems"]["bytes"]["vocals"] == len(b"fLaCvocals")
    got = c.get(f"/api/tracks/{tid}/audio?download=1", follow_redirects=False)
    assert got.status_code == 302 and "Song.wav" in unquote(got.headers["location"])  # the bucket names the download
    z = c.get(f"/api/tracks/{tid}/stems.zip")
    assert z.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert sorted(names) == sorted(f"Song ({n}).flac" for n in stems.STEM_NAMES)
    bulk = c.post("/api/files/zip", json={"ids": [tid], "kind": "audio"})
    assert zipfile.ZipFile(io.BytesIO(bulk.content)).read("Song.wav") == b"RIFF" + b"x" * 96
    gone = c.delete(f"/api/tracks/{tid}/stems")
    assert gone.status_code == 200 and gone.json()["freed_bytes"] == sum(len(b"fLaC" + n.encode()) for n in stems.STEM_NAMES)
    assert objects("stems/") == {}


def test_backfill_moves_what_was_there_before_the_bucket(home, db_path, monkeypatch, capsys, s3_server):
    from tastr import cli

    # Before the bucket: an upload, its stems and a card with its waveform data inline
    audio = write(home / "uploads" / "ab" / "old.wav", b"RIFF" + b"x" * 20)
    stem = write(home / "stems" / "abcd" / "drums.flac", b"fLaC")
    write(home / "uploads" / "ab" / "half.wav.part", b"partial")  # a download in progress: never copied
    monkeypatch.delenv("TASTR_AUDIO_BUCKET", raising=False)
    card = full_card()
    card.extras["views"] = VIEWS
    conn = db.connect(db_path)
    tid = db.save_card(conn, card)
    conn.close()
    assert db.size_report(db_path)["inline_views"] == 1

    use_bucket(monkeypatch, s3_server)
    # The bucket is turned on only now, after the files and the card were made.
    assert cli.main(["--db", str(db_path), "bucket-backfill", "--dry-run"]) == 0
    assert "would move 2 file(s)" in capsys.readouterr().out and objects() == {}
    assert db.size_report(db_path)["inline_views"] == 1  # a dry run changes nothing

    assert cli.main(["--db", str(db_path), "bucket-backfill"]) == 0
    assert set(objects()) == {"uploads/ab/old.wav", "stems/abcd/drums.flac", f"views/{tid}.json"}
    assert db.size_report(db_path)["inline_views"] == 0
    conn = db.connect(db_path)
    assert db.load_card(conn, tid).extras["views"] == blobs.IN_BUCKET and blobs.get_views(tid) == VIEWS
    conn.close()
    capsys.readouterr()
    assert cli.main(["--db", str(db_path), "bucket-backfill"]) == 0  # again: nothing left to move
    assert "moved 0 file(s)" in capsys.readouterr().out
    assert audio.exists() and stem.exists()  # the sweep deletes them later, once they're an hour old
    blobs.reset()


def test_backfill_refuses_without_a_bucket(db_path, monkeypatch, capsys):
    from tastr import cli

    monkeypatch.delenv("TASTR_AUDIO_BUCKET", raising=False)
    assert cli.main(["--db", str(db_path), "bucket-backfill"]) == 1
    assert "bucket is off" in capsys.readouterr().err


# ---- uploads straight to the bucket (TASTR_DIRECT_UPLOAD, TAS-66) -------------------------------------------

def direct_client(db_path, monkeypatch, **settings):
    def fake(source, options, on_stage=None, browser=None):
        assert blobs.in_bucket(source.local_path)  # the analysis reads the bucket's copy, as for a plain upload
        card = full_card()
        card.source = source
        return card

    monkeypatch.setattr(service, "_analyze", fake)
    return TestClient(create_app(db_path, Settings(hosted=True, access_team_domain=TEAM, access_aud=AUD,
                                                   direct_upload=True, **settings), verifier=stub_verifier))


def browser_put(upload, data):
    """What the page does: PUT the file to its signed link."""
    import urllib.request

    req = urllib.request.Request(upload["url"], data=data, method="PUT", headers={"Content-Type": upload["content_type"]})
    urllib.request.urlopen(req).close()


def test_direct_upload_goes_to_the_bucket_then_analyzes(bucket, home, db_path, monkeypatch):
    c = direct_client(db_path, monkeypatch)
    assert c.get("/api/config", headers=GOOD).json()["direct_upload"] is True
    r = c.post("/api/uploads", json={"files": [{"name": "my song.wav", "size": 8}]}, headers=GOOD)
    (up,) = r.json()["uploads"]
    assert up["name"] == "my song.wav" and up["content_type"] == "audio/wav" and up["expires_in"] == blobs.UPLOAD_SECONDS
    assert f"uploads/{up['id']}/my%20song.wav" in up["url"] and "Signature" in up["url"]
    browser_put(up, b"RIFFsong")
    r = c.post("/api/analyze/uploaded", data={"ids": [up["id"]]}, headers=GOOD)
    assert r.status_code == 200, r.text
    tid = r.json()["results"][0]["track_id"]
    assert tid is not None
    assert [p for p in (home / "uploads").rglob("*") if p.is_file()] == []  # the local copy was only a cache
    assert list(objects("uploads/")) == [f"uploads/{up['id']}/my song.wav"]
    again = c.post("/api/analyze/uploaded", data={"ids": [up["id"]]}, headers=GOOD)
    assert again.status_code == 404  # an upload is analyzed once


def test_direct_upload_in_the_background(bucket, home, db_path, monkeypatch):
    c = direct_client(db_path, monkeypatch)
    (up,) = c.post("/api/uploads", json={"files": [{"name": "a.wav", "size": 8}]}, headers=GOOD).json()["uploads"]
    browser_put(up, b"RIFFsong")
    r = c.post("/api/analyze/uploaded?background=1", data={"ids": [up["id"]]}, headers=GOOD)
    assert r.status_code == 202
    job = r.json()
    for _ in range(100):
        job = c.get(f"/api/analyze/jobs/{job['id']}", headers=GOOD).json()
        if job["state"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert job["state"] == "done" and job["results"][0]["track_id"] is not None


def test_direct_upload_drops_the_checked_copy_where_the_disk_is_memory(bucket, home, db_path, monkeypatch):
    # The Worker (blobs.keep_copies False): a batch of checked uploads waiting their turn mustn't fill its memory.
    monkeypatch.setattr(blobs, "keep_copies", False)
    c = direct_client(db_path, monkeypatch)
    (up,) = c.post("/api/uploads", json={"files": [{"name": "a.wav", "size": 8}]}, headers=GOOD).json()["uploads"]
    browser_put(up, b"RIFFsong")
    assert c.post(f"/api/uploads/{up['id']}/check", headers=GOOD).status_code == 200
    assert [p for p in (home / "uploads").rglob("*") if p.is_file()] == []
    r = c.post("/api/analyze/uploaded", data={"ids": [up["id"]]}, headers=GOOD)
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["track_id"] is not None  # brought back for its analysis


def test_direct_upload_refusals(bucket, home, db_path, monkeypatch):
    c = direct_client(db_path, monkeypatch, max_upload_mb=10 / 1048576)  # 10 bytes
    ask = lambda files: c.post("/api/uploads", json={"files": files}, headers=GOOD)  # noqa: E731
    assert ask([{"name": "big.wav", "size": 11}]).status_code == 413  # refused before any bytes move
    assert ask([{"name": "notes.txt", "size": 5}]).status_code == 415
    assert ask([]).status_code == 422
    assert c.post("/api/uploads", json={"files": [{"name": "a.wav", "size": 5}]}).status_code == 401
    (up,) = ask([{"name": "a.wav", "size": 5}]).json()["uploads"]
    early = c.post("/api/analyze/uploaded", data={"ids": [up["id"]]}, headers=GOOD)
    assert early.status_code == 409  # the file never reached the bucket; the link still works
    assert c.post("/api/analyze/uploaded", data={"ids": ["../../x"]}, headers=GOOD).status_code == 404
    browser_put(up, b"RIFF-but-too-long")  # the page said 5 bytes, then sent more than the limit
    r = c.post("/api/analyze/uploaded", data={"ids": [up["id"]]}, headers=GOOD)
    assert r.status_code == 413
    assert objects("uploads/") == {}  # nothing of a refused upload stays
    assert c.post("/api/analyze/uploaded", data={"ids": [up["id"]]}, headers=GOOD).status_code == 404


def test_direct_upload_is_only_for_the_person_who_asked(bucket, home, db_path, monkeypatch):
    c = direct_client(db_path, monkeypatch)
    (up,) = c.post("/api/uploads", json={"files": [{"name": "a.wav", "size": 8}]}, headers=GOOD).json()["uploads"]
    browser_put(up, b"RIFFsong")
    from tastr.web.routes.analyze import UPLOAD_MARKERS
    from tastr.markers import Markers
    Markers(db_path, UPLOAD_MARKERS).put(up["id"], {"owner": "someone@else.com", "name": "a.wav", "size": 8})
    assert c.post("/api/analyze/uploaded", data={"ids": [up["id"]]}, headers=GOOD).status_code == 404
    assert c.post(f"/api/uploads/{up['id']}/link", headers=GOOD).status_code == 404


def test_an_expired_link_can_be_renewed(bucket, home, db_path, monkeypatch):
    c = direct_client(db_path, monkeypatch)
    (up,) = c.post("/api/uploads", json={"files": [{"name": "a.wav", "size": 8}]}, headers=GOOD).json()["uploads"]
    fresh = c.post(f"/api/uploads/{up['id']}/link", headers=GOOD).json()
    assert fresh["id"] == up["id"] and f"uploads/{up['id']}/a.wav" in fresh["url"]
    assert c.post("/api/uploads/ffffffff/link", headers=GOOD).status_code == 404


def test_direct_upload_is_off_unless_turned_on(bucket, home, db_path, monkeypatch):
    c = TestClient(create_app(db_path, Settings(hosted=True, access_team_domain=TEAM, access_aud=AUD),
                              verifier=stub_verifier))
    assert c.get("/api/config", headers=GOOD).json()["direct_upload"] is False
    assert c.post("/api/uploads", json={"files": [{"name": "a.wav", "size": 8}]}, headers=GOOD).status_code == 404
    monkeypatch.setenv("TASTR_HOSTED", "1")
    monkeypatch.setenv("TASTR_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("TASTR_ACCESS_AUD", AUD)
    monkeypatch.setenv("TASTR_DIRECT_UPLOAD", "1")
    assert Settings.from_env().direct_upload is True


def test_unfinished_direct_uploads_are_pruned(bucket, home, db_path, monkeypatch):
    from tastr.web.routes.analyze import prune_uploads

    c = direct_client(db_path, monkeypatch)
    (up,) = c.post("/api/uploads", json={"files": [{"name": "a.wav", "size": 8}]}, headers=GOOD).json()["uploads"]
    browser_put(up, b"RIFFsong")
    assert prune_uploads(db_path) == 0  # still within its time
    assert prune_uploads(db_path, now=time.time() + 3 * blobs.UPLOAD_SECONDS) == 1
    assert objects("uploads/") == {}
    assert c.post("/api/analyze/uploaded", data={"ids": [up["id"]]}, headers=GOOD).status_code == 404


def test_an_upload_is_checked_before_its_turn(bucket, home, db_path, monkeypatch):
    c = direct_client(db_path, monkeypatch, max_upload_mb=10 / 1048576)  # 10 bytes
    ask = lambda: c.post("/api/uploads", json={"files": [{"name": "a.wav", "size": 8}]}, headers=GOOD).json()["uploads"][0]  # noqa: E731
    up = ask()
    assert c.post(f"/api/uploads/{up['id']}/check", headers=GOOD).status_code == 409  # not there yet
    browser_put(up, b"RIFFsong")
    r = c.post(f"/api/uploads/{up['id']}/check", headers=GOOD)
    assert r.status_code == 200 and r.json() == {"id": up["id"], "name": "a.wav"}
    r = c.post("/api/analyze/uploaded", data={"ids": [up["id"]]}, headers=GOOD)  # still open, and analyzes
    assert r.status_code == 200 and r.json()["results"][0]["track_id"] is not None
    big = ask()
    browser_put(big, b"RIFF-but-too-long")
    r = c.post(f"/api/uploads/{big['id']}/check", headers=GOOD)
    assert r.status_code == 413
    assert f"uploads/{big['id']}/a.wav" not in objects("uploads/")  # the refused copy is gone at once
    assert c.post("/api/analyze/uploaded", data={"ids": [big["id"]]}, headers=GOOD).status_code == 404


def test_a_storage_refusal_is_logged_for_the_admin(bucket, home, db_path, monkeypatch):
    c = direct_client(db_path, monkeypatch)
    (up,) = c.post("/api/uploads", json={"files": [{"name": "a.wav", "size": 8}]}, headers=GOOD).json()["uploads"]
    assert c.post(f"/api/uploads/{up['id']}/failed", json={"status": 0}, headers=GOOD).json() == {"ok": True}
    with closing(db.connect(db_path)) as conn:
        (e,) = [e for e in db.recent_events(conn, problems=True) if e["source"] == "r2"]
    assert e["kind"] == "failed" and not e["ok"] and "a.wav" in e["detail"] and "CORS" in e["detail"]
    assert c.post(f"/api/uploads/{up['id']}/failed", json={}, headers=GOOD).status_code == 404  # closed; Retry starts anew


def test_a_copied_track_gets_its_own_views(bucket, db_path, home):
    from tastr import library_copy

    conn = db.connect(db_path)
    card = full_card()
    card.source.local_path = str(write(home / "uploads" / "ab" / "x.wav"))
    card.extras["views"] = blobs.IN_BUCKET
    tid = db.save_card(conn, card, owner="a@b.c")
    blobs.put_views(tid, {"wave": [1, 2, 3]})
    new = library_copy.copy_tracks(conn, [tid], "a@b.c", "t@b.c")[tid]
    assert blobs.get_views(new) == {"wave": [1, 2, 3]}
    conn.close()


# ---- uploads of audio the library already has (TAS-94) ----------------------------------------------------------

def counting_client(db_path, monkeypatch, **settings):
    """A hosted app whose analysis is faked; `calls` counts how many files it analyzed."""
    calls = []

    def fake(source, options, on_stage=None, browser=None):
        calls.append(source.local_path)
        card = full_card()
        card.source = source
        return card

    monkeypatch.setattr(service, "_analyze", fake)
    app = create_app(db_path, Settings(hosted=True, access_team_domain=TEAM, access_aud=AUD, **settings),
                     verifier=stub_verifier)
    return TestClient(app), calls


def upload(c, data=b"RIFFsong", name="song.wav"):
    r = c.post("/api/analyze", files=[("files", (name, data, "audio/wav"))], headers=GOOD)
    assert r.status_code == 200, r.text
    return r.json()["results"][0]


def test_the_same_file_again_is_not_analyzed_or_stored_again(bucket, home, db_path, monkeypatch):
    import hashlib

    c, calls = counting_client(db_path, monkeypatch)
    first = upload(c)
    assert "already" not in first
    again = upload(c, name="renamed.wav")  # the same bytes under another name
    assert again == {"track_id": first["track_id"], "title": "renamed", "error": None, "already": True}
    assert len(calls) == 1
    (key,) = objects("uploads/")  # the second copy went, the first is still the track's audio
    assert key.endswith("/song.wav")
    assert [p for p in (home / "uploads").rglob("*") if p.is_file()] == []
    with closing(db.connect(db_path)) as conn:
        assert db.usage_today(conn, "friend@example.com", "tracks", hosted.today()) == 1  # only the first counts
    assert upload(c, b"RIFFother")["track_id"] != first["track_id"]  # other bytes are another track
    assert len(calls) == 2

    sha = hashlib.sha256(b"RIFFsong").hexdigest()
    known = c.post("/api/tracks/known", json={"sha256": [sha, "0" * 64]}, headers=GOOD).json()["known"]
    assert list(known) == [sha] and known[sha]["id"] == first["track_id"]
    assert c.post("/api/tracks/known", json={"sha256": ["nope"]}, headers=GOOD).status_code == 422
    assert c.post("/api/tracks/known", json={"sha256": [sha]}).status_code == 401


def test_known_files_are_per_library(bucket, home, db_path, monkeypatch):
    import hashlib

    c, calls = counting_client(db_path, monkeypatch)
    tid = upload(c)["track_id"]
    sha = hashlib.sha256(b"RIFFsong").hexdigest()
    with closing(db.connect(db_path)) as conn:
        assert service.known_tracks(conn, [sha], "other@example.com") == {}
        assert service.known_tracks(conn, [sha.upper()], "friend@example.com")[sha]["id"] == tid


def test_a_duplicate_direct_upload_in_the_background_is_skipped(bucket, home, db_path, monkeypatch):
    c, calls = counting_client(db_path, monkeypatch, direct_upload=True)
    tids = []
    for _ in range(2):
        (up,) = c.post("/api/uploads", json={"files": [{"name": "a.wav", "size": 8}]}, headers=GOOD).json()["uploads"]
        browser_put(up, b"RIFFsong")
        job = c.post("/api/analyze/uploaded?background=1", data={"ids": [up["id"]]}, headers=GOOD).json()
        for _ in range(100):
            job = c.get(f"/api/analyze/jobs/{job['id']}", headers=GOOD).json()
            if job["state"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert job["state"] == "done"
        tids.append(job["results"][0])
    assert tids[1]["already"] is True and tids[1]["track_id"] == tids[0]["track_id"]
    assert len(calls) == 1 and len(objects("uploads/")) == 1


def test_uploading_a_track_whose_audio_expired_brings_the_audio_back(bucket, home, db_path, monkeypatch):
    c, calls = counting_client(db_path, monkeypatch)
    tid = upload(c)["track_id"]
    with closing(db.connect(db_path)) as conn:
        old = db.load_card(conn, tid).source.local_path
        analyzed = conn.execute("SELECT analyzed_at FROM tracks WHERE id = ?", (tid,)).fetchone()
    blobs.remove(old)  # what expire_audio does
    assert c.get(f"/api/tracks/{tid}", headers=GOOD).json()["has_audio"] is False
    assert c.post("/api/tracks/known", json={"sha256": [db_sha(db_path, tid)]}, headers=GOOD).json()["known"] == {}
    back = upload(c, name="again.wav")
    assert back["track_id"] == tid and back["already"] is True
    assert len(calls) == 1  # not analyzed again
    assert c.get(f"/api/tracks/{tid}", headers=GOOD).json()["has_audio"] is True
    (key,) = objects("uploads/")
    assert key.endswith("/again.wav")
    with closing(db.connect(db_path)) as conn:
        assert db.load_card(conn, tid).source.local_path.endswith("/again.wav")
        assert conn.execute("SELECT analyzed_at FROM tracks WHERE id = ?", (tid,)).fetchone() == analyzed


def db_sha(db_path, tid):
    with closing(db.connect(db_path)) as conn:
        return conn.execute("SELECT sha256 FROM tracks WHERE id = ?", (tid,)).fetchone()[0]


def test_the_orphan_sweep_deletes_old_uploads_no_track_uses(bucket, home, db_path):
    conn = db.connect(db_path)
    used = []
    for i in range(3):
        f = write(home / "uploads" / f"u{i}" / "kept.wav")
        blobs.push(f)
        card = full_card()
        card.source.sha256, card.source.local_path, card.source.uri = f"sha{i}", str(f), str(f)
        db.save_card(conn, card, "friend@example.com")
        used.append(f)
    orphan = write(home / "uploads" / "orphan1" / "old copy.wav")
    waiting = write(home / "uploads" / "open1234" / "a.wav")  # a direct upload not finished yet
    for f in (orphan, waiting):
        blobs.push(f)
    later = time.time() + 3 * 86400
    assert hosted.orphan_uploads(conn, home / "uploads", {"open1234"}) == 0  # too new yet
    assert hosted.orphan_uploads(conn, home / "uploads", {"open1234"}, now=later) == 1
    assert sorted(objects("uploads/")) == sorted(["uploads/open1234/a.wav"] + [f"uploads/u{i}/kept.wav" for i in range(3)])
    assert not orphan.exists() and not orphan.parent.exists()
    conn.close()


def test_the_orphan_sweep_stops_when_most_uploads_match_no_track(bucket, home, db_path):
    conn = db.connect(db_path)
    for i in range(12):  # say the tracks were stored under another TASTR_HOME: nothing matches
        blobs.push(write(home / "uploads" / f"x{i}" / "a.wav"))
    assert hosted.orphan_uploads(conn, home / "uploads", now=time.time() + 3 * 86400) == 0
    assert len(objects("uploads/")) == 12
    conn.close()
