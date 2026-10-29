import time

import numpy as np
import pytest
from synth import SR, make_track

from tastr.card import Source, TrackCard
from tastr.pipeline import run_pipeline
from tastr.stages.core import _swing

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


@pytest.mark.parametrize("tonic,mode,allowed", [(9, "minor", {(9, "minor"), (0, "major")}),
                                                (7, "major", {(7, "major"), (4, "minor")})])
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
