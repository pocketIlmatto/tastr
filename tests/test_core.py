import time

import numpy as np
import pytest
from synth import SR, make_track

from tastr.engine.card import Source, TrackCard
from tastr.engine.pipeline import run_pipeline
from tastr.engine.stages.core import _swing

SHORT = (4, 8, 4)  # 16 bars is plenty for tempo and key

RANGES = {
    "brightness": (0, 1),
    "percussiveness": (0, 1),
    "low_end": (0, 1),
    "pulse_clarity": (0, 1),
    "danceability": (0, 1),
    "onset_rate": (0, 50),
    "dynamic_range_db": (0, 120),
    "swing_percent": (50, 75),
    "swing_confidence": (0, 1),
}


pytestmark = pytest.mark.audio


def analyze(y, sr=SR):
    card = TrackCard(source=Source(kind="file", uri="synthetic"))
    return run_pipeline(y, sr, card, stages=["core"])


@pytest.fixture(scope="module")
def default_card():
    """The default synth track analyzed once, shared by the tests that only inspect the result."""
    y, truth = make_track(bpm=124)
    return analyze(y), truth


def near_multiple(value, truth, tol=1.0):
    return any(abs(value - truth * m) <= tol * m for m in (1, 0.5, 2))


@pytest.mark.parametrize("bpm", [100, 124, 140])
def test_bpm(bpm):
    y, truth = make_track(bpm=bpm, bars_per_section=SHORT)
    card = analyze(y)
    assert not card.errors
    assert abs(card.core.bpm - bpm) <= 1.0 or near_multiple(card.core.bpm, bpm)
    assert 0 <= card.core.bpm_confidence <= 1


@pytest.mark.parametrize("tonic,mode,allowed", [
    (9, "minor", {(9, "minor"), (0, "major")}),
    pytest.param(7, "major", {(7, "major"), (4, "minor")}, marks=pytest.mark.xfail(strict=True, reason=(
        "the synth's loud bass root makes its I-V-vi-IV progression ambiguous with the IV key (C major), and the "
        "learned key model picks IV; see docs/bench.md 'Key detection notes', known weakness"))),
])
def test_key(tonic, mode, allowed):
    y, _ = make_track(bpm=124, tonic=tonic, mode=mode, bars_per_section=SHORT)
    key = analyze(y).core.key
    assert (key.tonic, key.mode) in allowed
    assert key.camelot and key.scale and 0 <= key.confidence <= 1


def test_beat_grid(default_card):
    card, _ = default_card
    beats, downs = card.core.beat_times, card.core.downbeat_times
    assert len(downs) * 4 <= len(beats) + 4
    assert beats == sorted(beats) and downs == sorted(downs)
    assert all(0 <= t <= card.duration for t in beats + downs)
    assert set(downs) <= set(beats)


def test_energy_series_follows_arrangement(default_card):
    card, truth = default_card
    s = card.series["energy"]
    assert len(s.values) == len(card.core.downbeat_times) == len(s.times)
    assert all(0 <= v <= 1 for v in s.values)
    bar = truth["bar_seconds"]
    t = np.array(s.times)
    v = np.array(s.values)
    intro = v[t < 8 * bar - 0.1].mean()
    drums = v[(t >= 8 * bar + 0.1) & (t < 24 * bar - 0.1)].mean()
    assert drums > intro
    assert 0 <= card.core.energy <= 1


def test_registry_features_present_and_in_range(default_card):
    feats = default_card[0].features
    for name, (lo, hi) in RANGES.items():
        assert name in feats, name
        assert np.isfinite(feats[name]) and lo <= feats[name] <= hi, (name, feats[name])


@pytest.mark.parametrize("kind", ["silence", "noise"])
def test_degenerate_audio_does_not_raise(kind):
    n = 4 * SR
    y = np.zeros(n, dtype=np.float32) if kind == "silence" else (
        np.random.default_rng(1).standard_normal(n).astype(np.float32) * 0.1)
    card = analyze(y)
    assert "core" not in card.errors
    assert card.core.loudness_db is not None
    if kind == "silence":
        assert card.core.bpm is None and card.core.key is None


def test_swing_measures_offbeat_position():
    beat = 24  # frames per beat
    beats = np.arange(0, beat * 40, beat)
    for sixteenth_pos, expected in ((6, 50.0), (8, 66.7)):  # 25% vs 33% of the beat
        env = np.zeros(beat * 41)
        env[beats] = 1.0
        env[beats + sixteenth_pos] = 0.8
        percent, conf = _swing(env, beats)
        assert abs(percent - expected) <= 2.0
        assert conf > 0.8


