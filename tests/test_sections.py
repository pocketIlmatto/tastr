"""Sections of the Rust card (engine-rs/src/stages/sections.rs) on synthetic tracks with known parts."""

import pytest
from synth import SR, make_track

from tastr.engine import rust
from tastr.engine.card import TrackCard

pytestmark = pytest.mark.rust


def _card(y):
    return TrackCard.from_dict(rust.card_from_samples(y) | {"source": {"kind": "file", "uri": "synth"}})


def _check_valid(card, min_bars=4):
    secs = card.sections
    assert secs and secs[0].start == 0.0
    assert secs[-1].end == pytest.approx(card.duration)
    for a, b in zip(secs, secs[1:]):
        assert a.end == b.start
    for s in secs:
        assert s.end > s.start
        assert s.label in {"intro", "verse", "chorus", "build", "drop", "breakdown", "outro", "full"}
        assert s.letter and 0 <= s.energy <= 1
        if min_bars:
            assert s.end_bar - s.start_bar + 1 >= min_bars


def test_synth_boundaries_energy_and_labels():
    """8 bars of intro, 16 with drums, 8 of outro: the boundaries land within a bar, the drums are the loudest."""
    y, truth = make_track(bpm=124, bars_per_section=(8, 16, 8))
    card = _card(y)
    assert "sections" not in card.errors
    _check_valid(card)
    bar = truth["bar_seconds"]
    bounds = [s.start for s in card.sections[1:]]
    for target in (8 * bar, 24 * bar):
        assert min(abs(b - target) for b in bounds) <= bar
    intro, outro = card.sections[0], card.sections[-1]
    drum = max(card.sections, key=lambda s: s.energy)
    assert intro.label == "intro"
    assert drum.label in {"drop", "chorus"}
    assert drum.energy > intro.energy and drum.energy > outro.energy
    assert card.features["section_count"] == len(card.sections)
    assert card.features["section_contrast"] > 0.1


def test_short_track_is_one_full_section():
    y, _ = make_track(bpm=124, bars_per_section=(2, 2, 2))
    card = _card(y)
    assert [s.label for s in card.sections] == ["full"]
    assert card.sections[0].end == pytest.approx(card.duration)


def test_beatless_audio_still_gets_sections():
    """No downbeats: the track is split into blocks, still covering all of it."""
    import numpy as np

    t = np.arange(SR * 60) / SR
    card = _card((0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32))
    _check_valid(card, min_bars=0)
