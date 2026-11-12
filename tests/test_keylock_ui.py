"""The mash-up's "Mix with" line with key lock (T70, sandbox v213): working, ready (fits or clashes), and without a
lock, the speed change that pulls a matching key out of tune. Runs fitLine from strudel-tpl.js in node with
strudel.js loaded."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "tastr" / "web" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def line(a_bpm, a_cam, b_bpm, b_cam, lock=None):
    src = (STATIC / "app" / "strudel-tpl.js").read_text()
    fn = re.search(r"^function fitLine\(.*?\n}$", src, re.M | re.S).group(0)
    script = (STATIC / "strudel.js").read_text() + """
const h = (tag, attrs, ...kids) => ({ tag, attrs, kids });
const text = (k) => (typeof k === "string" ? k : k.kids.map(text).join(""));
""" + fn + f"""
const a = {{ core: {{ bpm: {a_bpm}, key: {{ camelot: "{a_cam}" }} }} }}, t = {{ bpm: {b_bpm}, camelot: "{b_cam}" }}, lock = {json.dumps(lock)};
const fx = strudelMashFit({{ bpm: a.core.bpm, camelot: "{a_cam}" }}, t, !!lock);
const [cls, ...kids] = fitLine(a, t, fx, lock);
console.log(JSON.stringify([cls, kids.map(text).join(""), kids.some((k) => k.attrs && k.attrs.class === "spin")]));
"""
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_ready_and_same_key_is_green():
    cls, text, _ = line(123, "4A", 117.45, "4A", {"state": "ready"})
    assert cls == "st-fit ok" and text == "B is stretched to A's tempo (×1.047) with its key kept: 4A over 4A, same key."


def test_ready_and_clashing_is_red_with_the_hint():
    cls, text, _ = line(123, "4A", 117.45, "9A", {"state": "ready"})
    assert cls == "st-fit bad"
    assert text == "B is stretched to A's tempo with its key kept, but 9A over 4A clashes. Tracks marked ✓ fit better; they're listed first."


def test_working_is_muted_with_a_spinner():
    cls, text, spin = line(123, "4A", 117.45, "4A", {"state": "running"})
    assert cls == "st-fit wait" and spin
    assert text == "Key-locking B at A's tempo (×1.047) so its pitch stays put… Until it's ready, the code speeds B up, which raises it 0.8 semitones."


def test_no_lock_same_key_pulled_out_of_tune():
    cls, text, _ = line(123, "4A", 117.45, "4A")
    assert cls == "st-fit bad"
    assert text == "Both are 4A, but speeding B up ×1.047 to match A raises it 0.8 semitones, so it won't sit in key."


def test_no_lock_small_shift_onto_a_fitting_key_is_green():
    cls, text, _ = line(120, "8A", 118, "8A")
    assert cls == "st-fit ok" and text.endswith("to about 8A: same key.")


def test_no_lock_between_keys():
    cls, text, _ = line(123, "8A", 119, "1A")
    assert cls == "st-fit bad" and "semitones, between keys. Tracks marked ✓" in text
