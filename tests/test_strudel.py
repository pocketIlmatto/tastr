"""Tests for the "Try this in Strudel" code generator (tastr/web/static/strudel.js), run under node."""

import base64
import json
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import pytest
from ui_src import app_js

STRUDEL_JS = Path(__file__).parents[1] / "tastr" / "web" / "static" / "strudel.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run_js(card: dict, audio_url: str | None = None, edits: dict | None = None, start: int = 1) -> dict:
    script = f"""
const {{ strudelCode, strudelLink }} = require({json.dumps(str(STRUDEL_JS))});
const code = strudelCode({json.dumps(card)}, {json.dumps(audio_url)}, null, {json.dumps(edits)}, {start});
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
    # one song line per section, [bars, part, first bar]
    song = code[code.index("const song = ["):code.index("const drums")]
    assert "  [16, introA, 1], // Intro A, bars 1-16\n  [32, dropB, 17], // Drop B, bars 17-48\n  [8, introA, 49], // Intro A, bars 49-56\n]" in song
    assert "Comment a line out (//) to skip that section in every layer" in code
    # every layer is built from the song, using property access (double-quoted strings are patterns in Strudel)
    assert 'const drums = arrange(...song.map(([bars, p]) => [bars, p.drums])).bank("RolandTR909")' in code
    assert "p[" not in code
    assert 's("bd*4")' in code
    assert 's("[~ ~ ~ oh]*2")' in code
    assert code.rstrip().splitlines()[-4] == "all(x => x.early(start - 1)._punchcard())"
    assert "const start = 1 // the bar to play from: 1 is the top\ndrums: drums\nbass: bass\nchords: chords\n" in code


def test_gain_only_for_lanes_with_softer_steps():
    code = run_js(card())["code"]
    intro = code[code.index("const introA"):code.index("const dropB")]
    drop = code[code.index("const dropB"):code.index("const song")]
    assert ".gain(" not in intro
    assert drop.count(".gain(") == 1
    assert 's("hh*16").gain("[1 .55]*8")' in drop


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
    assert "const introA = {\n  drums: silence,\n}" in out["code"]
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
    assert code.count("const introA = {") == 1 and code.count("const dropB = {") == 1
    # null chords become rests; a section without bass is silence
    assert '  chords: chord("<Am ~ C!2>"),\n  bass: silence,\n}' in code
    assert ('  chords: chord("<F G Am!2>"),\n  bass: note("<f1 ~ a1 ab1>")'
            '.struct("[~ x]*4"),\n}') in code
    assert ('const chords = arrange(...song.map(([bars, p]) => [bars, p.chords]))'
            '.voicing().s("sawtooth").lpf(slider(1400, 200, 6000)).gain(.22).room(.3)') in code
    assert 'const bass = arrange(...song.map(([bars, p]) => [bars, p.bass])).s("sawtooth").lpf(slider(320, 60, 2000)).gain(.5)' in code
    assert "the detected chord loop" not in code
    assert code.rstrip().splitlines()[-4] == "all(x => x.early(start - 1)._punchcard())"
    assert "const start = 1 // the bar to play from: 1 is the top\ndrums: drums\nbass: bass\nchords: chords\n" in code


def test_section_without_entries_falls_back_to_the_track_loop():
    c = per_section_card()
    c["suggestions"][1]["data"]["by_section"] = [x for x in c["suggestions"][1]["data"]["by_section"] if x["idx"] == 1]
    c["suggestions"][2]["data"]["by_section"][1]["rhythm"] = None
    code = run_js(c)["code"]
    assert 'const introA = {\n  drums: stack(' in code and '  chords: chord("<Am F C G>"),' in code
    assert '.struct("[~ x]*4")' in code
    c["suggestions"][1]["data"]["by_section"][0]["progression"] = []
    drop = run_js(c)["code"].split("const dropB = {")[1]
    assert drop.split("}")[0].count("chords: silence,") == 1


def test_old_cards_keep_the_single_loop():
    code = run_js(card(["Am", "F", "C", "G"]))["code"]
    assert "plus the chord loop" in code and "  chords: " not in code and "  bass: " not in code
    assert 'const chords = chord("<Am F C G>")' in code


def test_per_section_harmony_with_audio_and_stems():
    c = with_grid(per_section_card(), first=0.5, bar=2.0)
    code = run_js(c, AUDIO)["code"]
    assert "with the detected drums, bass and chords for each section." in code
    assert 'original: part("orig")\ndrums: drums.gain(slider(.5, 0, 1))\n_bass: bass\n_chords: chords\n' in code
    stems = run_js_stems(c, AUDIO, ["vocals"])
    assert "  chords: chord(" in stems and "_bass: bass\n_chords: chords\n" in stems


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
    # the original follows the song too, from each section's first bar
    assert "const part = (name) => arrange(...song.map(([bars, p, from]) => [bars, orig(name, from, from + bars - 1)]))" in code
    assert 'original: part("orig")\ndrums: drums.gain(slider(.5, 0, 1))\n_bass: bass\n_chords: chords\n' in code
    assert 'loop the first drop with orig("orig", 17, 20)' in code
    assert "\nbass: bass" not in code and "all(x => x.early(start - 1)._punchcard())" in code


def test_audio_is_left_out_without_a_url_or_duration():
    assert "samples(" not in run_js(with_grid(card()))["code"]
    no_duration = with_grid(card())
    del no_duration["duration"]
    assert "samples(" not in run_js(no_duration, AUDIO)["code"]


def test_ui_offers_strudel_in_the_picker():
    static = STRUDEL_JS.parent
    js = app_js()
    assert 'strudel: { name: "Strudel", code: true }' in js
    assert "Open in Strudel" in js and "Copy short link" in js and "Copy code" in js
    assert '"/api/strudel/share"' in js  # the short share link (TAS-100)
    assert "Include the original audio, cut into bars" in js and "c.has_audio" in js
    html = (static / "index.html").read_text()
    assert html.index("/static/strudel.js") < html.index("/static/app/main.js")


def run_js_stems(card: dict, audio_url: str, stems: list[str]) -> str:
    script = f"""
