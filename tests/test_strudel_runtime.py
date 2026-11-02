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


def test_every_built_in_template_plays():
    """The templates (TAS-67), filled from a track and a mash-up partner, evaluate and play in Strudel."""
    from test_strudel import run_tpl, tpl_card

    b = with_grid(card(["Dm", "Bb"]), first=0.2, bar=240 / 128)
    b["id"], b["core"]["bpm"], b["core"]["key"] = 8, 128, {"tonic": 2, "mode": "minor", "camelot": "7A"}
    codes = run_tpl("STRUDEL_TEMPLATES.map((t) => strudelFill(t.text, strudelFillins(c, { audioUrl: url, b: { card: b, audioUrl: url } })).code)"
                    " .concat([strudelFill(STRUDEL_NEW_TEXT, strudelFillins(c)).code])", c=tpl_card(), b=b, url=AUDIO)
    results = play(*[(c, 16, 18) for c in codes])
    assert all(results), [len(r) for r in results]
    loop, drums, half, mash = results[1], results[2], results[3], results[4]
    assert at(loop, "s", "orig") and at(loop, "s", "bd")
    assert {e[2].get("bank") for e in at(drums, "s", "bd")} == {"RolandTR909"}
    assert at(half, "speed", 0.5)
    assert at(mash, "s", "a_mix") and at(mash, "s", "b_mix")


def test_mash_up_toggles_set_each_parts_gain_per_section():
    """TAS-87: with toggles, a part plays at gain 0 where it's off and at its level where it's on."""
    from test_strudel import AUDIO, B_AUDIO, BOTH, MA, MB, run_tpl

    code = run_tpl("(() => { const st = strudelMashApply(a, b, strudelMashState(a, b, null, both), 'trade', both);"
                   " const f = strudelFillins(a, { audioUrl: url, canStems: true, mash: st, b: { card: b, audioUrl: burl, canStems: true } });"
                   " return strudelFill(STRUDEL_TEMPLATES.find((t) => t.id === 'mash').text, f).code; })()",
                   a=MA, b=MB, url=AUDIO, burl=B_AUDIO, both=BOTH)
    verse, chorus = play((code, 8, 9), (code, 24, 25))  # bar 9: Verse B; bar 25: Chorus C
    gain = lambda ev, s: {e[2].get("gain") for e in at(ev, "s", s)}  # noqa: E731
    assert gain(verse, "a_vocals") == {1} and gain(verse, "b_vocals") == {0}
    assert gain(chorus, "a_vocals") == {0} and gain(chorus, "b_vocals") == {1}
    assert gain(chorus, "a_drums") == {1} and gain(chorus, "b_drums") == {0}
    plain = run_tpl("(() => { const st = strudelMashApply(a, b, strudelMashState(a, b, null, both), 'trade', both); st.ctl = 'plain';"
                    " return strudelFill(STRUDEL_TEMPLATES.find((t) => t.id === 'mash').text,"
                    " strudelFillins(a, { audioUrl: url, canStems: true, mash: st, b: { card: b, audioUrl: burl, canStems: true } })).code; })()",
                    a=MA, b=MB, url=AUDIO, burl=B_AUDIO, both=BOTH)
    (ch,) = play((plain, 24, 25))
    assert at(ch, "s", "b_vocals") and not at(ch, "s", "a_vocals")


@pytest.mark.parametrize("a_bpm, b_bpm, per", [(82, 162, 0.5), (120, 120, 1), (162, 82, 2)])
def test_mash_up_b_bars_play_back_to_back(a_bpm, b_bpm, per):
    """B in double or half time: its bars follow each other with no gaps and none skipped."""
    from test_strudel import AUDIO, A_SECS, B_AUDIO, B_SECS, BOTH, mash_card, run_tpl

    a, b = mash_card(7, a_bpm, A_SECS), mash_card(8, b_bpm, B_SECS)
    out = run_tpl("(() => { const st = strudelMashState(a, b, null, {}); st.ctl = 'plain'; st.rows.forEach((r) => (r.on = ['b.mix']));"
                  " const m = strudelMashCode(a, b, st, { aUrl: url, bUrl: burl, can: {} }); return [m.audio + '\\n' + m.arrange, b.duration]; })()",
                  a=a, b=b, url=AUDIO, burl=B_AUDIO, both=BOTH)
    code, dur = out
    (ev,) = play((code, 8, 12))  # A's bars 9-12, the first verse; B from its bridge (bars 13-20)
    ev = at(ev, "s", "b_mix")
    assert [e[1] - e[0] for e in ev] == [per] * len(ev) and len(ev) == 4 / per
    assert all(ev[i][1] == ev[i + 1][0] for i in range(len(ev) - 1))  # back to back
    bar = (240 / b_bpm) / dur
    starts = [round((e[2]["begin"] - 0.25 / dur) / bar) for e in ev]
    assert starts == list(range(12, 12 + len(ev)))  # B's bars 13, 14, … in order
