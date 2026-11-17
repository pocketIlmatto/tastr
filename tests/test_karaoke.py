"""Karaoke (tastr/karaoke.py): the API, mixing every stem but the vocals, and the mix going with its stems.

The mix is stubbed in the API tests; one test mixes real (tiny) FLAC stems with soundfile.
"""

import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from tastr import db, karaoke, stems
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
    card.source.sha256 = "cafe" * 16
    conn = db.connect(db_path)
    tid = db.save_card(conn, card)
    conn.close()
    return tid


def folder(home):
    return stems.stems_dir("cafe" * 16, 0)


def with_stems(home):
    f = folder(home)
    f.mkdir(parents=True, exist_ok=True)
    for n in stems.STEM_NAMES:
        stems.stem_path(f, n).write_bytes(b"fLaC" + n.encode())
    return f


@pytest.fixture
def gate():
    ev = threading.Event()
    ev.set()
    return ev


@pytest.fixture
def stub(monkeypatch, gate):
    calls = []

    def fake(srcs, dst):
        assert gate.wait(5)
        calls.append(sorted(Path(s).name for s in srcs))
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"fLaC mix")

    monkeypatch.setattr(karaoke, "mix_files", fake)
    return calls


def wait_for(client, tid, state, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = client.get(f"/api/tracks/{tid}/karaoke").json()
        if st["state"] == state:
            return st
        time.sleep(0.02)
    raise AssertionError(f"karaoke never reached {state}: {st}")


def test_needs_stems_first(client, track, stub):
    assert client.get(f"/api/tracks/{track}/karaoke").json()["state"] == "none"
    r = client.post(f"/api/tracks/{track}/karaoke")
    assert r.status_code == 409 and "stems" in r.json()["detail"]
    assert client.get(f"/api/tracks/{track}/karaoke/audio").status_code == 404


def test_mixes_bass_drums_and_other_then_serves_it(client, track, stub, gate, home):
    with_stems(home)
    gate.clear()
    assert client.post(f"/api/tracks/{track}/karaoke").json()["state"] in ("queued", "running")
    gate.set()
    st = wait_for(client, track, "ready")
    assert st["stems"] == "ready" and stub == [["bass.flac", "drums.flac", "other.flac"]]
    r = client.get(f"/api/tracks/{track}/karaoke/audio")
    assert r.status_code == 200 and r.content == b"fLaC mix" and r.headers["content-type"] == "audio/flac"
    client.post(f"/api/tracks/{track}/karaoke")  # already there: not mixed again
    assert len(stub) == 1
    assert client.get(f"/api/tracks/{track}").json()["karaoke_state"] == "ready"


def test_failed_mix_reports_an_error(client, track, monkeypatch, home):
    with_stems(home)

    def boom(srcs, dst):
        raise ValueError("bad stem")

    monkeypatch.setattr(karaoke, "mix_files", boom)
    client.post(f"/api/tracks/{track}/karaoke")
    assert "bad stem" in wait_for(client, track, "error")["error"]


def test_the_mix_goes_with_its_stems(client, track, stub, home):
    f = with_stems(home)
    client.post(f"/api/tracks/{track}/karaoke")
    wait_for(client, track, "ready")
    assert client.delete(f"/api/tracks/{track}/stems").status_code in (200, 204)
    assert not stems.instrumental_path(f).exists()
    assert client.get(f"/api/tracks/{track}/karaoke").json()["state"] == "none"


def test_separating_again_drops_the_old_mix(home, tmp_path, monkeypatch):
    f = with_stems(home)
    stems.instrumental_path(f).write_bytes(b"old mix")

    def fake_separate(audio, out, progress=None):
        out.mkdir(parents=True, exist_ok=True)
        for n in stems.STEM_NAMES:
            stems.stem_path(out, n).write_bytes(b"new")
        return stems.settings()

    monkeypatch.setattr(stems, "separate", fake_separate)
    stems.separate_track(tmp_path / "a.wav", f)
    assert stems.is_ready(f) and not stems.instrumental_path(f).exists()


def test_real_mix_sums_and_scales_instead_of_clipping(tmp_path):
    sr, n = 44100, 4410
    parts = {"bass": 0.5, "drums": 0.5, "other": 0.5}
    srcs = []
    for name, level in parts.items():
        p = tmp_path / f"{name}.flac"
        sf.write(p, np.full((n, 2), level, dtype="float32"), sr, subtype="PCM_16")
        srcs.append(p)
    karaoke.mix_files(srcs, tmp_path / "mix.flac")
    y, got_sr = sf.read(tmp_path / "mix.flac", dtype="float32")
    assert got_sr == sr and y.shape == (n, 2)
    assert np.abs(y).max() == pytest.approx(1.0, abs=1e-3)  # 1.5 scaled down to full scale, not clipped
    karaoke.mix_files(srcs[:1], tmp_path / "one.flac")
    assert sf.read(tmp_path / "one.flac", dtype="float32")[0].max() == pytest.approx(0.5, abs=1e-3)