const {{ strudelCode }} = require({json.dumps(str(STRUDEL_JS))});
console.log(JSON.stringify(strudelCode({json.dumps(card)}, {json.dumps(audio_url)}, {json.dumps(stems)})));
"""
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


def test_stems_replace_the_full_mix_and_unticked_stems_are_muted():
    code = run_js_stems(with_grid(card(["Am", "F", "C", "G"]), first=0.5, bar=2.0), AUDIO, ["vocals", "bass", "other"])
    assert "orig: '" not in code
    for n in ("vocals", "bass", "other", "drums"):
        assert f"  {n}: 'http://127.0.0.1:8000/api/tracks/7/stems/{n}'," in code
    assert "const orig = (name, a, b) => {" in code
    start = code.index("const part = (name) => arrange(")
    assert "orig(name, from, from + bars - 1)" in code[start:]
    tail = code[code.index("// --- Play:", start):]
    assert ('vocals_stem: part("vocals")\nbass_stem: part("bass")\nother_stem: part("other")\n'
            '_drums_stem: part("drums") // the sketch drums below play instead\n') in tail
    assert "drums: drums.gain(slider(.8, 0, 1))\n_bass: bass\n_chords: chords\n" in tail
    assert 'orig("vocals", 17, 20)' in code


def test_original_drums_stem_turns_the_sketch_drums_down():
    code = run_js_stems(with_grid(card()), AUDIO, ["drums"])
    assert '_vocals_stem: part("vocals")\n' in code and '\ndrums_stem: part("drums")\n' in code
    assert "drums.gain(slider(.5, 0, 1))" in code and "the sketch drums below play instead" not in code


EDITS = {
    "drums": {"1": {"kick": "x.x.x.x.x.x.x.x."}},
    "bass": {"1": {"notes": ["D", "D", "F", None], "rhythm": "x...x...x...x..."}},
    "chords": {"1": {"chords": ["Dm", "F", "C", None]}},
}


def test_edited_section_uses_the_edits_and_the_rest_stay_detected():
    code = run_js(per_section_card(), edits=EDITS)["code"]
    drop = code.split("const dropB = {")[1].split("\n}")[0]
    assert 's("bd*8")' in drop
    assert 'chords: chord("<Dm F C ~>")' in drop
    assert 'bass: note("<d2!2 f1 ~>").struct("x*4")' in drop
    intro = code.split("const introA = {")[1].split("\n}")[0]
    assert 'chords: chord("<Am ~ C!2>")' in intro and "bass: silence" in intro


def test_edits_header_only_with_edits():
    plain = run_js(per_section_card())["code"]
    assert "With your edits in" not in plain and "the detected" in plain
    code = run_js(per_section_card(), edits=EDITS)["code"]
    assert "// With your edits in: Drop B.\n" in code
    assert "the detected" not in code.split("// Press")[0]
    assert "With your edits in" not in run_js(per_section_card(), edits={"drums": {}, "bass": {}})["code"]


def test_edit_in_a_repeated_section_gets_its_own_part():
    code = run_js(card(["Am", "F", "C", "G"]), edits={"drums": {"2": {"kick": "xxxxxxxxxxxxxxxx"}}})["code"]
    assert "// With your edits in: Intro A." in code
    assert "const introA_bar49 = {" in code and "  [8, introA_bar49, 49], // Intro A, bars 49-56" in code
    assert "  [16, introA, 1]," in code


def test_whole_track_edits_on_cards_without_sections():
    c = card(["Am", "F", "C", "G"])
    c["sections"] = []
    code = run_js(c, edits={"chords": {"all": {"chords": ["C", "G", "Am", "F"]}}})["code"]
    assert "// With your edits in: Whole track." in code
    assert "  [1, track, 1], // Whole track, bars 1-1" in code
    assert 'chords: chord("<C G Am F>")' in code


def test_three_four_bars_play_in_three():
    waltz = {"kick": "x...........", "snare": "....x...x...", "hat": "xoxoxoxoxoxo"}
    c = {"source": {"title": "Waltz"}, "duration": 30.0, "sections": [section("verse", "A", 1, 8)],
         "core": {"bpm": 90.0, "beats_per_bar": 3, "downbeat_times": [0.0, 2.0, 4.0], "key": {"tonic": 0, "mode": "major"}},
         "suggestions": [{"category": "drums", "title": "Detected groove", "body": "", "data": {
             "origin": "detected", "pattern": waltz, "by_section": [{"idx": 0, "pattern": waltz}]}},
             {"category": "bass", "title": "Bass", "body": "", "data": {"loop": ["C"], "rhythm": "x...x.......",
                                                                     "by_section": [{"idx": 0, "loop": ["C"], "rhythm": "x...x......."}]}}]}
    code = run_js(c)["code"]
    assert "setcpm(90/3) // 1 cycle = 1 bar of 3/4" in code
    assert 's("bd ~ ~")' in code
    assert 's("~ sd sd")' in code
    assert '.struct("x x ~")' in code
    with_audio = run_js(c, "http://localhost:8000/api/tracks/1/audio")["code"]
    assert "bar = 2.0000" in with_audio


def test_shared_server_links_carry_their_token_to_the_stems():
    signed = "https://tastr.example.com/pub/1767225600.abc_-XYZ/tracks/7/audio"
    code = run_js_stems(with_grid(card(["Am", "F", "C", "G"]), first=0.5, bar=2.0), signed, ["vocals"])
    assert "  vocals: 'https://tastr.example.com/pub/1767225600.abc_-XYZ/tracks/7/stems/vocals'," in code
    assert "private link that works for a week" in code and "tastr serve" not in code
    local = run_js(with_grid(card(["Am", "F", "C", "G"]), first=0.5, bar=2.0), AUDIO)["code"]
    assert "keep `tastr serve` running" in local and "private link" not in local


def compact(fn: str, cases: list) -> list:
    script = f"""
