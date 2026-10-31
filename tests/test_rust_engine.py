"""The Rust engine (engine-rs/, docs/rust-engine.md): its native and WebAssembly builds give identical output,
and its HPSS matches the Python engine's. Skips unless both builds exist:

    cd engine-rs && cargo build --release && cargo build --release --lib --target wasm32-unknown-unknown
"""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from synth import make_track
from tastr.engine import SR, signals
from tastr.engine.card import Source, TrackCard
from tastr.engine.pipeline import AnalysisContext

ROOT = Path(__file__).parent.parent / "engine-rs"
NATIVE = ROOT / "target" / "release" / "tastr-engine"
WASM = ROOT / "target" / "wasm32-unknown-unknown" / "release" / "tastr_engine.wasm"
pytestmark = pytest.mark.skipif(not (NATIVE.exists() and WASM.exists() and shutil.which("node")),
                                reason="needs the Rust engine built natively and for WebAssembly, and Node")

ARRAYS = ("y", "harmonic", "percussive")
# The C maths functions engine-rs/src/detmath.rs defines; the native build must not take any from the system.
MATHS = {"sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sinh", "cosh", "tanh", "exp", "exp2", "exp10",
         "expm1", "log", "log2", "log10", "log1p", "cbrt", "pow", "hypot", "fmod", "sincos"}


def run(cmd, out: Path) -> dict[str, np.ndarray]:
    subprocess.run(cmd + [str(out)], check=True, capture_output=True)
    return {a: np.fromfile(out / f"{a}.f32", dtype="<f4") for a in ARRAYS}


def native(path: Path, out: Path):
    return run([str(NATIVE), str(path)], out)


def wasm(path: Path, out: Path):
    return run(["node", str(ROOT / "run-wasm.mjs"), str(WASM), str(path)], out)


@pytest.fixture(scope="module")
def stereo_44k(tmp_path_factory):
    """A 44.1 kHz stereo track with a noise floor, as WAV, and as MP3/AAC/Vorbis/FLAC when ffmpeg is there."""
    import soundfile as sf
    from scipy.signal import resample_poly

    d = tmp_path_factory.mktemp("rust")
    y = resample_poly(make_track(bpm=124, bars_per_section=(4, 4))[0], 2, 1)
    rng = np.random.default_rng(0)
    st = np.stack([y, 0.9 * np.roll(y, 37)], 1) + 1e-3 * rng.standard_normal((len(y), 2))
    wav = d / "mix.wav"
    sf.write(wav, st.astype(np.float32), 44100, subtype="PCM_16")
    files = [wav]
    if shutil.which("ffmpeg"):
        for ext, codec in (("mp3", ["-b:a", "192k"]), ("m4a", ["-c:a", "aac"]), ("ogg", ["-c:a", "libvorbis"]),
                           ("flac", [])):
            out = d / f"mix.{ext}"
            if subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(wav), *codec, str(out)]).returncode == 0:
                files.append(out)
    return files


def test_native_build_takes_no_maths_from_the_system():
    """glibc's sin/cos/pow can differ from WebAssembly's in the last bit; detmath.rs replaces them everywhere."""
    if not shutil.which("nm"):
        pytest.skip("needs nm")
    syms = subprocess.run(["nm", "-D", "--undefined-only", str(NATIVE)], capture_output=True, text=True, check=True).stdout
    imported = {line.split()[-1].split("@")[0] for line in syms.splitlines() if line.strip()}
    assert not imported & (MATHS | {m + "f" for m in MATHS})


def test_native_and_wasm_give_identical_bits(stereo_44k, tmp_path):
    for path in stereo_44k:
        a, b = native(path, tmp_path / "n" / path.name), wasm(path, tmp_path / "w" / path.name)
        for name in ARRAYS:
            assert a[name].tobytes() == b[name].tobytes(), f"{path.name}: {name} differs"


def test_hpss_matches_the_python_engine(tmp_path):
    rng = np.random.default_rng(1)
    for y in (make_track(bpm=124, bars_per_section=(4, 4))[0].astype(np.float32),
              (0.3 * rng.standard_normal(SR * 3 + 77)).astype(np.float32)):
        raw = tmp_path / "y.f32"
        y.astype("<f4").tofile(raw)  # raw audio at 22,050 Hz: the binary skips decoding
        ours = native(raw, tmp_path / "out")
        h, p = signals.hpss(AnalysisContext(y=y, sr=SR, card=TrackCard(source=Source(kind="file", uri="x"))))
        assert np.array_equal(ours["y"], y)
        for got, want in ((ours["harmonic"], h), (ours["percussive"], p)):
            assert got.shape == want.shape
            assert np.abs(got - want).max() < 1e-5 * np.abs(want).max()


def test_decoding_and_resampling_match_librosa_below_10_khz(stereo_44k, tmp_path):
    """Our resampler is shaped like soxr's HQ filter but isn't soxr: the two agree closely except in the top
    kilohertz, which the analysis barely uses."""
    import librosa

    from tastr.engine.audio import load_audio

    wav = stereo_44k[0]
    ours, theirs = native(wav, tmp_path / "out")["y"], load_audio(wav, SR)
    assert ours.shape == theirs.shape
    err = np.abs(librosa.stft(ours - theirs)) ** 2
    sig = np.abs(librosa.stft(theirs)) ** 2
    low = librosa.fft_frequencies(sr=SR) < 10000
    assert 10 * np.log10(err[low].sum() / sig[low].sum()) < -90


def test_opus_is_left_to_the_server(tmp_path):
    """symphonia can't decode Opus; the binary says so instead of producing anything."""
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    import soundfile as sf

    wav, opus = tmp_path / "a.wav", tmp_path / "a.opus"
    sf.write(wav, np.zeros(SR, np.float32), SR)
    if subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(wav), "-c:a", "libopus", str(opus)]).returncode:
        pytest.skip("ffmpeg has no Opus encoder")
    r = subprocess.run([str(NATIVE), str(opus)], capture_output=True, text=True)
    assert r.returncode == 1 and "unsupported codec" in r.stderr


def test_binary_reports_a_fingerprint(tmp_path):
    raw = tmp_path / "y.f32"
    np.zeros(SR, "<f4").tofile(raw)
    out = json.loads(subprocess.run([str(NATIVE), str(raw)], capture_output=True, text=True, check=True).stdout)
    assert len(out["fingerprint"]) == 16 and out["seconds"] == 1.0
