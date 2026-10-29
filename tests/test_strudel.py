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


def run_js(card: dict) -> dict:
    script = f"""
const {{ strudelCode, strudelLink }} = require({json.dumps(str(STRUDEL_JS))});
const code = strudelCode({json.dumps(card)});
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


def test_ui_offers_strudel_in_the_picker():
    static = STRUDEL_JS.parent
    js = (static / "app.js").read_text()
    assert 'strudel: { name: "Strudel", code: true }' in js
    assert "Open in Strudel" in js and "Copy code" in js
    html = (static / "index.html").read_text()
    assert html.index("/static/strudel.js") < html.index("/static/app.js")