const m = require({json.dumps(str(STRUDEL_JS))});
console.log(JSON.stringify({json.dumps(cases)}.map((a) => m.{fn}(...a))));
"""
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


def test_steps_are_written_the_short_way_a_live_coder_would():
    def cells(p, tok="x"):
        return [tok if ch != "." else "~" for ch in p]

    cases = {
        "x...x...x...x...": "x*4",
        "x.x.x.x.x.x.x.x.": "x*8",
        "xxxxxxxxxxxxxxxx": "x*16",
        "....x.......x...": "[~ x]*2",
        "..x...x...x...x.": "[~ x]*4",
        "......x.......x.": "[~ ~ ~ x]*2",
        "x..x..x.x..x..x.": "[x ~ ~ x ~ ~ x ~]*2",
        "x...........x...": "x ~ ~ x",
        "x.....x...x.....": "[x ~] [~ x] [~ x] [~ ~]",
        "x..x....x.x....x": "[x ~ ~ x] [~ ~ ~ ~] [x ~ x ~] [~ ~ ~ x]",
    }
    got = compact("compactSteps", [[cells(p), 4] for p in cases])
    assert dict(zip(cases, got)) == cases
    # 3/4: 12 steps, three beats
    three = {"x...........": "x ~ ~", "x...x...x...": "x*3", "x.x.x.x.x.x.": "x*6", "x.....x.....": "[x ~] [~ x] [~ ~]"}
    assert compact("compactSteps", [[cells(p), 3] for p in three]) == list(three.values())


def test_bar_lists_use_repeats_and_halves():
    cases = [[["Am", "F", "C", "G"]], [["Am", "~", "C", "C"]], [["Am", "F", "Am", "F"]], [["Am", "Am", "Am", "Am"]],
             [["d2", "d2", "f1", "~"]], [["C", "G", "Am"]]]
    assert compact("compactBars", cases) == ["<Am F C G>", "<Am ~ C!2>", "<Am F>", "Am", "<d2!2 f1 ~>", "<C G Am>"]


def test_knobs_are_sliders_and_a_punchcard_draws_under_the_code():
    plain = run_js(card(["Am", "F", "C", "G"]))["code"]
    assert ".lpf(slider(1400, 200, 6000))" in plain and ".lpf(slider(320, 60, 2000))" in plain
    assert "all(x => x.early(start - 1)._punchcard())" in plain
    audio = run_js(with_grid(card(["Am", "F", "C", "G"]), first=0.5, bar=2.0), AUDIO)["code"]
    assert "drums: drums.gain(slider(.5, 0, 1))\n" in audio and "all(x => x.early(start - 1)._punchcard())" in audio


def with_bass(c, by_section, **data):
    c["suggestions"].append({"category": "bass", "title": "Bass loop", "body": "", "data": {"loop": ["A"], "rhythm": "", "by_section": by_section, **data}})
    return c


def test_bass_that_moves_inside_a_bar_is_written_as_weighted_runs():
    by = [{"idx": 0, "loop": ["A", "F", "C", "G"], "rhythm": "..x...x...x...x.",
           "beats": [None, ["F", "F", "F", "C"], None, ["G", "G", "D", "D"]]},
          {"idx": 1, "loop": ["A"], "rhythm": None, "beats": [["A", "A", "A", None]]}]  # a beat with no confident note: no move
    code = run_js(with_bass(card(["Am", "F", "C", "G"]), by))["code"]
    assert 'bass: note("<a1 [f1@3 c2] c2 [g1 d2]>").struct("[~ x]*4")' in code
    assert 'bass: note("a1").struct("[~ x]*4")' in code


def test_sidechain_ducks_the_bass_and_chords_under_the_kick():
    c = card(["Am", "F", "C", "G"])
    c["suggestions"].append({"category": "bass", "title": "Sidechain the bass to the kick", "body": "", "data": {"release_ms": 242, "template": "{sidechain}"}})
    code = run_js(c)["code"]
    assert code.count('s("bd*4").duck("2:3").duckdepth(.6)') == 2  # the kick in both parts
    assert "lpf(slider(1400, 200, 6000)).gain(.22).room(.3).orbit(3)" in code
    assert "lpf(slider(320, 60, 2000)).gain(.5).orbit(2)" in code
    assert "the kick ducks the bass (orbit 2) and the chords (orbit 3)" in code
    plain = run_js(card(["Am", "F", "C", "G"]))["code"]
    assert ".duck(" not in plain and ".orbit(" not in plain


def test_a_pedal_bass_at_dubstep_tempo_wobbles():
    c = with_bass(card(["Am", "F", "C", "G"]), [], movement="pedal")
    c["tags"] = [{"tag": "dubstep/halftime tempo", "score": 0.8}]
    assert ".gain(.5).lpsync(8).lpdepth(2).lpq(6)" in run_js(c)["code"]
    c["tags"] = [{"tag": "house tempo", "score": 0.8}]
    assert ".lpsync(" not in run_js(c)["code"]


def test_start_bar_plays_from_a_chosen_section():
    code = run_js(card(["Am", "F", "C", "G"]), start=17)["code"]
    assert "const start = 17 // the bar to play from: 1 is the top (here: Drop B)" in code
    assert "all(x => x.early(start - 1)._punchcard())" in code
    # out of range: from the top
    assert "const start = 1 // the bar to play from: 1 is the top\n" in run_js(card(), start=999)["code"]


# ---- Templates (TAS-67): fill-ins, the mash-up fit, the diff ----

def run_tpl(expr: str, **data) -> object:
    """Evaluate `expr` with strudel.js's exports in scope and `data` as JSON globals."""
    script = f"""
const S = require({json.dumps(str(STRUDEL_JS))});
Object.assign(globalThis, S, {json.dumps(data)});
console.log(JSON.stringify({expr}));
"""
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


