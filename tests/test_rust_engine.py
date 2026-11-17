"""The Rust engine (engine-rs/, docs/rust-engine.md): its native and WebAssembly builds give identical output,
its HPSS, shared signals, beats, chroma and section clustering match librosa's (tests/librosa_ref.py), the whole
card is checked against stored ones (tests/golden/), and the benchmarks' entry points (engine-rs/src/bench.rs)
find what synthetic inputs hold. Skips unless both builds exist:

    cd engine-rs && cargo build --release && cargo build --release --lib --target wasm32-unknown-unknown
"""

import base64
import json
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import numpy as np
import pytest
import librosa_ref as ref
from synth import make_track

from tastr.engine import SR

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


def test_hpss_matches_librosa(tmp_path):
    rng = np.random.default_rng(1)
    for y in (make_track(bpm=124, bars_per_section=(4, 4))[0].astype(np.float32),
              (0.3 * rng.standard_normal(SR * 3 + 77)).astype(np.float32)):
        raw = tmp_path / "y.f32"
        y.astype("<f4").tofile(raw)  # raw audio at 22,050 Hz: the binary skips decoding
        ours = native(raw, tmp_path / "out")
        h, p = ref.hpss(y)
        assert np.array_equal(ours["y"], y)
        for got, want in ((ours["harmonic"], h), (ours["percussive"], p)):
            assert got.shape == want.shape
            assert np.abs(got - want).max() < 1e-5 * np.abs(want).max()


def test_shared_signals_match_librosa(tmp_path):
    rng = np.random.default_rng(2)
    y = make_track(bpm=124, bars_per_section=(4, 4))[0]
    y = (y + 1e-3 * rng.standard_normal(y.size)).astype(np.float32)
    raw = tmp_path / "y.f32"
    y.astype("<f4").tofile(raw)
    ours = native(raw, tmp_path / "out")
    for name, want in ref.signals(y).items():
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
    env = ref.onset_envelope(ref.hpss(y)[1])
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
    h, _ = ref.hpss(y)
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
    """engine-rs/src/features.rs holds the drum envelopes' filters (librosa_ref._envelopes) as numbers; they must
    still be what scipy designs."""
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


GOLDEN = Path(__file__).parent / "golden"


def golden_track() -> np.ndarray:
    return make_track(bpm=128, bars_per_section=(8, 16, 8))[0].astype(np.float32)


def golden_hits() -> dict:
    """Drum-model hits on golden_track's grid, as the server would hand them in (engine/adt.py)."""
    step, rng = 60 / 128 / 4, np.random.default_rng(3)
    at = lambda steps: np.array([(b * 16 + s) * step for b in range(32) for s in steps])  # noqa: E731
    times = {"kick": at([0, 8]), "snare": at([4, 12]), "tom": at([14]), "hat": at(range(0, 16, 2)),
             "cymbal": at([0]) + 0.004}
    return {k: (t, rng.uniform(0.5, 1.0, t.size).astype(np.float32)) for k, t in times.items()}


def check_golden(name: str, card: dict) -> None:
    """The card equals the stored one in tests/golden/ (its spectrogram PNG by hash). After a deliberate engine
    change, TASTR_UPDATE_GOLDEN=1 rewrites the file; review its diff like any other change."""
    import hashlib
    import os

    card = json.loads(json.dumps(card))
    spec = card["extras"]["views"]["spectrogram"]
    png_pixels(spec["png"])  # decodes
    spec["png"] = "sha256:" + hashlib.sha256(spec["png"].encode()).hexdigest()
    path = GOLDEN / f"{name}.json"
    if os.environ.get("TASTR_UPDATE_GOLDEN"):
        path.write_text(json.dumps(card, indent=1, sort_keys=True) + "\n")
    assert card == json.loads(path.read_text()), f"the card differs from {path}; see check_golden"


def test_card_matches_the_golden_card():
    """Every stage of the Rust card, checked against a stored card."""
    from tastr.engine import rust

    card = rust.card_from_samples(golden_track())
    assert card["errors"] == {} and card["stages_run"] == rust.STAGES
    check_golden("card-128bpm", card)


def test_drum_model_card_matches_the_golden_card(monkeypatch):
    """With the drum model's hits handed in, the model's kick, tom and hi-hats replace those lanes, a cymbal
    lane joins, and its pad and help come too."""
    from tastr.engine import rust

    card = rust.card_from_samples(golden_track(), None, golden_hits())
    assert card["extras"]["drum_model"] == "adtof" and "cymbal" in card["extras"]["suggestions.drum_grid"]
    groove = next(s for s in card["suggestions"] if s["title"] == "Detected groove")
    assert groove["data"]["pads"]["cymbal"] == "C#2" and "ADTOF" in groove["data"]["lane_help"]["kick"]
    check_golden("card-128bpm-drum-model", card)
    no_model = rust.card_from_samples(golden_track())
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


# --- The benchmark entry points (engine-rs/src/bench.rs) find what synthetic inputs hold ---------------------


