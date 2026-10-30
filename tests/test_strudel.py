"""Tests for the "Try this in Strudel" code generator (tastr/web/static/strudel.js), run under node."""

import base64
import json
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import pytest

STRUDEL_JS = Path(__file__).parents[1] / "tastr" / "web" / "static" / "strudel.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run_js(card: dict, audio_url: str | None = None) -> dict:
    script = f"""
const {{ strudelCode, strudelLink }} = require({json.dumps(str(STRUDEL_JS))});
const code = strudelCode({json.dumps(card)}, {json.dumps(audio_url)});
console.log(JSON.stringify({{ code, link: strudelLink(code) }}));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def section(label, letter, a, b):
    return {"start": 0, "end": 1, "label": label, "letter": letter, "start_bar": a, "end_bar": b}


DROP = {"kick": "x...x...x...x...", "snare": "....x.......x...", "hat": "xoxoxoxoxoxoxoxo", "open_hat": "......x.......x."}
INTRO = {"kick": "x...x...x...x...", "snare": "................", "hat": "..x...x...x...x."}


def card(progression=None, mode="minor"):
    secs = [section("intro", "A", 1, 16), section("drop", "B", 17, 48), section("intro", "A", 49, 56),
            {"start": 0, "end": 1, "label": "tail", "letter": "C"}]  # no bars: left out
    sugs = [{"category": "drums", "title": "Detected groove", "body": "", "data": {
        "origin": "detected", "pattern": DROP,
        "by_section": [{"idx": 0, "pattern": INTRO}, {"idx": 1, "pattern": DROP}, {"idx": 2, "pattern": {"kick": "................"}}]}}]
    if progression:
        sugs.append({"category": "harmony", "title": "Chord loop guess", "body": "", "data": {"progression": progression}})
    return {"source": {"title": "Night Shift", "artist": "Okra Lane"}, "sections": secs, "suggestions": sugs,
            "core": {"bpm": 124.04, "key": {"tonic": 9, "mode": mode}}}


def test_sections_become_parts_arranged_by_bar_count():
    code = run_js(card(["Am", "F", "N", "C", "G"]))["code"]
    assert "// Night Shift by Okra Lane" in code
    assert "Sketched by Tastr: 124 BPM, A minor" in code
    assert "setcpm(124/4)" in code
    # one const per section type, the first occurrence's pattern
    assert code.count("const introA =") == 1 and code.count("const dropB =") == 1
    assert "tail" not in code
    assert "[16, introA], // Intro A, bars 1-16" in code
    assert "[32, dropB], // Drop B, bars 17-48" in code
    assert "[8, introA] // Intro A, bars 49-56" in code
    assert code.index('const drums = arrange(') < code.index('.bank("RolandTR909")')
    assert 's("[bd ~ ~ ~] [bd ~ ~ ~] [bd ~ ~ ~] [bd ~ ~ ~]")' in code
    assert 's("[~ ~ ~ ~] [~ ~ oh ~] [~ ~ ~ ~] [~ ~ oh ~]")' in code
    assert code.rstrip().splitlines()[-4] == "stack(drums, bass, chords)"


def test_gain_only_for_lanes_with_softer_steps():
    code = run_js(card())["code"]
    intro = code[code.index("const introA"):code.index("const dropB")]
    drop = code[code.index("const dropB"):code.index("const drums")]
    assert ".gain(" not in intro
    assert drop.count(".gain(") == 1
    assert 's("[hh hh hh hh] [hh hh hh hh] [hh hh hh hh] [hh hh hh hh]").gain("[1 .55 1 .55] [1 .55 1 .55] [1 .55 1 .55] [1 .55 1 .55]")' in drop


def test_detected_chord_loop_drops_no_chord():
    code = run_js(card(["Am", "F", "N", "C", "G"]))["code"]
    assert "the detected chord loop" in code
    assert 'chord("<Am F C G>")' in code
    assert 'note("<a1 f1 c2 g1>")' in code


def test_fallback_harmony_when_no_chord_loop():
    code = run_js(card())["code"]
    assert "a common loop in A minor (no confident chord loop was detected)" in code
    assert 'chord("<Am F C G>")' in code
    major = run_js(card(mode="major"))["code"]
    assert 'chord("<A E F#m D>")' in major


def test_empty_part_is_silence_and_link_round_trips():
    c = card()
    c["suggestions"][0]["data"]["by_section"][0]["pattern"] = {"kick": "................"}
    out = run_js(c)
    assert "const introA = silence" in out["code"]
    assert out["link"].startswith("https://strudel.cc/#")
    decoded = base64.b64decode(urllib.parse.unquote(out["link"].split("#", 1)[1])).decode("utf-8")
    assert decoded == out["code"]


def per_section_card():
    c = card(["Am", "F", "C", "G"])
    meta = lambda i: {k: c["sections"][i][k] for k in ("label", "letter", "start_bar", "end_bar")}
    c["suggestions"][1]["data"]["by_section"] = [
        {"idx": 0, **meta(0), "progression": ["Am", None, "C", "C"], "roman": ["i", None, "III", "III"]},
        {"idx": 1, **meta(1), "progression": ["F", "G", "Am", "Am"], "roman": ["VI", "VII", "i", "i"]},
        {"idx": 2, **meta(2), "progression": ["Dm", "Dm", "Am", "Am"], "roman": ["iv", "iv", "i", "i"]},  # same part as idx 0
    ]
    c["suggestions"].append({"category": "bass", "title": "Bass loop", "body": "", "data": {
        "loop": ["A", "F", "C", "G"], "rhythm": "x...x...x...x...", "by_section": [
            {"idx": 0, **meta(0), "loop": [], "degrees": [], "rhythm": None},
            {"idx": 1, **meta(1), "loop": ["F", None, "A", "Ab"], "degrees": ["6", None, "1", "(Ab)"], "rhythm": "..x...x...x...x."},
        ]}})
    return c


def test_bass_and_chords_get_one_part_per_section_type():
    code = run_js(per_section_card())["code"]
    assert "drums, bass and chords for each section" in code
    assert code.count("const introA_chords =") == 1 and code.count("const dropB_chords =") == 1
    # null chords become rests; a section without bass is silence
    assert 'const introA_chords = chord("<Am ~ C C>"), introA_bass = silence' in code
    assert ('const dropB_chords = chord("<F G Am Am>"), dropB_bass = note("<f1 ~ a1 ab1>")'
            '.struct("[~ ~ x ~] [~ ~ x ~] [~ ~ x ~] [~ ~ x ~]")') in code
    chords = code[code.index("const chords = arrange("):code.index("const bass = arrange(")]
    assert "  [16, introA_chords],\n  [32, dropB_chords],\n  [8, introA_chords]\n)" in chords
    assert chords.rstrip().endswith('.voicing().s("sawtooth").lpf(1400).gain(.22).room(.3)')
    assert '  [8, introA_bass]\n).s("sawtooth").lpf(320).gain(.5)' in code
    assert "the detected chord loop" not in code
    assert code.rstrip().splitlines()[-4] == "stack(drums, bass, chords)"


def test_section_without_entries_falls_back_to_the_track_loop():
    c = per_section_card()
    c["suggestions"][1]["data"]["by_section"] = [x for x in c["suggestions"][1]["data"]["by_section"] if x["idx"] == 1]
    c["suggestions"][2]["data"]["by_section"][1]["rhythm"] = None
    code = run_js(c)["code"]
    assert 'const introA_chords = chord("<Am F C G>")' in code
    assert '.struct("[~ x]*4")' in code
    c["suggestions"][1]["data"]["by_section"][0]["progression"] = []
    assert "const dropB_chords = silence" in run_js(c)["code"]


def test_old_cards_keep_the_single_loop():
    code = run_js(card(["Am", "F", "C", "G"]))["code"]
    assert "plus the chord loop" in code and "_chords" not in code and "_bass" not in code
    assert 'const chords = chord("<Am F C G>")' in code


def test_per_section_harmony_with_audio_and_stems():
    c = with_grid(per_section_card(), first=0.5, bar=2.0)
    code = run_js(c, AUDIO)["code"]
    assert "with the detected drums, bass and chords for each section." in code
    assert "stack(original, drums.gain(.5) /*, bass, chords */)" in code
    stems = run_js_stems(c, AUDIO, ["vocals"])
    assert "const dropB_chords" in stems and "  // bass, chords,\n)" in stems


AUDIO = "http://127.0.0.1:8000/api/tracks/7/audio"


def with_grid(c, first=0.5, bar=240 / 124, n=57):
    c["id"], c["duration"] = 7, round(first + n * bar, 3)
    c["core"]["downbeat_times"] = [round(first + i * bar, 3) for i in range(n)]
    return c


def test_tempo_comes_from_the_bar_grid():
    c = with_grid(card(), bar=240 / 125)
    assert "setcpm(125/4) // 1 cycle = 1 bar (tempo from the detected bar grid)" in run_js(c)["code"]
    assert "setcpm(124/4) // 1 cycle = 1 bar\n" in run_js(card())["code"]


def test_original_audio_is_cut_into_bars_per_section():
    code = run_js(with_grid(card(["Am", "F", "C", "G"]), first=0.5, bar=2.0), AUDIO)["code"]
    assert "The original audio cut into bars" in code
    assert f"samples({{ orig: '{AUDIO}' }})" in code  # single quotes: double quotes are patterns in Strudel
    assert f'"{AUDIO}"' not in code
    assert "const len = 114.500, bar = 2.0000, first = 0.500" in code
    original = code[code.index("const original = arrange("):code.index("stack(original")]
    assert "[16, orig(1, 16)], // Intro A" in original
    assert "[32, orig(17, 48)], // Drop B" in original
    assert "[8, orig(49, 56)] // Intro A" in original
    assert original.count("orig(") == 3  # one per section with bars
    assert "stack(original, drums.gain(.5) /*, bass, chords */)" in code
    assert "loop the first drop with orig(17, 20)" in code
    assert "stack(drums, bass, chords)" not in code


def test_audio_is_left_out_without_a_url_or_duration():
    assert "samples(" not in run_js(with_grid(card()))["code"]
    no_duration = with_grid(card())
    del no_duration["duration"]
    assert "samples(" not in run_js(no_duration, AUDIO)["code"]


def test_ui_offers_strudel_in_the_picker():
    static = STRUDEL_JS.parent
    js = (static / "app.js").read_text()
    assert 'strudel: { name: "Strudel", code: true }' in js
    assert "Open in Strudel" in js and "Copy code" in js
    assert "Include the original audio, cut into bars" in js and "c.has_audio" in js
    html = (static / "index.html").read_text()
    assert html.index("/static/strudel.js") < html.index("/static/app.js")


def run_js_stems(card: dict, audio_url: str, stems: list[str]) -> str:
    script = f"""