def tpl_card(**kw):
    c = with_grid(card(["Am", "F", "C", "G"]))
    c["core"]["key"]["camelot"] = "8A"
    c["sections"][1]["energy"] = 0.9
    return {**c, **kw}


def test_fill_ins_fill_and_report_unknown_names():
    out = run_tpl("strudelFill('setcpm({cpm}) // {title}{by}, {key} {{bpm}} {nope} {drums.nope}', strudelFillins(c))", c=tpl_card())
    assert out["code"] == "setcpm(124/4) // Night Shift by Okra Lane, A minor {bpm} {nope} {drums.nope}"
    assert out["unknown"] == ["nope", "drums.nope"]


def test_fill_ins_follow_the_track():
    f = run_tpl("Object.fromEntries(Object.entries(strudelFillins(c, { audioUrl: url })).map(([k, x]) => [k, x.v]))",
                c=tpl_card(), url=AUDIO)
    assert f["chords"] == "<Am F C G>" and f["bass"] == "<a1 f1 c2 g1>" and f["scale"] == "a:minor"
    assert (f["drop"], f["drop.end"]) == ("17", "20")
    assert f["drums.drop"] == f["drums"] and f["drums.drop"].startswith('stack(\n  s("bd*4"),') and f["drums.intro"] != f["drums"]
    assert f["drums.song"].startswith("arrange(\n  [16, stack(") and "// Drop B, bars 17-48" in f["drums.song"]
    assert f["loop"] == 'orig("orig", 17, 20)' and f"samples({{ orig: '{AUDIO}' }})" in f["audio"]
    assert f["stems.vocals"] == "'http://127.0.0.1:8000/api/tracks/7/stems/vocals'"
    assert f["song"].startswith("// --- Parts") and "setcpm" not in f["song"]
    assert "b.title" not in f  # no second track


