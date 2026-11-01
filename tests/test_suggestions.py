"""The suggestions on Rust cards (engine-rs/src/stages/suggestions.rs) for synthetic tracks with known drums,
bass and chords. The engine runs on the audio alone, as in the app."""

import json

import pytest
from synth import make_track

from tastr.engine import rust

pytestmark = [pytest.mark.audio, pytest.mark.skipif(not rust.available(), reason="needs the Rust engine built")]

CATEGORIES = {"setup", "drums", "bass", "harmony", "arrangement", "sound"}


def card_for(meter=None, **kw):
    y, truth = make_track(**kw)
    return rust.card_from_samples(y.astype("float32"), meter), truth


@pytest.fixture(scope="module")
def card():
    return card_for(bpm=124, tonic=9, mode="minor")[0]


def test_drum_grid_matches_synth(card):
    assert not card["errors"]
    grid = card["extras"]["suggestions.drum_grid"]
    assert grid["kick"]["detected"] and grid["hat"]["detected"]
    assert all(grid["kick"]["pattern"][i] == "x" for i in (0, 4, 8, 12))
    assert all(grid["hat"]["pattern"][i] == "x" for i in (2, 6, 10, 14))
    assert len(grid["kick"]["velocity"]) == 16


def test_harmony_and_bass_match_synth(card):
    prog = card["extras"]["suggestions.harmony"]["progression"]
    assert len(set(prog) & {"Am", "F", "C", "G"}) >= 3
    assert set(card["extras"]["suggestions.bass"]["loop"]) <= {"A", "F", "C", "G"}


def test_suggestions_well_formed(card):
    sugs = card["suggestions"]
    assert sugs
    for s in sugs:
        assert s["category"] in CATEGORIES
        assert s["title"] and len(s["title"]) <= 80 and s["body"]
        json.dumps(s["data"])
        for pat in (s["data"].get("pattern") or {}).values():
            assert len(pat) == 16 and set(pat) <= set("xo.")
    assert {"setup", "drums", "bass", "harmony"} <= {s["category"] for s in sugs}


def test_daw_specific_text_is_templated(card):
    templated = [s for s in card["suggestions"] if "template" in s["data"]]
    assert any(s["category"] == "setup" and "Set the project to" in s["title"] for s in templated)
    assert all("{" in s["data"]["template"] and s["body"] for s in templated)  # the Ableton wording stays too


def test_templates_and_the_detected_groove_carry_bpm_and_style(card):
    bpm = round(card["core"]["bpm"])
    templates = [s for s in card["suggestions"] if s["data"].get("origin") == "template"]
    assert templates
    for s in templates:
        assert s["data"]["style_key"] in {"four_floor", "dnb", "halftime", "hiphop", "generic"}
        assert s["data"]["bpm"] == bpm
    groove = next(s for s in card["suggestions"] if s["data"].get("origin") == "detected")
    assert groove["data"]["bpm"] == bpm


def test_groove_chords_and_bass_by_section():
    """Three 8-bar sections with their own chord loops; the middle one has no bass line."""
    card, _ = card_for(bpm=124, tonic=9, mode="minor", bars_per_section=(8, 8, 8),
                       section_progressions=[[(0, 3), (8, 4), (3, 4), (10, 4)],  # Am F C G
                                             [(5, 3), (5, 3), (0, 3), (7, 4)],  # Dm Dm Am E
                                             [(3, 4), (10, 4)]],  # C G
                       bass_sections=(True, False, True), kick_gain=0.6)
    by = {s["category"]: s["data"]["by_section"] for s in card["suggestions"] if "by_section" in s["data"]}
    spans = [(s["start_bar"], s["end_bar"]) for s in by["harmony"]]
    assert spans[:2] == [(1, 8), (9, 16)] and spans[2][0] == 17, spans
    assert [s["progression"] for s in by["harmony"]] == [["Am", "F", "C", "G"], ["Dm", "Dm", "Am", "E"],
                                                          ["C", "G", "C", "G"]]
    assert len(by["harmony"][1]["roman"]) == 4  # numerals in the key the engine found
    assert [s["loop"] for s in by["bass"]] == [["A", "F", "C", "G"], [], ["C", "G", "C", "G"]]
    assert by["bass"][1]["rhythm"] is None
    notes = card["extras"]["suggestions.bass"]["bar_notes"]
    assert notes[8:16] == [None] * 8  # bass-less bars stay in place as None
    assert len(card["extras"]["suggestions.harmony"]["bar_chords"]) == len(notes)
    groove = next(s for s in card["suggestions"] if s["title"] == "Detected groove")
    assert [s["idx"] for s in groove["data"]["by_section"]] == [0, 1, 2]
    assert all(len(p) == 16 for s in groove["data"]["by_section"] for p in s["pattern"].values())


def test_bass_that_moves_inside_a_bar_gets_its_beat_notes():
    """Am F C G with the bass on the fifth for the last beat of every second bar: F F F C and G G G D. (The
    engine finds the bars from the audio, so a section's loop may start on any of the four.)"""
    card, _ = card_for(bpm=124, tonic=9, mode="minor", bars_per_section=(8, 8), bass_moves=True, kick_gain=0.6)
    bass = next(s for s in card["suggestions"] if s["category"] == "bass" and "by_section" in s["data"])
    moves = {"F": ["F", "F", "F", "C"], "G": ["G", "G", "G", "D"]}
    loops = [s for s in bass["data"]["by_section"] if len(s["loop"]) == 4]
    assert loops
    for s in loops:
        i = s["loop"].index("A")
        assert s["loop"][i:] + s["loop"][:i] == ["A", "F", "C", "G"], s["loop"]
        for note, beats in zip(s["loop"], s["beats"]):
            assert beats is None or beats == moves[note], (note, beats)
    assert any(b == moves["F"] for s in loops for b in s["beats"]) and any(b == moves["G"] for s in loops for b in s["beats"])


def test_three_four_bars_have_12_steps_and_waltz_ideas():
    """With 3 beats per bar the grid, every pattern and the pattern ideas are 12 steps long."""
    card, _ = card_for(meter=3, bpm=124, tonic=9, mode="minor")
    grid = card["extras"]["suggestions.drum_grid"]
    assert len(grid["kick"]["pattern"]) == 12 and len(grid["kick"]["velocity"]) == 12
    assert all(grid["kick"]["pattern"][i] == "x" for i in (0, 4, 8))  # the synth's kick is on every beat
    for s in card["suggestions"]:
        for pat in (s["data"].get("pattern") or {}).values():
            assert len(pat) == 12, s["title"]
        if s["data"].get("rhythm"):
            assert len(s["data"]["rhythm"]) == 12
    ideas = [s for s in card["suggestions"] if s["data"].get("origin") == "template"]
    assert ideas and {s["data"]["style_key"] for s in ideas} == {"waltz"}
