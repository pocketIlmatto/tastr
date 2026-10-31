"""P4 and P5 of the Rust engine (docs/rust-engine.md): the Rust engine making every card (tastr/engine/rust.py),
the Python engine only when asked for or when Rust can't, and whole cards made in the browser (TASTR_BROWSER_ENGINE, tastr/engine/browser_card.py). The parts that need the Rust
library or its WebAssembly build skip without them:

    cd engine-rs && cargo build --release && cargo build --release --lib --target wasm32-unknown-unknown
"""

import json
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from synth import make_track, write_wav

from tastr.engine import DecodeError, analyze_file, browser_card, rust
from tastr.engine.card import Source

ROOT = Path(__file__).parent.parent / "engine-rs"
NATIVE = ROOT / "target" / "release" / "tastr-engine"
needs_lib = pytest.mark.skipif(not (rust.available() and NATIVE.exists()), reason="needs the Rust engine built")
needs_wasm = pytest.mark.skipif(not (rust.wasm_path() and shutil.which("node")),
                                reason="needs the Rust engine's WebAssembly build and Node")


@pytest.fixture(scope="module")
def song(tmp_path_factory):
    path = tmp_path_factory.mktemp("rust") / "song.wav"
    write_wav(path, make_track(bpm=124, bars_per_section=(8, 8))[0])
    return path


def test_engine_choice(monkeypatch):
    monkeypatch.delenv("TASTR_ENGINE", raising=False)
    assert rust.chosen() == "rust"
    monkeypatch.setenv("TASTR_ENGINE", "Python")
    assert rust.chosen() == "python" and rust.chosen({"engine": "rust"}) == "rust"
    monkeypatch.setenv("TASTR_ENGINE", "fortran")
    assert rust.chosen() == "rust"


@needs_lib
def test_rust_engine_makes_the_card_the_binary_makes(song, tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_ENGINE", "rust")
    card = analyze_file(song)
    assert card.extras["engine"] == f"rust {rust.version()}" and not card.errors
    assert card.source.local_path == str(song) and card.source.title == "song"
    out = tmp_path / "card.json"
    subprocess.run([str(NATIVE), "card", str(song), str(out)], check=True, capture_output=True)
    d = card.to_dict()
    d.pop("source"), d["extras"].pop("engine")
    want = json.loads(out.read_text())
    want.pop("source")
    assert d == want
    # The meter option reaches it.
    assert analyze_file(song, options={"beats_per_bar": 3}).core.beats_per_bar == 3


@needs_lib
def test_samples_in_gives_the_same_card_as_the_binary(tmp_path):
    y = make_track(bpm=128, bars_per_section=(4, 4))[0].astype(np.float32)
    raw = tmp_path / "y.f32"
    y.tofile(raw)
    out = tmp_path / "card.json"
    subprocess.run([str(NATIVE), "card", str(raw), str(out)], check=True, capture_output=True)
    assert rust.card_from_samples(y) == json.loads(out.read_text())


@needs_lib
def test_python_engine_steps_in_when_rust_cant(song, tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_ENGINE", "rust")
    part = analyze_file(song, ["core"])
    assert "engine" not in part.extras and part.extras["engine_fallback"] == "only some stages were asked for"
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"RIFF....not audio")
    with pytest.raises(DecodeError):
        analyze_file(bad)  # neither Rust nor Python can decode it
    monkeypatch.setenv("TASTR_ENGINE", "python")
    ref = analyze_file(song, ["core"])
    assert "engine" not in ref.extras and "engine_fallback" not in ref.extras


@needs_lib
def test_rust_is_the_default(song, monkeypatch):
    monkeypatch.delenv("TASTR_ENGINE", raising=False)
    card = analyze_file(song)
    assert card.extras["engine"] == f"rust {rust.version()}" and "decoded_by" not in card.extras


@needs_lib
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_formats_rust_cant_decode_are_decoded_here(song, tmp_path, monkeypatch):
    """Opus: Python decodes it and Rust still makes the card from the audio."""
    monkeypatch.setenv("TASTR_ENGINE", "rust")
    opus = tmp_path / "song.opus"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(song), "-c:a", "libopus", str(opus)],
                   check=True)
    card = analyze_file(opus)
    assert card.extras["engine"] == f"rust {rust.version()}" and card.extras["decoded_by"] == "python"
    assert abs(card.core.bpm - 124) < 1


def test_server_sends_the_engine_setting_to_modal(song, monkeypatch):
    """TASTR_ENGINE is set on the Fly server; it reaches Modal in the options."""
    from tastr import service
    from tastr.cloud import remote

    seen = {}

    def fake(path, stages, options, on_stage=None):
        seen["options"] = options
        return analyze_file(song, ["core"])

    monkeypatch.setenv("TASTR_ANALYSIS_BACKEND", "modal")
    monkeypatch.setenv("TASTR_ENGINE", "rust")
    monkeypatch.setattr(remote, "analyze", fake)
    service._analyze(Source(kind="file", uri=str(song), local_path=str(song)), None, None)
    assert seen["options"] == {"engine": "rust"}


# --- Browser cards ---