def test_fill_ins_use_the_meter_and_stems():
    c = tpl_card()
    c["core"]["beats_per_bar"] = 3
    c["suggestions"][0]["data"]["by_section"][1]["pattern"] = {"kick": "x...x...x..."}
    f = run_tpl("Object.fromEntries(Object.entries(strudelFillins(c, { audioUrl: url, stems: ['vocals', 'bass'] })).map(([k, x]) => [k, x.v]))",
                c=c, url=AUDIO)
    assert f["cpm"].endswith("/3") and 's("bd*3")' in f["drums.drop"]
    assert f["loop"] == 'stack(orig("vocals", 17, 20), orig("bass", 17, 20))' and "  vocals: '" in f["audio"]


def test_built_in_templates_fill_without_unknowns():
    b = with_grid(card(["Dm", "Bb"]), first=0.2, bar=240 / 128)
    b["id"], b["source"]["title"], b["core"]["bpm"], b["core"]["key"] = 8, "Second", 128, {"tonic": 2, "mode": "minor", "camelot": "7A"}
    out = run_tpl("STRUDEL_TEMPLATES.map((t) => strudelFill(t.text, strudelFillins(c, { audioUrl: url, b: { card: b, audioUrl: url.replace('/7/', '/8/') } })))",
                  c=tpl_card(), b=b, url=AUDIO)
    assert all(not r["unknown"] for r in out)
    mash = out[4]["code"]
    assert "  a_mix: 'http://127.0.0.1:8000/api/tracks/7/audio'," in mash and "  b_mix: 'http://127.0.0.1:8000/api/tracks/8/audio'," in mash
    assert "const B = (name, a, b) => {" in mash and ".clip(1).speed(0.969)" in mash and "B: 128 BPM, D minor (7A)" in mash
    assert "  section(32, [17, 48], [17, 48], {" in mash  # the drop takes B's drop
    assert run_tpl("STRUDEL_TEMPLATES.filter((t) => strudelUsesMash(t.text)).map((t) => t.id)") == ["mash"]
    assert run_tpl("STRUDEL_TEMPLATES.filter((t) => strudelUsesMashParts(t.text)).map((t) => t.id)") == ["mash"]
    # {b.audio} and {b.loop} stay for your own templates
    own = run_tpl("strudelFill('{audio}\\n{b.audio}\\nstack({loop}, {b.loop}.speed({b.speed}))', strudelFillins(c, { audioUrl: url, b: { card: b, audioUrl: url.replace('/7/', '/8/') } }))",
                  c=tpl_card(), b=b, url=AUDIO)
    assert not own["unknown"] and "samples({ b: 'http://127.0.0.1:8000/api/tracks/8/audio' })" in own["code"]
    assert "const origB = (a, b) => {" in own["code"] and "origB(17, 20).speed(0.969)" in own["code"]
    assert run_tpl("STRUDEL_TEMPLATES.filter((t) => strudelUsesAudio(t.text)).map((t) => t.id)") == ["loop", "half", "mash"]


def test_unfill_turns_values_back_into_fill_ins():
    out = run_tpl("strudelUnfill('// Night Shift {x}\\nsetcpm(124/4) // A minor, 8A', strudelFillins(c))", c=tpl_card())
    assert out == "// {title} {{x}}\nsetcpm({cpm}) // {key}, {camelot}"


@pytest.mark.parametrize("a, b, speed, fit", [
    (124, 124, 1, "same key"),
    (124, 62, 1, "same key"),          # folded by an octave: plays at normal speed
    (128, 120, 1.067, "keys clash"),   # 8A up 1.1 semitones -> 3A
    (120, 127, 0.945, "mixes well"),   # 4A (F minor) down 1 semitone -> E minor, 9A: next to 8A
])
def test_mash_up_speed_and_key_fit(a, b, speed, fit):
    cam_b = {"same key": "8A", "keys clash": "8A", "mixes well": "4A"}[fit]
    out = run_tpl("strudelMashFit({ bpm: a, camelot: '8A' }, { bpm: b, camelot: cb })", a=a, b=b, cb=cam_b)
    assert out["r"] == speed and out["fit"] == fit
    assert run_tpl("strudelCamelot(9, 'minor') + strudelCamelot(0, 'major') + strudelCamelot(8, 'minor')") == "8A8B1A"


