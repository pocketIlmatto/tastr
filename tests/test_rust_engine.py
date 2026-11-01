"""The Rust engine (engine-rs/, docs/rust-engine.md): its native and WebAssembly builds give identical output,
its HPSS, shared signals, beats, chroma and section clustering match the Python engine's, and so do its cards. Skips unless both builds exist:

    cd engine-rs && cargo build --release && cargo build --release --lib --target wasm32-unknown-unknown
"""

import base64
import json
import re
import shutil
import struct
import subprocess
import zlib
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

# The C maths functions engine-rs/src/detmath.rs defines; the native build must not take any from the system.
MATHS = {"sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sinh", "cosh", "tanh", "exp", "exp2", "exp10",
         "expm1", "log", "log2", "log10", "log1p", "cbrt", "pow", "hypot", "fmod", "sincos"}


def run(cmd, out: Path) -> dict[str, np.ndarray]:
    """Every output the build writes, by name, shaped (rows, cols) or flat for one row."""
    subprocess.run(cmd + [str(out)], check=True, capture_output=True)
    shapes = json.loads((out / "manifest.json").read_text())
    arrays = {name: np.fromfile(out / f"{name}.f32", dtype="<f4").reshape(rows, cols)
              for name, (rows, cols) in shapes.items()}
    return {name: a[0] if a.shape[0] == 1 else a for name, a in arrays.items()}


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
        assert a.keys() == b.keys() and len(a) > 3
        for name in a:
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


def python_signals(y: np.ndarray) -> dict[str, np.ndarray]:
    import librosa

    from tastr.engine import drums

    ctx = AnalysisContext(y=y, sr=SR, card=TrackCard(source=Source(kind="file", uri="x")))
    env = drums._envelopes(ctx)
    return {"onset_envelope": signals.onset_envelope(ctx), "rms": signals.rms(ctx), "mfcc": signals.mfcc(ctx),
            "centroid": librosa.feature.spectral_centroid(S=np.abs(librosa.stft(y)), sr=SR)[0],
            "mel_percussive": signals.mel_percussive(ctx)[0], "low_band_flux": signals.low_band_flux(ctx),
            "air_flux": drums._air_flux(ctx), "log_mel": signals.log_mel(ctx)[0],
            "envelope_mid": env["mid"], "envelope_air": env["air"]}


def test_shared_signals_match_the_python_engine(tmp_path):
    rng = np.random.default_rng(2)
    y = make_track(bpm=124, bars_per_section=(4, 4))[0]
    y = (y + 1e-3 * rng.standard_normal(y.size)).astype(np.float32)
    raw = tmp_path / "y.f32"
    y.astype("<f4").tofile(raw)
    ours = native(raw, tmp_path / "out")
    for name, want in python_signals(y).items():
        got = ours[name]
        assert got.shape == want.shape, name
        err = np.abs(got - want).max()
        if name.startswith("envelope_"):  # dB of the 4 ms envelope; tiny HPSS differences show near silence
            assert err < 0.01, name
        else:  # float32 throughout, as librosa; only the summation order and maths library differ
            assert err <= 2e-6 * np.abs(want).max(), name


