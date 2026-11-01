"""The audio bucket (tastr/blobs.py): uploads and stems kept in S3-compatible storage (R2), the disk as a cache.

Runs against moto's in-memory S3, so no network or credentials are needed.
"""

import os
import time

import pytest
from fastapi.testclient import TestClient

from tastr import blobs, db, service, stems
from tastr.cloud import jobs, remote
from tastr.web import hosted
from tastr.web.hosted import Settings
from tastr.web.server import create_app
from test_foundation import full_card

moto = pytest.importorskip("moto")

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


@pytest.fixture
def bucket(home, monkeypatch):
    for k, v in {"TASTR_AUDIO_BUCKET": BUCKET, "TASTR_AUDIO_REGION": "us-east-1",
                 "TASTR_AUDIO_ACCESS_KEY_ID": "test", "TASTR_AUDIO_SECRET_ACCESS_KEY": "test"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TASTR_AUDIO_ENDPOINT", raising=False)
    with moto.mock_aws():
        blobs.reset()
        blobs.client().create_bucket(Bucket=BUCKET)
        yield
    blobs.reset()


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
    head = blobs.client().head_object(Bucket=BUCKET, Key="uploads/ab/song.mp3")
    assert head["ContentType"] == "audio/mpeg"  # so browsers can play it from a signed link
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
    assert calls[-1][1][0].startswith("https://") and BUCKET in calls[-1][1][0]


def test_modal_writes_stems_straight_to_the_bucket(bucket, home, monkeypatch):
    audio = write(home / "uploads" / "a" / "song.wav")
    blobs.push(audio)
    folder = stems.stems_dir("abc123", 1)
    monkeypatch.setenv("TASTR_STEMS_BACKEND", "modal")

    def fake_stems(path, put_urls=None):
        assert set(put_urls) == set(stems.STEM_NAMES)
        for name in put_urls:  # what Modal's run_stems does with each link
            blobs.client().put_object(Bucket=BUCKET, Key=blobs.key(stems.stem_path(folder, name)), Body=b"fLaC")
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