def test_diff_and_hunks():
    d = run_tpl("strudelDiff('a\\nb\\nc\\nd\\ne\\nf', 'a\\nB\\nc\\nd\\ne\\nf\\ng')")
    assert d == [[" ", "a"], ["-", "b"], ["+", "B"], [" ", "c"], [" ", "d"], [" ", "e"], [" ", "f"], ["+", "g"]]
    h = run_tpl("strudelHunks(strudelDiff('a\\nb\\nc\\nd\\ne\\nf', 'a\\nB\\nc\\nd\\ne\\nf\\ng'))")
    assert h == [[" ", "a"], ["-", "b"], ["+", "B"], [" ", "c"], None, [" ", "f"], ["+", "g"]]


def test_summary_for_claude():
    out = run_tpl("strudelSummary(c)", c=tpl_card())
    assert out.splitlines()[:2] == ["Night Shift by Okra Lane", "124 BPM, 4/4, A minor (8A)"]
    assert "Drop B bars 17-48" in out


# ---- Mash-up parts by section (TAS-87) ----

def mash_card(id_, bpm, secs, title="A track"):
    """secs: (label, letter, first bar, last bar, energy). A bar grid at `bpm` in 4/4 starting at 0.25 s."""
    bar = 240 / bpm
    n = max(x[3] for x in secs) + 1
    return {"id": id_, "duration": round(0.25 + n * bar, 3), "source": {"title": title}, "suggestions": [],
            "core": {"bpm": bpm, "key": {"tonic": 9, "mode": "minor", "camelot": "8A"}, "downbeat_times": [round(0.25 + i * bar, 3) for i in range(n)]},
            "sections": [{"start": 0, "end": 1, "label": l, "letter": k, "start_bar": a, "end_bar": b, "energy": e} for l, k, a, b, e in secs]}


A_SECS = [("intro", "A", 1, 8, .3), ("verse", "B", 9, 24, .5), ("chorus", "C", 25, 40, .8), ("verse", "B", 41, 56, .5), ("chorus", "C", 57, 72, .8), ("outro", "D", 73, 80, .2)]
B_SECS = [("intro", "A", 1, 4, .2), ("chorus", "B", 5, 12, .9), ("bridge", "C", 13, 20, .45), ("chorus", "B", 21, 28, .9), ("chorus", "B", 29, 36, .85)]
MA, MB = mash_card(7, 120, A_SECS, "Quiet Garden"), mash_card(8, 120, B_SECS, "Second")
B_AUDIO = AUDIO.replace("/7/", "/8/")
BOTH = {"a": True, "b": True}


def mash(expr: str, a=MA, b=MB, **data):
    return run_tpl(expr, a=a, b=b, url=AUDIO, burl=B_AUDIO, both=BOTH, **data)


def test_b_section_is_the_nth_same_label_else_the_closest_energy():
    froms = mash("a.sections.map((x, idx) => strudelMashFrom(a, b, { ...x, idx }))")
    # verses: B has none, so the closest energy (bridge .45); choruses: B's 1st and 2nd; intro: B's intro; outro: B's intro (.2)
    assert froms == [0, 2, 1, 2, 3, 0]


def test_quick_picks_fill_the_grid():
    st = mash("strudelMashState(a, b, null, both)")
    assert st["src"] == {"a": "stems", "b": "stems"} and st["preset"] == "avox"
    assert all(r["on"] == ["a.vocals", "b.bass", "b.other", "b.drums"] for r in st["rows"])
    trade = mash("strudelMashApply(a, b, strudelMashState(a, b, null, both), 'trade', both).rows.map((r) => r.on)")
    verse, chorus = trade[1], trade[2]
    assert verse == ["a.vocals", "a.bass", "a.other", "a.drums"]
    assert chorus == ["a.bass", "a.other", "a.drums", "b.vocals"]
    # A drop or hook row trades too
    drop = mash_card(7, 120, [("verse", "A", 1, 8, .5), ("drop", "B", 9, 16, .9), ("hook", "C", 17, 24, .8)])
    rows = run_tpl("strudelMashApply(a, b, strudelMashState(a, b, null, both), 'trade', both).rows.map((r) => r.on.includes('b.vocals'))", a=drop, b=MB, both=BOTH)
    assert rows == [False, True, True]
    # Full mixes only: the Mix columns
    mix = mash("strudelMashApply(a, b, strudelMashState(a, b, null, {}), 'trade', {}).rows.map((r) => r.on)")
    assert mix[1] == ["a.mix"] and mix[2] == ["a.mix", "b.mix"]
    assert mash("strudelMashApply(a, b, strudelMashState(a, b, null, both), 'clear', both).rows.every((r) => !r.on.length)")


def test_switching_mix_and_stems_keeps_the_picks():
    out = mash("""(() => {
      const st = strudelMashApply(a, b, strudelMashState(a, b, null, both), 'trade', both);
      const mixed = JSON.parse(JSON.stringify(strudelMashSwitch(st, 'b', 'mix')));
      return [mixed.rows.map((r) => r.on), strudelMashSwitch(st, 'b', 'stems').rows.map((r) => r.on), mixed.preset];
    })()""")
    mixed, back, preset = out
    assert mixed[1] == ["a.vocals", "a.bass", "a.other", "a.drums"] and mixed[2] == ["a.bass", "a.other", "a.drums", "b.mix"]
    assert back[2] == ["a.bass", "a.other", "a.drums", "b.vocals", "b.bass", "b.other", "b.drums"] and preset is None


