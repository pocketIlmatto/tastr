"""Browser signals (TASTR_BROWSER_SIGNALS): HPSS run by the page (tastr/web/static/signals.js) and the engine's use
of it (tastr/engine/browser_signals.py). The JS runs under Node, so these skip without it."""

import json
import shutil
import struct
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from synth import make_track, write_wav
from tastr.engine import SR, analyze_file, browser_signals, signals
from tastr.engine.card import Source, TrackCard
from tastr.engine.pipeline import AnalysisContext

SIGNALS_JS = Path(__file__).parent.parent / "tastr" / "web" / "static" / "signals.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")


def run_js(code: str, y: np.ndarray, tmp_path: Path) -> bytes:
    """Run `code` under Node with `S` = signals.js and `y` = the audio as a Float32Array; returns what it writes to `out`."""
    src, out = tmp_path / "y.f32", tmp_path / "out.bin"
    y.astype(np.float32).tofile(src)
    script = f"""
const fs = require("fs"), S = require({json.dumps(str(SIGNALS_JS))});
const b = fs.readFileSync({json.dumps(str(src))}), y = new Float32Array(b.buffer, b.byteOffset, b.length / 4);
const out = {json.dumps(str(out))};
{code}
"""
    subprocess.run(["node", "-e", script], check=True)
    return out.read_bytes() if out.exists() else b""


def js_harmonic(y: np.ndarray, tmp_path: Path) -> np.ndarray | None:
    raw = run_js("const h = S.hpssHarmonic(y); if (h) fs.writeFileSync(out, Buffer.from(h.buffer));", y, tmp_path)
    return np.frombuffer(raw, dtype=np.float32) if raw else None


def py_hpss(y: np.ndarray):
    return signals.hpss(AnalysisContext(y=y, sr=SR, card=TrackCard(source=Source(kind="file", uri="x"))))


def encode(y: np.ndarray, harmonic: np.ndarray, sr: int = SR) -> bytes:
    """browser_signals' format, written the way signals.js writes it."""
    scales = [max(float(np.abs(a).max()), 1e-9) / 32767 for a in (y, harmonic)]
    head = json.dumps({"format": "tastr-signals", "version": 1, "sr": sr, "n": int(y.size), "arrays": ["y", "harmonic"],
                       "scales": scales}).encode()
    head += b" " * (len(head) % 2)
    body = b"".join(np.round(a / s).clip(-32767, 32767).astype("<i2").tobytes() for a, s in zip((y, harmonic), scales))
    return b"TSG1" + struct.pack("<I", len(head)) + head + body


@needs_node
def test_js_hpss_matches_the_engine(tmp_path):
    """The page's harmonic part is the engine's to within float rounding, on music and noise, odd lengths included."""
    rng = np.random.default_rng(0)
    for y in (make_track(bpm=124, bars_per_section=(4, 4))[0].astype(np.float32),
              (0.3 * rng.standard_normal(SR * 3 + 77)).astype(np.float32)):
        ours, theirs = js_harmonic(y, tmp_path), py_hpss(y)[0]
        assert ours.shape == theirs.shape
        assert np.abs(ours - theirs).max() < 1e-5 * np.abs(theirs).max()