@pytest.mark.perf
def test_three_minute_track_under_30s():
    y, _ = make_track(bpm=124, bars_per_section=(12, 32, 16, 12, 16, 12))  # ~3.2 min at 124 BPM
    assert y.size / SR >= 180
    analyze(make_track(bpm=124)[0])  # warm up numba
    t0 = time.perf_counter()
    analyze(y)
    assert time.perf_counter() - t0 < 30


def test_key_profile_ignores_frame_loudness():
    import numpy as np

    from tastr.engine import music

    quiet = np.zeros((12, 10)); quiet[0] = 1.0
    loud = np.zeros((12, 10)); loud[7] = 100.0
    profile = music.key_profile(np.concatenate([quiet, loud], axis=1))
    assert profile[0] == pytest.approx(profile[7])


@pytest.mark.parametrize("mode", ["major", "minor"])
def test_estimate_key_follows_transposition(mode):
    import numpy as np

    from tastr.engine import music

    # A scale profile with a strong tonic, fifth and third: transposing it must transpose the key.
    third = 4 if mode == "major" else 3
    scale = [0, 2, 4, 5, 7, 9, 11] if mode == "major" else [0, 2, 3, 5, 7, 8, 10]
    base = np.full(12, 0.02)
    base[scale] = 0.10
    base[[0, 7, third]] = [0.20, 0.16, 0.15]
    base /= base.sum()
    for tonic in range(12):
        est = music.estimate_key(np.roll(base, tonic))
        assert (est["tonic"], est["mode"]) == (tonic, mode)
        assert 0 <= est["confidence"] <= est["probability"] <= 1
        assert len(est["alternatives"]) == 3


def test_key_probabilities_are_a_distribution_over_24_keys():
    import numpy as np

    from tastr.engine import music

    p = music.key_probabilities(np.random.default_rng(0).random(12) / 6)
    assert p.shape == (24,) and p.min() >= 0 and p.sum() == pytest.approx(1.0)


def test_downbeats_land_on_bar_starts(default_card):
    card, truth = default_card
    bar = truth["bar_seconds"]
    off = (np.array(card.core.downbeat_times) / bar) % 1.0
    assert np.mean(np.minimum(off, 1 - off) < 0.06) > 0.8


FPB = 20  # spectrogram frames per beat in the synthetic bars below


def _bars(meter: int, start: int, n_beats: int = 64):
    """Log-mel of `n_beats` beats in `meter`: kick on every beat, snare on the backbeats (2 and 4 in 4/4,
    2 and 3 in 3/4), a new chord each bar; the clip starts on beat `start + 1`."""
    freqs = np.geomspace(30, 11000, 128)
    log_mel = np.full((128, FPB * n_beats), 0.1)
    rng = np.random.default_rng(0)
    chords = rng.random((n_beats // meter + 2, 128)) * (freqs > 200) * (freqs < 2000)
    for i in range(n_beats):
        pos = (start + i) % meter  # 0 = beat 1
        bar = (start + i) // meter
        seg = slice(i * FPB, (i + 1) * FPB)
        log_mel[:, seg] += chords[bar][:, None]
        log_mel[freqs < 120, i * FPB:i * FPB + 3] += 3.0  # kick
        if pos in ((1, 3) if meter == 4 else (1, 2)):
            log_mel[(freqs > 300) & (freqs < 2500), i * FPB:i * FPB + 3] += 2.0  # snare
    return np.arange(n_beats) * FPB, log_mel, freqs


@pytest.mark.parametrize("start", [0, 1, 2, 3])
def test_downbeat_phase_finds_bar_one(start):
    from tastr.engine.stages.core import _downbeat_phase

    assert _downbeat_phase(*_bars(4, start)) == (4 - start) % 4


@pytest.mark.parametrize("start", [0, 1, 2])
def test_downbeat_phase_finds_bar_one_in_three(start):
    from tastr.engine.stages.core import _downbeat_phase

    assert _downbeat_phase(*_bars(3, start), beats_per_bar=3) == (3 - start) % 3


@pytest.mark.parametrize("meter", [3, 4])
def test_meter_score_tells_three_from_four(meter):
    from tastr.engine.stages.core import METER_THRESHOLD, _meter_score

    assert (_meter_score(*_bars(meter, 1)) > METER_THRESHOLD) == (meter == 3)


def test_core_records_a_meter_guess(default_card):
    card, _ = default_card
    assert card.extras["core.meter_guess"]["beats_per_bar"] == 4  # synthetic tracks are 4/4
