import json

import numpy as np
import pytest

from tastr.engine.card import Core, Source, TrackCard
from tastr.ingest import load_audio
from tastr.engine.pipeline import SR, AnalysisContext, run_pipeline
from tastr.engine.stages.suggestions import SuggestionsStage

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


def test_detected_groove_by_section(synth_wav, truth_core):
    from tastr.engine.card import Section

    y = load_audio(str(synth_wav[0]), SR)
    base = run_pipeline(y, SR, TrackCard(source=Source(kind="file", uri="x")), stages=["suggestions"])
    bars = len(base.core.downbeat_times) - 1
    assert bars >= 4
    card = TrackCard(source=Source(kind="file", uri="x"), core=base.core)
    half = bars // 2
    card.sections = [
        Section(0.0, 1.0, "intro", "A", 1, half, 0.3),
        Section(1.0, 2.0, "drop", "B", half + 1, bars, 0.9),
    ]
    SuggestionsStage().run(AnalysisContext(y=y, sr=SR, card=card))
    groove = next(s for s in card.suggestions if s.title == "Detected groove")
    by = groove.data["by_section"]
    assert [s["idx"] for s in by] == [0, 1]
    assert [s["label"] for s in by] == ["intro", "drop"]
    for s in by:
        assert set(s["pattern"]) == set(groove.data["pattern"])
        assert all(len(p) == 16 for p in s["pattern"].values())
    assert by[0]["pattern"]["kick"][0] == "x"
    assert all(groove.data["pattern"][k] for k in ("kick", "hat"))
    # Section labels stay lowercase in data but are capitalized in the arrangement text.
    arr = next(s for s in card.suggestions if s.title.startswith("Arrangement map"))
    assert "Intro A (bars" in arr.body and "Drop B (bars" in arr.body
    assert [x["name"] for x in arr.data["locators"]] == ["Intro A", "Drop B"]
    json.dumps(groove.data)


def test_daw_specific_text_is_templated(card):
    templated = {s.title.split(":")[0]: s.data["template"] for s in card.suggestions if "template" in s.data}
    assert "Set the project to" in " ".join(s.title for s in card.suggestions if "template" in s.data and s.category == "setup")
    assert all("{" in t for t in templated.values())
    for s in card.suggestions:
        if "template" in s.data:
            assert s.body  # rendered Ableton wording stays available to non-UI consumers