const {{ strudelCode }} = require({json.dumps(str(STRUDEL_JS))});
console.log(JSON.stringify(strudelCode({json.dumps(card)}, {json.dumps(audio_url)}, {json.dumps(stems)})));
"""
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


def test_stems_replace_the_full_mix_and_unticked_stems_are_commented_out():
    code = run_js_stems(with_grid(card(["Am", "F", "C", "G"]), first=0.5, bar=2.0), AUDIO, ["vocals", "bass", "other"])
    assert "orig: '" not in code
    for n in ("vocals", "bass", "other", "drums"):
        assert f"  {n}: 'http://127.0.0.1:8000/api/tracks/7/stems/{n}'," in code
    assert "const orig = (name, a, b) => {" in code
    start = code.index("const part = (name) => arrange(")
    part = code[start:code.index("stack(\n", start)]
    assert "[16, orig(name, 1, 16)], // Intro A" in part and "[8, orig(name, 49, 56)] // Intro A" in part
    tail = code[code.index("stack(\n", start):]
    assert '  part("vocals"),\n  part("bass"),\n  part("other"),\n  // part("drums"), // the sketch drums below play instead\n' in tail
    assert "  drums.gain(.8),\n  // bass, chords,\n)" in tail
    assert 'orig("vocals", 17, 20)' in code


def test_original_drums_stem_turns_the_sketch_drums_down():
    code = run_js_stems(with_grid(card()), AUDIO, ["drums"])
    assert '  // part("vocals"),\n' in code and '  part("drums"),\n' in code
    assert "drums.gain(.5)" in code and "the sketch drums below play instead" not in code