def test_saved_arrangement_is_checked_against_the_cards():
    saved = {"src": {"a": "stems", "b": "stems"}, "ctl": "plain", "preset": None, "pick": "trade",
             "rows": [{"idx": 0, "on": ["a.drums", "b.nope"], "from": 99}, {"idx": 1, "on": ["b.vocals"], "from": 4}]}
    st = mash("strudelMashState(a, b, saved, both)", saved=saved)
    assert st["ctl"] == "plain" and st["rows"][0] == {"idx": 0, "on": ["a.drums"], "from": 0}  # unknown part dropped, B section reset
    assert st["rows"][1]["on"] == ["b.vocals"] and st["rows"][1]["from"] == 4
    assert st["rows"][2]["on"] == ["a.bass", "a.other", "a.drums", "b.vocals"]  # new rows: the quick pick last used
    # B without stems now: its stem picks become its Mix
    st = mash("strudelMashState(a, b, saved, { a: true })", saved=saved)
    assert st["src"]["b"] == "mix" and st["rows"][1]["on"] == ["b.mix"]


def test_plain_code_loads_only_ticked_parts_and_silent_rows_are_silence():
    out = mash("""(() => {
      const st = strudelMashApply(a, b, strudelMashState(a, b, null, both), 'trade', both);
      st.ctl = 'plain'; st.rows[0].on = []; st.rows[5].on = ['a.vocals'];
      return strudelMashCode(a, b, st, { aUrl: url, bUrl: burl, can: both });
    })()""")
    assert "b_vocals: 'http://127.0.0.1:8000/api/tracks/8/stems/vocals'" in out["audio"] and "b_drums" not in out["audio"]
    lines = out["arrange"].splitlines()
    assert lines[2] == "  [8, silence], // Intro A, bars 1-8"
    assert lines[4].startswith('  [16, stack(A("a_bass", 25, 40), A("a_drums", 25, 40), A("a_other", 25, 40), B("b_vocals", 5, 12))],')
    assert lines[4].endswith("// Chorus C, bars 25-40 · B from Chorus B (bars 5-12, looped)")


def test_live_code_has_toggles_and_levels():
    out = mash("strudelMashCode(a, b, strudelMashApply(a, b, strudelMashState(a, b, null, both), 'trade', both), { aUrl: url, bUrl: burl, can: both })")
    assert out["audio"].count("_vocals: '") == 2 and "b_drums: '" in out["audio"]  # every part loads
    ar = out["arrange"]
    assert "const levelA = { vocals: slider(1, 0, 1), bass:   slider(1, 0, 1)" in ar
    assert "A('a_' + p, a1, a2).gain(t.mul(levelA[p]))" in ar and '"a_" +' not in ar
    assert "  section(16, [25, 40], [5, 12], {\n    A: { vocals: slider(0, 0, 1, 1)," in ar
    assert "    B: { vocals: slider(1, 0, 1, 1), bass:   slider(0, 0, 1, 1)" in ar


def test_double_and_half_time_b_uses_per():
    fast = mash_card(8, 162, B_SECS, "Fast")
    out = run_tpl("[strudelMashPer({ bpm: 82, bar: 240 / 82 }, { bpm: 162, bar: 240 / 162 }), strudelMashPer({ bpm: 82, bar: 240 / 82 }, { bpm: 41, bar: 240 / 41 })]")
    assert out == [0.5, 2]
    slow_a = mash_card(7, 82, A_SECS, "Slow")
    code = run_tpl("strudelMashCode(a, b, strudelMashState(a, b, null, both), { aUrl: url, bUrl: burl, can: both })", a=slow_a, b=fast, url=AUDIO, burl=B_AUDIO, both=BOTH)
    assert "  const k = run(b - a + 1).slow((b - a + 1) * 0.5).add(a - 1)" in code["audio"] and "s(name).segment(2)" in code["audio"]
    assert ".speed(1.012)" in code["audio"]
    assert "  section(16, [9, 24], [13, 20], {" in code["arrange"]  # B's bridge is 8 bars of the 32 needed: looped
    assert "(bars 13-20, looped)" in code["arrange"]
    assert "  section(8, [1, 8], [1, 4], {" in code["arrange"]
    # {b.loop}'s origB gets the same fix
    f = run_tpl("strudelFillins(a, { audioUrl: url, b: { card: b, audioUrl: burl } })['b.audio'].v", a=slow_a, b=fast, url=AUDIO, burl=B_AUDIO)
    assert 's("b").segment(2)' in f and ".slow((b - a + 1) * 0.5)" in f


