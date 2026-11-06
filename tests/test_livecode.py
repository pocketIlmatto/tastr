"""Tests for the TidalCycles and Sonic Pi code writers (tastr/web/static/livecode.js), run under node.

The Sonic Pi code also runs under plain Ruby with Sonic Pi's few calls stubbed out, which checks that it parses and
that the loops play the song: the right samples and notes in the right bars.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

LIVECODE_JS = Path(__file__).parents[1] / "tastr" / "web" / "static" / "livecode.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run_js(fn: str, card: dict, edits: dict | None = None) -> str:
    script = f"""
const m = require({json.dumps(str(LIVECODE_JS))});
console.log(JSON.stringify(m.{fn}({json.dumps(card)}, {json.dumps(edits)})));
"""
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


def section(label, letter, a, b):
    return {"start": 0, "end": 1, "label": label, "letter": letter, "start_bar": a, "end_bar": b}


DROP = {"kick": "x...x...x...x...", "snare": "....x.......x...", "hat": "xoxoxoxoxoxoxoxo", "open_hat": "......x.......x."}
INTRO = {"kick": "x...x...x...x...", "hat": "..x...x...x...x."}


def card(per_section=True, beats=4):
    secs = [section("intro", "A", 1, 16), section("drop", "B", 17, 48), section("intro", "A", 49, 56),
            {"start": 0, "end": 1, "label": "tail", "letter": "C"}]  # no bars: left out
    harm = {"progression": ["Am", "F", "N", "C", "G"]}
    bass = {"loop": ["A", "F"]}
    if per_section:
        harm["by_section"] = [{"idx": 0, "progression": ["Am", "F"]}, {"idx": 1, "progression": ["Am", "F", "C", "G"]}]
        bass["by_section"] = [{"idx": 0, "loop": ["A", "F"]},
                              {"idx": 1, "loop": ["A", "F", "C", "G"], "beats": [None, None, ["C", "C", "C", "G"], None],
                               "rhythm": "x.x.x.x.x.x.x.x."}]
    sugs = [{"category": "drums", "title": "Detected groove", "body": "", "data": {
        "origin": "detected", "pattern": DROP, "by_section": [{"idx": 0, "pattern": INTRO}, {"idx": 1, "pattern": DROP}]}},
        {"category": "harmony", "title": "Chord loop guess", "body": "", "data": harm},
        {"category": "bass", "title": "Bass", "body": "", "data": bass}]
    core = {"bpm": 124.04, "key": {"tonic": 9, "mode": "minor"}}
    if beats == 3:
        core["beats_per_bar"] = 3
    return {"source": {"title": "Night Shift", "artist": "Okra Lane"}, "sections": secs, "suggestions": sugs, "core": core}


# --- TidalCycles ---

def test_tidal_parts_and_song():
    code = run_js("tidalCode", card())
    assert "-- Night Shift by Okra Lane" in code
    assert "setcps (124/60/4) -- 1 cycle = 1 bar" in code
    # one part per section type, (drums, chords, bass), in one let block with no blank line inside
    assert code.count("introA = (") == 1 and code.count("dropB = (") == 1
    let = code[code.index("let introA"):code.index("bassOf (_, _, b) = b")]
    assert "\n\n" not in let
    assert 'let introA = ( stack [s "bd*4", s "[~ hh]*4"]' in code
    # drum lanes in shared mini-notation; soft hats get a gain pattern
    assert 's "[~ sn]*2"' in code and 's "hh*16" # gain "[1 0.85]*8"' in code and 's "[~ ~ ~ oh]*2"' in code
    # chords one per bar; bass a beat-weighted bar, Tidal octaves (middle C is c5), on its rhythm
    assert 'n "<a\'minor f\'major c\'major g\'major>"' in code
    assert 'struct "t*8" $ n "<a2 f2 [c3@3 g2] g2>"' in code
    assert 'struct "[~ t]*4" $ n "<a2 f2>"' in code  # default off-beat bass
    # the song in cycles, cumulative; the bar-less tail is left out
    assert "[ (0, 16, layer introA) -- Intro A, bars 1-16\n      , (16, 48, layer dropB) -- Drop B, bars 17-48\n" \
           "      , (48, 56, layer introA) -- Intro A, bars 49-56\n      ]" in code
    assert "tail" not in code
    assert "resetCycles" in code
    assert "d1 $ song drumsOf" in code and "d2 $ song bassOf" in code and "d3 $ song chordsOf" in code


def test_tidal_single_chord_loop_and_three_four():
    code = run_js("tidalCode", card(per_section=False, beats=3))
    assert "setcps (124/60/3) -- 1 cycle = 1 bar of 3/4" in code
    # without per-section harmony every part plays the detected loop, rests ("N") dropped
    assert code.count('n "<a\'minor f\'major c\'major g\'major>"') == 2
    # 12 steps a bar: the default bass sits on the off-beat 8ths of 3 beats
    assert 'struct "[~ t]*3"' in code
    assert 's "bd*3"' in code


def test_tidal_edits_and_flats():
    edits = {"chords": {"1": {"chords": ["Bbm7", "Eb", None, "F#"]}}, "bass": {"1": {"notes": ["Bb", None], "rhythm": None}}}
    code = run_js("tidalCode", card(), edits)
    assert "-- With your edits in: Drop B." in code
    assert 'n "<bf\'minor7 ef\'major ~ fs\'major>"' in code
    assert 'n "<bf2 ~>"' in code


def test_tidal_without_sections_or_harmony():
    c = {"source": {"title": "Loop"}, "sections": [], "suggestions": [{"category": "drums", "title": "", "body": "", "data": {
        "origin": "detected", "pattern": {"kick": "x.......x......."}}}], "core": {}}
    code = run_js("tidalCode", c)
    assert "tempo unknown, using 120 BPM" in code and "unknown key" in code
    assert 'let track = ( stack [s "bd*2"]' in code
    assert ", silence\n" in code and ", silence )" in code
    assert "[ (0, 1, layer track) -- Whole track, bars 1-1" in code


# --- Sonic Pi ---

RUBY_HARNESS = r"""
require "json"
$events = []; $t = 0.0; $ticks = Hash.new(0); $loops = []; $synth = nil
def use_bpm(b); $bpm = b; end
def use_synth(s); $synth = s; end
def sleep(b); $t += b; end
def sample(s, **o); $events << [$cur, $t, "sample", s.to_s, o[:amp]]; end
def play(n, **o); $events << [$cur, $t, "play", $synth.to_s, n.to_s]; end
def chord(root, q); [root, q]; end
def live_loop(name, &blk); $loops << [name, blk]; end
def tick; v = $ticks[$cur]; $ticks[$cur] += 1; v; end
eval(File.read(ARGV[0]))
$loops.each { |name, blk| $cur = name.to_s; $t = 0.0; Integer(ARGV[1]).times { blk.call }; $events << [$cur, $t, "end", "", nil] }
puts JSON.generate({ bpm: $bpm, events: $events })
"""


def run_sonic(code: str, tmp_path: Path, bars: int) -> dict:
    src, harness = tmp_path / "sketch.rb", tmp_path / "harness.rb"
    src.write_text(code)
    harness.write_text(RUBY_HARNESS)
    out = subprocess.run(["ruby", str(harness), str(src), str(bars)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_sonic_pi_code_text():
    code = run_js("sonicPiCode", card())
    assert "# Night Shift by Okra Lane" in code
    assert "use_bpm 124 # one beat = a quarter note, 4 beats to the bar" in code
    assert 'drums: [[:bd_haus, "x...x...x...x..."], [:drum_cymbal_closed, "..x...x...x...x."]],' in code
    assert "chords: [chord(:a3, :minor), chord(:f3, :major), chord(:c3, :major), chord(:g3, :major)]," in code
    assert "bass: [[:a1], [:f1], [:c2, :c2, :c2, :g1], [:g1]]," in code
    assert "  [16, :introA], # Intro A, bars 1-16\n  [32, :dropB], # Drop B, bars 17-48\n  [8, :introA], # Intro A, bars 49-56\n]" in code
    assert "tail" not in code


@pytest.mark.skipif(shutil.which("ruby") is None, reason="ruby is not installed")
def test_sonic_pi_plays_the_song(tmp_path):
    run = run_sonic(run_js("sonicPiCode", card()), tmp_path, 56)
    assert run["bpm"] == 124
    ev = run["events"]
    # every loop takes exactly one bar (4 beats) per pass, so they stay in step
    assert all(e[1] == 56 * 4 for e in ev if e[2] == "end")
    bar = lambda e: int(e[1] // 4) + 1  # noqa: E731
    snares = {bar(e) for e in ev if e[0] == "drums" and e[3] == "sn_dolf"}
    assert snares == set(range(17, 49))  # only the drop has a snare
    soft = [e for e in ev if e[0] == "drums" and e[3] == "drum_cymbal_closed" and bar(e) == 17 and e[4] == 0.5]
    assert len(soft) == 8
    # bass: off-beat 8ths in the intro, every 8th in the drop, with the drop's per-beat bar (C C C G)
    bass = [e for e in ev if e[0] == "bass" and e[2] == "play"]
    assert len([e for e in bass if bar(e) == 1]) == 4
    bar19 = [e[4] for e in bass if bar(e) == 19]
    assert bar19 == ["c2"] * 6 + ["g1"] * 2
    # chords: one per bar, the part's loop indexed by the song's bar
    chords = [e[4] for e in ev if e[0] == "chords" and e[2] == "play"]
    assert len(chords) == 56 and chords[0] == '[:a3, :minor]' and chords[18] == '[:c3, :major]'


@pytest.mark.skipif(shutil.which("ruby") is None, reason="ruby is not installed")
def test_sonic_pi_three_four_and_rests(tmp_path):
    edits = {"chords": {"all": {"chords": [None, "Bb"]}}, "bass": {"all": {"notes": [None, "Bb"], "rhythm": None}}}
    code = run_js("sonicPiCode", card(beats=3), edits)
    assert "chords: [nil, chord(:bb3, :major)]," in code and "bass: [[], [:bb1]]," in code
    ev = run_sonic(code, tmp_path, 4)["events"]
    assert all(e[1] == 4 * 3 for e in ev if e[2] == "end")
    assert [e[4] for e in ev if e[0] == "chords" and e[2] == "play"] == ["[:bb3, :major]"] * 2
    assert {int(e[1] // 3) for e in ev if e[0] == "bass" and e[2] == "play"} == {1, 3}


# --- The switch in the app (checked in a browser for the PR; these keep the wiring from coming apart) ---

def test_page_loads_the_writers_and_the_switch():
    from ui_src import app_js

    static = LIVECODE_JS.parent
    html = (static / "index.html").read_text()
    assert html.index('src="/static/strudel.js"') < html.index('src="/static/livecode.js"') < html.index('src="/static/app/main.js"')
    js = app_js()
    assert '"tastr-livecode"' in js and '"aria-label": "Live-coding language"' in js
    assert "tidalCode(c, edits)" in js and "sonicPiCode(c, edits)" in js
    assert "body.push(liveView(c))" in (static / "app" / "card.js").read_text()
