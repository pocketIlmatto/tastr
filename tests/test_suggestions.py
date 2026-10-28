import json

import numpy as np
import pytest

from tastr.card import Core, Source, TrackCard
from tastr.ingest import load_audio
from tastr.pipeline import SR, AnalysisContext, run_pipeline
from tastr.stages.suggestions import SuggestionsStage

CATEGORIES = {"setup", "drums", "bass", "harmony", "arrangement", "sound"}


@pytest.fixture
def card(synth_wav, truth_core):
    y = load_audio(str(synth_wav[0]), SR)
    return run_pipeline(y, SR, TrackCard(source=Source(kind="file", uri="x")), stages=["suggestions"])


def test_drum_grid_matches_synth(card):
    assert "suggestions" not in card.errors, card.errors
    grid = card.extras["suggestions.drum_grid"]
    assert grid["kick"]["detected"] and grid["hat"]["detected"]
    assert all(grid["kick"]["pattern"][i] == "x" for i in (0, 4, 8, 12))
    assert all(grid["hat"]["pattern"][i] == "x" for i in (2, 6, 10, 14))
    assert len(grid["kick"]["velocity"]) == 16


def test_harmony_and_bass_match_synth(card):
    prog = card.extras["suggestions.harmony"]["progression"]
    assert len(set(prog) & {"Am", "F", "C", "G"}) >= 3
    assert set(card.extras["suggestions.bass"]["loop"]) <= {"A", "F", "C", "G"}


def test_suggestions_well_formed(card):
    assert card.suggestions
    for s in card.suggestions:
        assert s.category in CATEGORIES
        assert s.title and len(s.title) <= 80 and s.body
        json.dumps(s.data)
        for pat in (s.data.get("pattern") or {}).values():
            assert len(pat) == 16 and set(pat) <= set("xo.")
    assert {"setup", "drums", "bass", "harmony"} <= {s.category for s in card.suggestions}


def test_empty_core_does_not_raise():
    card = TrackCard(source=Source(kind="file", uri="x"), core=Core())
    ctx = AnalysisContext(y=np.zeros(SR, dtype=np.float32), sr=SR, card=card)
    SuggestionsStage().run(ctx)
    assert isinstance(card.suggestions, list)
