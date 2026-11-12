"""Tests for mash-up key lock (tastr/keylock.py): the API, the cache, pruning, delete cleanup and the real stretch.

The stretch is stubbed in the API tests; one test runs Signalsmith Stretch on a tone when python-stretch is
installed (the `keylock` extra) and checks that the tempo changes and the pitch doesn't.
"""

import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tastr import db, keylock, stems
from tastr.web.server import create_app
from test_foundation import full_card


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def client(db_path, home):
    return TestClient(create_app(db_path))


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


@pytest.fixture
def gate():
    ev = threading.Event()
    ev.set()
    return ev


@pytest.fixture
def stub(monkeypatch, gate):
    """Each call: (source, ratio). Writes a small file instead of stretching."""
    calls = []

    def fake(src, dst, ratio):
        assert gate.wait(5)
        calls.append((Path(src).name, ratio))
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"fLaC" + Path(src).name.encode())

    monkeypatch.setattr(keylock, "stretch_file", fake)
    monkeypatch.setattr(keylock, "available", lambda: True)
    return calls


def wait_for(client, tid, ratio, state, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = client.get(f"/api/tracks/{tid}/keylock/{ratio}").json()
        if st["state"] == state:
            return st
        time.sleep(0.02)
    raise AssertionError(f"key lock never reached {state}: {st}")


def test_not_installed_is_501(client, track, monkeypatch):
    monkeypatch.setattr(keylock, "available", lambda: False)
    r = client.post(f"/api/tracks/{track}/keylock/1.034")
    assert r.status_code == 501 and "tastr[keylock]" in r.json()["detail"]
    assert client.get(f"/api/tracks/{track}").json()["keylock_available"] is False


def test_bad_ratio_is_422(client, track, stub):
    for bad in ("abc", "0.5", "2", "nan"):
        assert client.get(f"/api/tracks/{track}/keylock/{bad}").status_code == 422
        assert client.post(f"/api/tracks/{track}/keylock/{bad}").status_code == 422
    assert stub == []


def test_stretches_the_mix_then_serves_it_from_the_cache(client, track, stub, gate, home):
    gate.clear()
    r = client.post(f"/api/tracks/{track}/keylock/1.0342")  # rounded to the mash-up's three decimals
    assert r.status_code == 202 and r.json()["state"] in ("queued", "running") and r.json()["ratio"] == "1.034"
    assert client.get(f"/api/tracks/{track}/keylock/1.034/audio").status_code == 404
    gate.set()
    st = wait_for(client, track, "1.034", "ready")
    assert st["parts"] == ["mix"]
    assert stub == [("song.wav", 1.034)]
    path = home / "keylock" / ("beef" * 4) / "1.034" / "mix.flac"
    assert path.read_bytes() == b"fLaCsong.wav"
    a = client.get(f"/api/tracks/{track}/keylock/1.034/audio", headers={"Origin": "https://strudel.cc"})
    assert a.status_code == 200 and a.content == b"fLaCsong.wav"
    assert a.headers["access-control-allow-origin"] == "https://strudel.cc"
    # Cached: asking again does no new work.
    assert client.post(f"/api/tracks/{track}/keylock/1.034").json()["state"] == "ready"
    time.sleep(0.05)
    assert len(stub) == 1


def test_stems_separated_later_are_stretched_on_their_own(client, track, stub, home):
    client.post(f"/api/tracks/{track}/keylock/0.96")
    wait_for(client, track, "0.960", "ready")
    sdir = home / "stems" / ("beef" * 4)
    sdir.mkdir(parents=True)
    for n in stems.STEM_NAMES:
        stems.stem_path(sdir, n).write_bytes(b"x")
    client.post(f"/api/tracks/{track}/keylock/0.96")
    st = wait_for(client, track, "0.960", "ready")
    end = time.monotonic() + 5
    while len(st["parts"]) < 5 and time.monotonic() < end:
        time.sleep(0.02)
        st = client.get(f"/api/tracks/{track}/keylock/0.960").json()
    assert st["parts"] == ["mix", "vocals", "bass", "other", "drums"]
    assert sorted(s for s, _ in stub) == sorted(["song.wav", "vocals.flac", "bass.flac", "other.flac", "drums.flac"])
    assert client.get(f"/api/tracks/{track}/keylock/0.96/stems/bass").content == b"fLaCbass.flac"
    assert client.get(f"/api/tracks/{track}/keylock/0.96/stems/nope").status_code == 404


def test_each_track_keeps_its_latest_ratios(client, track, stub, home):
    tdir = home / "keylock" / ("beef" * 4)
    for i, r in enumerate(("1.010", "1.020", "1.030", "1.040")):
        client.post(f"/api/tracks/{track}/keylock/{r}")
        wait_for(client, track, r, "ready")
        for f in (tdir / r).glob("*.flac"):  # make the order of writes unambiguous
            os.utime(f, (1_000_000 + i * 10,) * 2)
    assert sorted(d.name for d in tdir.iterdir()) == ["1.020", "1.030", "1.040"]


def test_delete_removes_the_cache(client, track, stub, home):
    client.post(f"/api/tracks/{track}/keylock/1.05")
    wait_for(client, track, "1.050", "ready")
    assert (home / "keylock" / ("beef" * 4)).is_dir()
    assert client.delete(f"/api/tracks/{track}").status_code == 204
    assert not (home / "keylock" / ("beef" * 4)).exists()


def test_failed_stretch_reports_an_error(client, track, monkeypatch):
    monkeypatch.setattr(keylock, "available", lambda: True)

    def boom(src, dst, ratio):
        raise RuntimeError("bad audio")

    monkeypatch.setattr(keylock, "stretch_file", boom)
    client.post(f"/api/tracks/{track}/keylock/1.1")
    st = wait_for(client, track, "1.100", "error")
    assert "bad audio" in st["error"] and st["parts"] == []
    assert not list(keylock.root().rglob("*.flac"))


def test_signed_links_serve_the_key_locked_audio_and_count_toward_a_daily_cap(db_path, home, tmp_path, stub):
    from test_hosted import GOOD, make_client, save

    audio = tmp_path / "song.wav"
    audio.write_bytes(b"RIFF....")
    mine = save(db_path, "friend@example.com", path=str(audio))
    c = make_client(db_path, link_secret="s3cret", daily_keylock=1)
    link = c.get(f"/api/tracks/{mine}/strudel-link", headers=GOOD).json()["audio"]
    kl = link.removesuffix("/audio") + "/keylock/1.034/audio"
    assert c.get(kl).status_code == 404  # not stretched yet
    assert c.post(f"/api/tracks/{mine}/keylock/1.034", headers=GOOD).status_code == 202
    end = time.monotonic() + 5
    while c.get(f"/api/tracks/{mine}/keylock/1.034", headers=GOOD).json()["state"] != "ready" and time.monotonic() < end:
        time.sleep(0.02)
    got = c.get(kl, headers={"origin": "https://strudel.cc"})  # no login, like strudel.cc
    assert got.status_code == 200 and got.content == b"fLaCsong.wav"
    assert c.options(kl, headers={"origin": "https://strudel.cc"}).status_code == 204
    assert c.get(link.removesuffix("/audio") + "/keylock/1.034/stems/bass").status_code == 404
    # Cached: free. A new ratio is new work, over today's cap of 1.
    assert c.post(f"/api/tracks/{mine}/keylock/1.034", headers=GOOD).status_code == 202
    r = c.post(f"/api/tracks/{mine}/keylock/1.05", headers=GOOD)
    assert r.status_code == 429 and "key-locked" in r.json()["detail"]


def test_real_stretch_keeps_the_pitch(tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("python_stretch")
    sf = pytest.importorskip("soundfile")
    pytest.importorskip("librosa")
    sr = keylock.SAMPLE_RATE
    t = np.arange(sr * 4) / sr
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype("float32")
    sf.write(tmp_path / "a.wav", np.stack([tone, tone], 1), sr)
    keylock.stretch_file(tmp_path / "a.wav", tmp_path / "b.flac", 1.25)
    y, got_sr = sf.read(tmp_path / "b.flac")
    assert got_sr == sr and y.shape[1] == 2
    assert abs(len(y) / sr - 4 / 1.25) < 0.05  # faster
    seg = y[sr // 2: sr * 2, 0]
    freq = np.argmax(np.abs(np.fft.rfft(seg))) * sr / len(seg)
    assert abs(freq - 440) < 3  # same pitch
