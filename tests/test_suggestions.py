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


def test_template_suggestions_carry_bpm_and_style_key(card):
    templates = [s for s in card.suggestions if s.data.get("origin") == "template"]
    assert templates
    for s in templates:
        assert s.data["style_key"] in {"four_floor", "dnb", "halftime", "hiphop", "generic"}
        assert s.data["bpm"] == round(card.core.bpm)


def test_detected_groove_carries_bpm(card):
    groove = next(s for s in card.suggestions if s.data.get("origin") == "detected")
    assert groove.data["bpm"] == round(card.core.bpm)


@pytest.fixture(scope="module")
def sectioned_card():
    """Three 8-bar sections with their own chord loops; the middle one (with drums) has no bass line."""
    from synth import TruthCoreStage, make_track

    from tastr.engine.card import Section
    from tastr.engine.pipeline import register_stage, registered_stages

    y, truth = make_track(bpm=124, tonic=9, mode="minor", bars_per_section=(8, 8, 8),
                          section_progressions=[[(0, 3), (8, 4), (3, 4), (10, 4)],  # Am F C G
                                                [(5, 3), (5, 3), (0, 3), (7, 4)],  # Dm Dm Am E
                                                [(3, 4), (10, 4)]],  # C G
                          bass_sections=(True, False, True))
    original = registered_stages()["core"]
    register_stage(TruthCoreStage(truth), replace=True)
    try:
        base = run_pipeline(y, SR, TrackCard(source=Source(kind="file", uri="x")), stages=["suggestions"])
    finally:
        register_stage(original, replace=True)
    card = TrackCard(source=Source(kind="file", uri="x"), core=base.core)
    card.sections = [Section(0.0, 1.0, "intro", "A", 1, 8, 0.3), Section(1.0, 2.0, "drop", "B", 9, 16, 0.9),
                     Section(2.0, 3.0, "outro", "C", 17, 24, 0.3)]
    SuggestionsStage().run(AnalysisContext(y=y, sr=SR, card=card))
    return card


def _by_section(card, category):
    sug = next(s for s in card.suggestions if s.category == category and "by_section" in s.data)
    json.dumps(sug.data)
    return sug.data["by_section"]


def test_chord_loop_by_section(sectioned_card):
    by = _by_section(sectioned_card, "harmony")
    assert [(s["idx"], s["label"], s["letter"], s["start_bar"], s["end_bar"]) for s in by] == [
        (0, "intro", "A", 1, 8), (1, "drop", "B", 9, 16), (2, "outro", "C", 17, 24)]
    assert [s["progression"] for s in by] == [["Am", "F", "C", "G"], ["Dm", "Dm", "Am", "E"], ["C", "G", "C", "G"]]
    assert by[1]["roman"] == ["iv", "iv", "i", "V"]


def test_bass_loop_by_section_with_a_bassless_section(sectioned_card):
    by = _by_section(sectioned_card, "bass")
    assert [s["loop"] for s in by] == [["A", "F", "C", "G"], [], ["C", "G", "C", "G"]]
    assert by[1]["rhythm"] is None and by[1]["degrees"] == []
    for s in (by[0], by[2]):
        assert len(s["rhythm"]) == 16 and s["rhythm"][0] == "x"
    # Per-bar reads stay aligned to bars: the bass-less bars are None, not dropped.
    notes = sectioned_card.extras["suggestions.bass"]["bar_notes"]
    assert notes[8:16] == [None] * 8 and notes[16:18] == ["C", "G"]
    assert len(sectioned_card.extras["suggestions.harmony"]["bar_chords"]) == len(notes)
    assert not any(k.startswith("_") for k in sectioned_card.extras["suggestions.bass"])


def test_section_loop_skips_unknown_bars():
    from tastr.engine.stages.suggestions import _section_loop

    assert _section_loop([None] * 6) == []
    assert _section_loop([None, None, None, None, "Am", None, "C", "G"]) == ["Am", None, "C", "G"]
    assert _section_loop(["Am", "F"]) == ["Am", "F"]