def noisy_track(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    y = make_track(bpm=124, bars_per_section=(4, 4))[0]
    return (y + 1e-3 * rng.standard_normal(y.size)).astype(np.float32)


def test_tempo_beats_and_onsets_match_librosa(tmp_path):
    """Given the same onset envelope, the same tempo, beats and onsets as librosa, and the same tempogram."""
    import librosa

    y = noisy_track(3)
    env = signals.onset_envelope(AnalysisContext(y=y, sr=SR, card=TrackCard(source=Source(kind="file", uri="x"))))
    tempo, beats = librosa.beat.beat_track(onset_envelope=env, sr=SR, hop_length=512)
    tempo = float(np.atleast_1d(tempo)[0])
    _, faster = librosa.beat.beat_track(onset_envelope=env, sr=SR, hop_length=512, bpm=tempo * 1.5)
    raw = tmp_path / "env.f32"
    env.astype("<f4").tofile(raw)
    ours = run([str(NATIVE), "rhythm", str(raw), repr(tempo * 1.5)], tmp_path / "out")
    tg = librosa.feature.tempogram(onset_envelope=env, sr=SR, hop_length=512, win_length=384).mean(axis=1)
    assert np.abs(ours["tempogram_mean"] - tg).max() < 1e-6
    assert ours["tempo"][0] == np.float32(tempo)
    assert np.array_equal(ours["beats"], beats) and np.array_equal(ours["beats_at_bpm"], faster)
    assert np.array_equal(ours["onsets"], librosa.onset.onset_detect(onset_envelope=env, sr=SR, hop_length=512))


def test_chroma_and_bass_cqt_match_librosa(tmp_path):
    """Same tuning estimate; chroma and the bass CQT within float32 noise plus the resampler's small differences
    (each octave below the top one is computed from audio halved by our resampler instead of soxr)."""
    import librosa

    y = noisy_track(4)
    h, _ = signals.hpss(AnalysisContext(y=y, sr=SR, card=TrackCard(source=Source(kind="file", uri="x"))))
    raw = tmp_path / "h.f32"
    h.astype("<f4").tofile(raw)
    ours = run([str(NATIVE), "chroma", str(raw)], tmp_path / "out")
    assert ours["tuning"][0] == np.float32(librosa.estimate_tuning(y=h, sr=SR, bins_per_octave=36))
    chroma = librosa.feature.chroma_cqt(y=h, sr=SR, hop_length=512)
    assert ours["chroma"].shape == chroma.shape and np.abs(ours["chroma"] - chroma).max() < 1e-3
    bass = np.abs(librosa.cqt(h, sr=SR, hop_length=512, fmin=librosa.note_to_hz("C1"), n_bins=36, bins_per_octave=12))
    assert ours["bass_cqt"].shape == bass.shape and np.abs(ours["bass_cqt"] - bass).max() < 2e-3 * bass.max()


def test_ward_boundaries_match_librosa(tmp_path):
    """librosa.segment.agglomerative on bar-like features: sections plus noise, and exact repeats (ties)."""
    import librosa

    rng = np.random.default_rng(5)
    for _ in range(40):
        n, f = int(rng.integers(4, 120)), int(rng.integers(1, 26))
        k = int(rng.integers(1, min(n, 16) + 1))
        z = np.repeat(rng.normal(size=(f, n // 8 + 1)), 8, axis=1)[:, :n]
        z = (z + rng.normal(scale=rng.choice([0.0, 0.1, 1.0]), size=(f, n))).astype(np.float32)
        z.astype("<f4").tofile(tmp_path / "z.f32")
        subprocess.run([str(NATIVE), "ward", str(tmp_path / "z.f32"), str(f), str(k), str(tmp_path / "out")], check=True)
        ours = np.fromfile(tmp_path / "out" / "boundaries.f32", "<f4")
        assert np.array_equal(ours, librosa.segment.agglomerative(z.astype(np.float64), k))


def test_butterworth_tables_are_scipys():
    """engine-rs/src/features.rs holds drums._envelopes' filters as numbers; they must still be what scipy designs."""
    import re

    from scipy import signal

    src = (ROOT / "src" / "features.rs").read_text()

    def table(name):
        body = re.search(rf"const {name}: [^=]+= \[(.*?)\];", src, re.S).group(1)
        return np.array([[float(v) for v in row.split(",") if v.strip()] for row in re.findall(r"\[([^\[\]]+)\]", body)])

    nyq = SR / 2
    mid = signal.butter(4, [800, min(3000, nyq * 0.9)], btype="bandpass", fs=SR, output="sos")
    air = signal.butter(4, min(6000, nyq * 0.8), btype="highpass", fs=SR, output="sos")
    for name, sos in (("MID", mid), ("AIR", air)):
        assert np.array_equal(table(f"{name}_SOS"), sos)
        assert np.array_equal(table(f"{name}_ZI"), signal.sosfilt_zi(sos))


def test_aac_priming_is_trimmed(stereo_44k, tmp_path):
    """The M4A edit list says how much encoder priming to skip; the decoded audio lines up with the WAV it came from."""
    m4a = [p for p in stereo_44k if p.suffix == ".m4a"]
    if not m4a:
        pytest.skip("no AAC encoder")
    wav = native(stereo_44k[0], tmp_path / "wav")["y"]
    aac = native(m4a[0], tmp_path / "aac")["y"]
    assert abs(len(aac) - len(wav)) < 30
    n = min(len(wav), len(aac)) - 200
    seg = slice(2000, n)
    lags = range(-8, 9)
    best = max(lags, key=lambda k: float(np.dot(wav[seg], aac[seg.start + k:seg.stop + k])))
    assert best == 0


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


def test_constants_match_the_python_engine():
    """engine-rs/src/consts.rs is copied from the key model and the calibration tables; refits must update it."""
    from tastr.engine import calibration_table, key_model

    src = (ROOT / "src" / "consts.rs").read_text()

    def const(name):
        body = re.search(rf"pub const {name}: [^=]+=\s*(.*?);\n", src, re.DOTALL).group(1)
        return np.array([float(x) for x in re.findall(r"-?\d+\.?\d*(?:e-?\d+)?", body)])

    assert np.array_equal(const("KEY_WEIGHTS"), np.asarray(key_model.WEIGHTS).ravel())
    assert np.array_equal(const("KEY_BIAS"), np.asarray(key_model.BIAS).ravel())
    assert np.array_equal(const("BPM_KNOTS"), np.array([k[:2] for k in calibration_table.BPM]).ravel())
    assert np.array_equal(const("KEY_KNOTS"), np.array([k[:2] for k in calibration_table.KEY]).ravel())


def png_pixels(url: str) -> np.ndarray:
    raw = base64.b64decode(url.split(",", 1)[1])
    pos, idat = 8, b""
    while pos < len(raw):
        n = struct.unpack(">I", raw[pos:pos + 4])[0]
        if raw[pos + 4:pos + 8] == b"IDAT":
            idat += raw[pos + 8:pos + 8 + n]
        pos += 12 + n
    return np.frombuffer(zlib.decompress(idat), np.uint8)


def rust_card(path: Path, out: Path, *extra: str) -> dict:
    subprocess.run([str(NATIVE), "card", str(path), str(out), *extra], check=True, capture_output=True)
    return json.loads(out.read_text())


def test_cards_match_the_python_engine(tmp_path):
    """The same audio through both engines gives the same card. The spectrogram PNG is deflated differently, so
    its pixels are compared, and an FFT rounding difference may move a pixel or two by one level."""
    from tastr.engine.pipeline import run_pipeline

    y = make_track(bpm=128, bars_per_section=(8, 16, 8))[0].astype(np.float32)
    raw = tmp_path / "y.f32"
    y.tofile(raw)
    py = json.loads(json.dumps(run_pipeline(y, SR, TrackCard(source=Source(kind="file", uri="x"))).to_dict()))
    rs = rust_card(raw, tmp_path / "card.json")
    assert rs["stages_run"] == py["stages_run"] and rs["errors"] == py["errors"] == {}
    pa = png_pixels(py["extras"]["views"]["spectrogram"].pop("png"))
    pb = png_pixels(rs["extras"]["views"]["spectrogram"].pop("png"))
    assert pa.size == pb.size and np.count_nonzero(pa != pb) <= pa.size // 10000
    py.pop("source"), rs.pop("source")
    assert rs == py


def test_drum_model_cards_match_the_python_engine(tmp_path, monkeypatch):
    """With the drum model's hits handed in (the server runs the model), the Rust card is the Python engine's:
    the model's kick, tom and hi-hats replace those lanes, a cymbal lane joins, and its pad and help come too."""
    from tastr.engine import adt, rust
    from tastr.engine.pipeline import run_pipeline

    y, _ = make_track(bpm=128, bars_per_section=(8, 16, 8))
    y = y.astype(np.float32)
    step, rng = 60 / 128 / 4, np.random.default_rng(3)
    at = lambda steps: np.array([(b * 16 + s) * step for b in range(32) for s in steps])  # noqa: E731
    times = {"kick": at([0, 8]), "snare": at([4, 12]), "tom": at([14]), "hat": at(range(0, 16, 2)),
             "cymbal": at([0]) + 0.004}
    hits = {k: (t, rng.uniform(0.5, 1.0, t.size).astype(np.float32)) for k, t in times.items()}
    monkeypatch.setattr(adt, "wanted", lambda options: True)
    monkeypatch.setattr(adt, "onsets", lambda y, sr: hits)
    py = json.loads(json.dumps(run_pipeline(y, SR, TrackCard(source=Source(kind="file", uri="x"))).to_dict()))
    rs = rust.card_from_samples(y, None, hits)
    assert py["extras"]["drum_model"] == rs["extras"]["drum_model"] == "adtof"
    assert "cymbal" in rs["extras"]["suggestions.drum_grid"]
    groove = next(s for s in rs["suggestions"] if s["title"] == "Detected groove")
    assert groove["data"]["pads"]["cymbal"] == "C#2" and "ADTOF" in groove["data"]["lane_help"]["kick"]
    for card in (py, rs):
        card["extras"]["views"]["spectrogram"].pop("png")
        card.pop("source")
    assert rs == py
    no_model = rust.card_from_samples(y)
    assert "drum_model" not in no_model["extras"] and "cymbal" not in no_model["extras"]["suggestions.drum_grid"]


def test_card_meter_option_and_wasm_card(stereo_44k, tmp_path):
    """The WebAssembly build's card is byte for byte the native one, and a given meter is used as is."""
    wav = stereo_44k[0]
    subprocess.run([str(NATIVE), "card", str(wav), str(tmp_path / "n.json")], check=True, capture_output=True)
    subprocess.run(["node", str(ROOT / "run-wasm.mjs"), str(WASM), "card", str(wav), str(tmp_path / "w.json")],
                   check=True, capture_output=True)
    assert (tmp_path / "n.json").read_bytes() == (tmp_path / "w.json").read_bytes()
    three = rust_card(wav, tmp_path / "3.json", "3")["core"]
    assert three["beats_per_bar"] == 3 and three["meter_set"]