def wasm_card(path: Path, tmp_path: Path) -> dict:
    out = tmp_path / "w.json"
    subprocess.run(["node", str(ROOT / "run-wasm.mjs"), str(rust.wasm_path()), "card", str(path), str(out)],
                   check=True, capture_output=True)
    return json.loads(out.read_text())


def message(card: dict, engine: str | None = None) -> bytes:
    return json.dumps({"format": "tastr-card", "version": 1, "engine": engine or rust.wasm_id(),
                       "engine_version": "0.4.0", "card": card}).encode()


@needs_wasm
def test_browser_card_is_checked(song, tmp_path, monkeypatch):
    src = Source(kind="file", uri=str(song), local_path=str(song), title="song")
    card = wasm_card(song, tmp_path)
    monkeypatch.setenv("TASTR_BROWSER_ENGINE", "")
    with pytest.raises(browser_card.BrowserCardError, match="turned off"):
        browser_card.load(message(card), src)
    monkeypatch.setenv("TASTR_BROWSER_ENGINE", "1")
    got = browser_card.load(message(card), src)
    assert got.extras["browser_card"] is True and got.extras["engine"] == "rust 0.4.0"
    assert got.source.title == "song" and got.core.bpm == card["core"]["bpm"]
    bad = [
        (message(card, engine="0" * 16), "different engine build"),
        (message({**card, "stages_run": {"core": "0.2.1"}}), "stages"),
        (message({**card, "duration": card["duration"] + 30}), "lasts"),
        (message({**card, "sample_rate": 44100}), "Hz"),
        (message({**card, "core": {"bpm": 120, "nonsense": 1}}), "not a card"),
        (b'{"format": "tastr-card", "version": 2}', "version"),
        (b"{not json, tastr-card", "not JSON"),
    ]
    for data, why in bad:
        with pytest.raises(browser_card.BrowserCardError, match=why):
            browser_card.load(data, src)


def wait_job(client, job_id):
    for _ in range(600):
        job = client.get(f"/api/analyze/jobs/{job_id}").json()
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError("job didn't finish")


@needs_wasm
def test_upload_with_a_browser_card_skips_the_analysis(song, db_path, tmp_path, monkeypatch):
    from tastr import service
    from tastr.web.server import create_app

    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TASTR_LOOKUP", "")
    monkeypatch.setenv("TASTR_BROWSER_ENGINE", "1")
    analyzed = []
    real = service._analyze_audio
    monkeypatch.setattr(service, "_analyze_audio", lambda *a: analyzed.append(1) or real(*a))
    client = TestClient(create_app(db_path))
    cfg = client.get("/api/config").json()["browser_engine"]
    assert cfg["engine"] == rust.wasm_id()
    wasm = client.get(cfg["wasm"])
    assert wasm.status_code == 200 and wasm.headers["content-type"] == "application/wasm"
    card = wasm_card(song, tmp_path)
    r = client.post("/api/analyze?background=1", files=[("files", ("song.wav", song.read_bytes(), "audio/wav")),
                                                        ("card", ("card.json", message(card), "application/json"))])
    job = wait_job(client, r.json()["id"])
    assert job["state"] == "done", job
    stored = client.get(f"/api/tracks/{job['results'][0]['track_id']}").json()
    assert stored["extras"]["browser_card"] is True and stored["core"]["bpm"] == card["core"]["bpm"]
    assert not analyzed
    # A card from another build is set aside and the server analyzes the file itself.
    r = client.post("/api/analyze?background=1", files=[("files", ("song.wav", song.read_bytes(), "audio/wav")),
                                                        ("card", ("card.json", message(card, "f" * 16), "application/json"))])
    job = wait_job(client, r.json()["id"])
    stored = client.get(f"/api/tracks/{job['results'][0]['track_id']}").json()
    assert stored["extras"]["browser_card"].startswith("set aside") and analyzed


def test_browser_engine_off_by_default(db_path, tmp_path, monkeypatch):
    from tastr.web.server import create_app

    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TASTR_BROWSER_ENGINE", raising=False)
    client = TestClient(create_app(db_path))
    assert client.get("/api/config").json()["browser_engine"] is None
    assert client.get(f"/engine/{rust.WASM_NAME}").status_code == 404


@needs_lib
def test_bench_decodes_files_rust_cant(song, monkeypatch):
    """The bench runner falls back to the Python decoder like analyze_file (FMA Keys has MP3s Rust rejects)."""
    from tastr.engine.bench import runner
    from tastr.engine.bench.datasets import Item, Truth

    monkeypatch.setenv("TASTR_ENGINE", "rust")

    def refuse(path, beats_per_bar=None):
        raise rust.RustDecodeError("the Rust engine could not decode it")

    monkeypatch.setattr(rust, "card_from_file", refuse)
    card = runner.analyze_item(Item(id="song", truth=Truth(), path=str(song)))
    assert card.extras["decoded_by"] == "python" and abs(card.core.bpm - 124) < 1


@needs_lib
def test_decode_errors_carry_the_decoders_message(tmp_path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"not audio at all")
    with pytest.raises(rust.RustDecodeError, match="could not decode .*: .+"):
        rust.card_from_file(bad)