def _beat_this_spectrogram(seed: int, frames: int = 1500) -> tuple[np.ndarray, np.ndarray]:
    """A 128-band log-mel spectrogram at 50 fps with a kick on every beat, a backbeat and eighth-note hats, and its
    beat frames."""
    rng = np.random.default_rng(seed)
    log_mel = (rng.random((128, frames)) * 0.3).astype(np.float32)
    per = 50 * 60 / rng.uniform(80, 175)
    beats = np.round(np.arange(rng.uniform(0, per), frames - 2, per)).astype(int)
    log_mel[:15, beats] += 3
    log_mel[40:80, beats[1::2]] += 2
    log_mel[100:, np.round(np.arange(per / 2, frames - 2, per / 2)).astype(int)] += 1
    return log_mel, beats


@pytest.mark.parametrize("seed", range(4))
def test_bench_downbeat_meter_and_tempo(seed):
    """A heavier kick on one beat in four (one without the backbeat) is bar one; the meter reads 4/4; the tempo is
    the spectrogram's, or a metrical neighbour of it (the eighth-note hats allow a 3:2 reading)."""
    from tastr.engine import rust
    from tastr.engine.bench import tempo_spec
    from tastr.engine.bench.downbeats import FPS

    log_mel, beats = _beat_this_spectrogram(seed)
    one = 2 * seed % 4
    log_mel[:20, beats[one::4]] += 2
    shape = dict(bands=128, frames=log_mel.shape[1], beats=beats.size)
    assert rust.bench("downbeat_phase", log_mel, beats, m=4, **shape)["phase"] == one
    m = rust.bench("meter", log_mel, beats, **shape)
    assert m["guess"] == 4 and m["score"] <= m["threshold"]
    onset, low_flux = tempo_spec.envelopes(log_mel)
    rs = rust.bench("tempo", onset, low_flux, log_mel, n=onset.size, bands=128, frames=log_mel.shape[1], fps=FPS)
    truth = 60 * FPS / np.diff(beats).mean()
    assert any(abs(rs["bpm"] - truth * k) <= 0.03 * truth * k for k in (0.5, 2 / 3, 1, 1.5, 2)), (rs["bpm"], truth)
    assert 0 <= rs["confidence"] <= 1


def test_bench_sections_find_the_parts():
    """Bar-aligned parts with their own timbre come back as section starts, for every config."""
    from itertools import product

    from tastr.engine.bench import sections_spec as ss

    rng = np.random.default_rng(2)
    info = {"sr": 22050.0, "hop": 1024, "fmin": 0.0, "fmax": 11025.0}
    configs = list(product(ss.SECONDS, ss.PHRASES, ss.CAPS))
    bar = 2.0
    frames = 5200
    duration = frames * info["hop"] / info["sr"]
    edges = [0, 16, 32, 48, 64]  # bars
    part = np.searchsorted([e * bar * info["sr"] / info["hop"] for e in edges[1:-1]], np.arange(frames), side="right")
    mel = ((rng.random((len(edges), 80)) * 10)[part].T + rng.random((80, frames))) ** 2
    sig = ss.signals_from_mel(mel, info)
    bar_times, starts, current = ss._starts(sig, list(np.arange(0, duration, bar)), duration, info, configs)
    assert current in configs and np.allclose(bar_times[:5], np.arange(5) * bar)
    for c, s in starts.items():
        for e in edges[1:-1]:
            assert min(abs(b - e) for b in s) <= 1, (c, s)  # starts are bar numbers


def test_bench_key_profile_is_the_chromas():
    from tastr.engine.bench import key_features
    from tastr.engine.bench.synth import make_track

    y, _ = make_track(bpm=118, tonic=9, mode="minor")
    rs = key_features.features(y, matrices=True)
    assert not rs["silent"] and rs["profile"].shape == (12,) and rs["profile"].sum() == pytest.approx(1, abs=1e-6)
    assert rs["chroma"].shape[0] == 12 and rs["bass"].shape[0] == 36
    assert np.allclose(key_features.profile_of(rs["chroma"]), rs["profile"], atol=1e-6)
    assert key_features.features(np.zeros(SR, np.float32))["silent"]


def test_bench_drum_lanes():
    from tastr.engine.bench import drums as bench_drums
    from tastr.engine.bench.synth import make_drum_track

    y, _ = make_drum_track({"kick": "x...x...x...x...", "hat": "..x...x...x...x."}, bpm=128)
    acts, hop = bench_drums._activations(y, "synth")
    assert hop == 512 and tuple(acts) == bench_drums.LANES
    assert all(a.size == 1 + y.size // hop for a in acts.values())
    beat = int(round(60 / 128 * SR / hop))
    assert acts["kick"][:beat * 8].argmax() % beat <= 1


def test_bench_reports_bad_calls():
    from tastr.engine import rust

    with pytest.raises(rust.RustEngineError, match="unknown op"):
        rust.bench("nope")
    with pytest.raises(rust.RustEngineError, match="cut short"):
        rust.bench("downbeat_phase", np.zeros(10), bands=128, frames=100, beats=0)