def test_b_stems_for_your_own_templates():
    f = mash("Object.fromEntries(Object.entries(strudelFillins(a, { audioUrl: url, b: { card: b, audioUrl: burl, stems: ['vocals'] } })).map(([k, x]) => [k, x.v]))")
    assert "samples({ b_vocals: 'http://127.0.0.1:8000/api/tracks/8/stems/vocals' })" in f["b.audio"] and "const origB = (a, b, name) => {" in f["b.audio"]
    assert f["b.loop"] == 'stack(origB(5, 8, "b_vocals"))'


def test_signed_links_reach_both_tracks_stems():
    signed = "https://tastr.example/pub/tok123/tracks/7/audio"
    out = mash("strudelMashCode(a, b, strudelMashState(a, b, null, both), { aUrl: s1, bUrl: s2, can: both })", s1=signed, s2=signed.replace("/7/", "/8/").replace("tok123", "tok456"))
    assert "b_vocals: 'https://tastr.example/pub/tok456/tracks/8/stems/vocals'" in out["audio"] and "private links" in out["audio"]


# ---- Key lock (T70): B stretched on the server to A's tempo with its pitch kept ----

@pytest.mark.parametrize("a, b, cam_b, fit, own", [
    (123, 117.45, "4A", "keys clash", "keys clash"),   # 0.81 semitones: in tune at 11A, which clashes with 8A
    (123, 117.45, "8A", "keys clash", "same key"),     # B is 8A too, but the speed change moves it to 3A
    (123, 119, "8A", "between keys", "same key"),      # 0.57 semitones: between keys
    (120, 118, "8A", "same key", "same key"),          # 0.29 semitones: in tune
])
def test_mash_up_fit_counts_a_shift_near_a_whole_step_as_in_tune(a, b, cam_b, fit, own):
    out = run_tpl("strudelMashFit({ bpm: a, camelot: '8A' }, { bpm: b, camelot: cb })", a=a, b=b, cb=cam_b)
    assert out["fit"] == fit and out["own"] == own and out["ok"] == (fit in ("same key", "mixes well"))


def test_mash_up_fit_with_key_lock_keeps_bs_own_key():
    out = run_tpl("strudelMashFit({ bpm: 123, camelot: '4A' }, { bpm: 117.45, camelot: '4A' }, true)")
    assert out["fit"] == "same key" and out["ok"] and out["lock"] and out["cam"] == "4A" and out["r"] == 1.047
    out = run_tpl("strudelMashFit({ bpm: 123, camelot: '4A' }, { bpm: 117.45, camelot: '5A' }, true)")
    assert out["fit"] == "mixes well" and out["ok"]


def test_key_locked_b_plays_at_speed_one_from_the_stretched_file():
    fast = mash_card(8, 124, B_SECS, "Second")  # A 120, B 124: speed 0.968
    lock_url = B_AUDIO.replace("/audio", "/keylock/0.968/audio")
    plain = mash("strudelMashCode(a, b, strudelMashState(a, b, null, {}), { aUrl: url, bUrl: burl, can: {} })", b=fast)
    out = mash("strudelMashCode(a, b, strudelMashState(a, b, null, {}), { aUrl: url, bUrl: burl, can: {}, bLock: { url: lk } })",
               b=fast, lk=lock_url)
    assert ".speed(0.968)" in plain["audio"] and ".speed(" not in out["audio"]
    assert f"b_mix: '{lock_url}'" in out["audio"] and "key-locked: stretched to A's tempo (×0.968)" in out["audio"]
    bar = 240 / 124
    assert f"bbar = {bar / 0.968:.4f}" in out["audio"] and f"bbar = {bar:.4f}" in plain["audio"]
    # Stems come from next to the stretched mix.
    out = mash("strudelMashCode(a, b, strudelMashSwitch(strudelMashState(a, b, null, both), 'b', 'stems'), { aUrl: url, bUrl: burl, can: both, bLock: { url: lk } })",
               b=fast, lk=lock_url)
    assert "b_bass: '" + B_AUDIO.replace("/audio", "/keylock/0.968/stems/bass") + "'" in out["audio"]


def test_key_locked_fill_ins():
    fast = mash_card(8, 124, B_SECS, "Second")
    f = mash("Object.fromEntries(Object.entries(strudelFillins(a, { audioUrl: url, b: { card: b, audioUrl: burl, lock: { url: 'https://x/keylock/0.968/audio' } } })).map(([k, x]) => [k, x.v]))", b=fast)
    assert f["b.speed"] == "1" and f["b.shift"] == "not at all" and f["b.newkey"] == "A minor (8A, same key)"
    # {b.audio} loads the stretched file (whose bars are shorter by the speed), never the original.
    assert "b: 'https://x/keylock/0.968/audio'" in f["b.audio"] and B_AUDIO not in f["b.audio"]
    assert f"{240 / 124 / 0.968:.4f}" in f["b.audio"]