@needs_node
def test_js_leaves_tracks_shorter_than_the_median_window_to_the_server(tmp_path):
    assert js_harmonic(np.zeros(SR // 2, np.float32), tmp_path) is None  # 22 frames < 31


@needs_node
def test_js_encoding_reads_back_in_python(tmp_path):
    y = (0.5 * np.sin(np.arange(SR * 2) / 7)).astype(np.float32)
    h = (0.25 * np.cos(np.arange(SR * 2) / 5)).astype(np.float32)
    h.tofile(tmp_path / "h.f32")
    data = run_js(f"""const hb = fs.readFileSync({json.dumps(str(tmp_path / "h.f32"))});
fs.writeFileSync(out, Buffer.from(S.encodeSignals(y, new Float32Array(hb.buffer, hb.byteOffset, hb.length / 4))));""", y, tmp_path)
    sig = browser_signals.parse(data, SR)
    assert np.abs(sig.y - y).max() < 2e-5 and np.abs(sig.harmonic - h).max() < 1e-5


def test_parse_refuses_what_does_not_fit():
    y = np.zeros(SR, np.float32)
    good = encode(y, y)
    assert browser_signals.parse(good, SR).y.size == SR
    for bad, why in ((b"nope", "not a signals file"), (good[:-2], "cut short"), (good + b"\0\0", "too long"),
                     (encode(y, y, sr=44100), "44100 Hz")):
        with pytest.raises(browser_signals.SignalsError, match=why):
            browser_signals.parse(bad, SR)


def test_percussive_part_is_audio_minus_harmonic():
    """What the engine rebuilds from the page's harmonic part is HPSS's own percussive part."""
    y = make_track(bpm=124, bars_per_section=(4, 4))[0].astype(np.float32)
    h, p = py_hpss(y)
    rebuilt = browser_signals.BrowserSignals(y=y, harmonic=h, sr=SR).hpss()[1]
    assert np.abs(rebuilt - p).max() < 1e-6


@pytest.mark.audio
def test_engine_uses_the_signals_and_skips_hpss(synth_wav, monkeypatch):
    path, _ = synth_wav
    plain = analyze_file(path)
    from tastr.engine.audio import load_audio

    y = load_audio(path, SR)
    data = encode(y, py_hpss(y)[0])
    monkeypatch.setattr(signals, "hpss", lambda ctx: pytest.fail("HPSS ran on the server"))
    card = analyze_file(path, signals=data)
    assert card.extras["browser_signals"] is True
    assert card.core.bpm == plain.core.bpm and card.core.key.tonic == plain.core.key.tonic
    assert card.core.downbeat_times == plain.core.downbeat_times
    assert [s.start for s in card.sections] == [s.start for s in plain.sections]


@pytest.mark.audio
def test_signals_from_another_file_are_set_aside(synth_wav, tmp_path):
    path, _ = synth_wav
    y = np.zeros(SR * 3, np.float32)  # 3 s of signals for a much longer file
    card = analyze_file(path, ["core"], signals=encode(y, y))
    assert card.extras["browser_signals"].startswith("set aside") and card.core.bpm
    assert analyze_file(path, ["core"], signals=b"garbage").extras["browser_signals"].startswith("set aside")


def wait_job(client, job_id):
    for _ in range(600):
        job = client.get(f"/api/analyze/jobs/{job_id}").json()
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError("job didn't finish")


@pytest.mark.parametrize("flag", ["1", ""])
def test_server_passes_signals_on_only_with_the_flag(flag, db_path, tmp_path, monkeypatch):
    from tastr import service
    from tastr.web.server import create_app
    from test_foundation import full_card

    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TASTR_LOOKUP", "")
    monkeypatch.setenv("TASTR_BROWSER_SIGNALS", flag)
    got = {}

    def fake(source, stages, options, on_stage=None, signals=None):
        got["signals"] = Path(signals).read_bytes() if signals else None
        card = full_card()
        card.source = source
        return card

    monkeypatch.setattr(service, "_analyze", fake)
    client = TestClient(create_app(db_path))
    assert client.get("/api/config").json()["browser_signals"] is bool(flag)
    r = client.post("/api/analyze?background=1", files=[("files", ("song.wav", b"RIFF", "audio/wav")),
                                                        ("signals", ("signals.tsg", b"TSG1 data", "application/octet-stream"))])
    assert wait_job(client, r.json()["id"])["state"] == "done"
    assert got["signals"] == (b"TSG1 data" if flag else None)
    assert not list((tmp_path / "home").rglob("signals.tsg"))  # read, then deleted


@pytest.mark.audio
def test_hosted_style_upload_with_real_signals_is_analyzed_from_them(synth_wav, db_path, tmp_path, monkeypatch):
    from tastr.engine.audio import load_audio
    from tastr.web.server import create_app

    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TASTR_LOOKUP", "")
    monkeypatch.setenv("TASTR_BROWSER_SIGNALS", "1")
    path, _ = synth_wav
    song = tmp_path / "song.wav"
    write_wav(song, load_audio(path, SR))
    y = load_audio(song, SR)
    client = TestClient(create_app(db_path))
    r = client.post("/api/analyze?background=1", files=[("files", ("song.wav", song.read_bytes(), "audio/wav")),
                                                        ("signals", ("signals.tsg", encode(y, py_hpss(y)[0]), "application/octet-stream"))])
    job = wait_job(client, r.json()["id"])
    assert job["state"] == "done", job
    card = client.get(f"/api/tracks/{job['results'][0]['track_id']}").json()
    assert card["extras"]["browser_signals"] is True
