"""The Strudel sketches, played by Strudel itself: tests/strudel-runtime/eval.mjs evaluates them with
@strudel/core, mini, tonal and transpiler (pinned there) and returns the events they play.

Optional: skipped unless `npm ci` has been run in tests/strudel-runtime (CI does it when the sketch changes).
Strudel is AGPL-3.0; it is only installed for these tests, never bundled or shipped with Tastr.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_strudel import AUDIO, card, run_js, run_js_stems, with_bass, with_grid

RUNTIME = Path(__file__).parent / "strudel-runtime"


def _runtime_ok() -> bool:
    if shutil.which("node") is None or not (RUNTIME / "node_modules" / "@strudel" / "core").is_dir():
        return False
    # module.registerHooks (Node 22.15+) stands in for a browser-only import of @strudel/core.
    return subprocess.run(["node", "-e", "process.exit(require('node:module').registerHooks ? 0 : 1)"]).returncode == 0


pytestmark = pytest.mark.skipif(not _runtime_ok(), reason="Strudel test runtime not installed (npm ci in tests/strudel-runtime)")


def play(*programs: tuple[str, float, float]) -> list[list]:
    """Each (code, from cycle, to cycle) -> its events as [begin, end, value], in time order."""
    out = subprocess.run(["node", str(RUNTIME / "eval.mjs")], input=json.dumps([{"code": c, "from": a, "to": b} for c, a, b in programs]),
                         capture_output=True, text=True, check=True, cwd=RUNTIME).stdout
    results = json.loads(out)
    for r in results:
        assert "error" not in r, r.get("error")
    return [r["events"] for r in results]


def at(events, key, value):
    return [e for e in events if e[2].get(key) == value]


def test_the_plain_sketch_plays_its_drums_bass_and_chords():
    sec = {"idx": 0, "loop": ["A", "F", "C", "G"], "rhythm": "..x...x...x...x."}
    code = run_js(with_bass(card(["Am", "F", "C", "G"]), [sec, {**sec, "idx": 1}, {**sec, "idx": 2}]))["code"]
    (drop,) = play((code, 16, 17))  # bar 17: the first bar of Drop B
    assert [e[0] - 16 for e in at(drop, "s", "bd")] == [0, 0.25, 0.5, 0.75]
    assert [e[0] - 16 for e in at(drop, "s", "sd")] == [0.25, 0.75]
    assert len(at(drop, "s", "hh")) == 16 and len(at(drop, "s", "oh")) == 2
    bass = [e for e in drop if e[2].get("s") == "sawtooth" and e[2].get("gain") == 0.5]
    assert [(e[0] - 16, e[2]["note"]) for e in bass] == [(0.125, "a1"), (0.375, "a1"), (0.625, "a1"), (0.875, "a1")]
    assert {e[2]["cutoff"] for e in bass} == {320}  # the slider's starting value (lpf sets "cutoff")
    chords = [e for e in drop if e[2].get("gain") == 0.22]
    assert chords and all(e[0] == 16 for e in chords)


def test_a_moving_bass_bar_plays_its_runs():
    sec = {"idx": 0, "loop": ["A", "F", "C", "G"], "rhythm": "..x...x...x...x.", "beats": [None, ["F", "F", "F", "C"], None, None]}
    code = run_js(with_bass(card(["Am", "F", "C", "G"]), [sec]))["code"]
    (bar2,) = play((code, 1, 2))
    bass = [e[2]["note"] for e in bar2 if e[2].get("s") == "sawtooth" and e[2].get("gain") == 0.5]
    assert bass == ["f1", "f1", "f1", "c2"]


def test_start_plays_from_the_chosen_bar():
    c = card(["Am", "F", "C", "G"])
    top, from17 = play((run_js(c)["code"], 16, 18), (run_js(c, start=17)["code"], 0, 2))
    assert from17 and [[a - 16, b - 16, v] for a, b, v in top] == from17


def test_sidechain_kicks_duck_the_bass_and_chord_orbits():
    c = card(["Am", "F", "C", "G"])
    c["suggestions"].append({"category": "bass", "title": "Sidechain", "body": "", "data": {"template": "{sidechain}"}})
    (ev,) = play((run_js(c)["code"], 16, 17))
    kicks = at(ev, "s", "bd")
    assert kicks and all(e[2]["duckorbit"] == [2, 3] and e[2]["duckdepth"] == 0.6 for e in kicks)
    assert {e[2].get("orbit") for e in ev if e[2].get("s") == "sawtooth"} == {2, 3}


def test_with_stems_the_ticked_stems_and_sketch_drums_play_and_muted_lines_dont():
    code = run_js_stems(with_grid(card(["Am", "F", "C", "G"]), first=0.5, bar=2.0), AUDIO, ["vocals", "bass"])
    (ev,) = play((code, 16, 17))
    sounds = {e[2].get("s") for e in ev}
    assert {"vocals", "bass", "bd"} <= sounds
    assert not {"other", "drums", "sawtooth"} & sounds  # _other_stem, _drums_stem, _bass and _chords are muted
    vocal = at(ev, "s", "vocals")[0][2]
    # bar 17 of the stem: seconds 0.5 + 16 * 2.0 = 32.5 to 34.5 of a 114.5 s file
    assert vocal["begin"] == pytest.approx(32.5 / 114.5) and vocal["end"] == pytest.approx(34.5 / 114.5)


def test_every_kind_of_sketch_evaluates():
    from test_strudel import EDITS

    wob = with_bass(card(["Am", "F", "C", "G"]), [], movement="pedal")
    wob["tags"] = [{"tag": "dubstep/halftime tempo", "score": 0.8}]
    waltz = {"kick": "x...........", "snare": "....x...x..."}
    three = {"source": {"title": "Waltz"}, "duration": 30.0, "sections": [], "core": {"bpm": 90.0, "beats_per_bar": 3, "key": {"tonic": 0, "mode": "major"}},
             "suggestions": [{"category": "drums", "title": "Detected groove", "body": "", "data": {"origin": "detected", "pattern": waltz}}]}
    codes = [run_js(card())["code"], run_js(card(["Am", "F", "C", "G"]), edits=EDITS)["code"], run_js(wob)["code"], run_js(three)["code"],
             run_js(with_grid(card(["Am", "F", "C", "G"])), AUDIO)["code"]]
    results = play(*[(c, 0, 4) for c in codes])
    assert all(results)
    assert any("lpsync" in e[2] for e in results[2])
    assert [e[0] for e in at(results[3], "s", "bd")] == [0, 1, 2, 3]  # one bar of 3/4 per cycle
