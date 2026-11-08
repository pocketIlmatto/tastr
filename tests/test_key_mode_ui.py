"""The card's key when the engine flags its mode as a guess (core.key.mode_uncertain): "C major?" with the other mode
named under it, and an "Other mode" button in the label bar. Run in node on the functions taken from card.js and
daw.js; cards without the flag must show the key exactly as before."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "tastr" / "web" / "static" / "app"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def grab(src: str, name: str) -> str:
    m = re.search(rf"^(?:export )?(const {name} = .*?;|function {name}\(.*?\n}})$", src, re.M | re.S)
    assert m, name
    return m.group(1)


def run(body: str):
    card, daw, lib = ((APP / f).read_text() for f in ("card.js", "daw.js", "library.js"))
    defs = [grab(daw, n) for n in ("KEY_SPELL", "keyName", "modeOther", "camelotParallel")]
    defs += [grab(lib, "camelotNear")]
    defs += [grab(card, n) for n in ("statBtn", "sureness", "MODE_Q_TIP", "keyStat", "keyUnsureStat")]
    defs.append("function stat(value, label, title) { return h(\"div\", { class: \"stat\", ...(title ? { title } : {}) }, h(\"b\", {}, value), h(\"span\", {}, label)); }")
    script = """
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
let filtered = null; const keyFilter = (cam) => { filtered = cam; };
// a tiny DOM stand-in: text() flattens an element the way the browser shows it
const h = (tag, attrs, ...kids) => { const el = { tag, attrs: { ...attrs }, kids: kids.flat().filter((k) => k != null && k !== false), dataset: {},
  setAttribute(k, v) { this.attrs[k] = v; } }; return el; };
const text = (el) => (typeof el === "string" ? el : el.kids.map(text).join(""));
""" + "\n".join(defs) + f"\nlet out;\n{body}\nconsole.log(JSON.stringify(out));"
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_parallel_keys_wrap_round_the_wheel():
    out = run('out = ["8B", "5A", "1B", "3A", "12A", "nope"].map(camelotParallel).concat([modeOther({ tonic: 1, mode: "major" })]);')
    assert out[:6] == ["5A", "8B", "10A", "6B", "3B", None]  # C maj -> C min, A min -> A maj, B maj -> B min, ...
    assert out[6] == {"tonic": 1, "mode": "minor"}


def test_a_mode_guess_shows_the_other_mode():
    out = run("""
    const b = keyStat({ tonic: 0, mode: "major", camelot: "8B", scale: "major", mode_uncertain: true });
    b.attrs.onclick();
    out = { value: text(b.kids[0]), muted: text(b.kids[0].kids[0].kids[2]), cls: b.kids[0].kids[0].kids[2].attrs.class, under: text(b.kids[1]),
            title: b.attrs.title, aria: b.attrs["aria-label"], filtered, unsure: "unsure" in b.dataset };
    """)
    assert out["value"] == "C major?" and out["muted"] == "major?" and out["cls"] == "mode-q"
    assert out["under"] == "8B · or C minor (5A)"
    assert out["title"].startswith("Major or minor is a guess for this track: its third barely sounds")
    assert "about half for rock). If you know, label the key.\nClick to show tracks in 8B" in out["title"]
    assert out["aria"] == "Key: C major or C minor, the mode is a guess. Show tracks in 8B and keys that mix with it."
    assert out["filtered"] == "8B" and out["unsure"]


def test_a_minor_guess_names_its_major_and_keeps_the_calibrated_chance():
    out = run("""
    const b = keyStat({ tonic: 9, mode: "minor", camelot: "8A", mode_uncertain: true, likely_correct: 0.62 });
    out = { value: text(b.kids[0]), under: text(b.kids[1]) };
    """)
    assert out == {"value": "A minor?", "under": "8A · or A major (11B) · 62% likely right"}


@pytest.mark.parametrize("flag", ["", ", mode_uncertain: false"])
def test_without_the_flag_the_key_is_unchanged(flag):
    out = run(f"""
    const b = keyStat({{ tonic: 0, mode: "major", camelot: "8B", scale: "major"{flag} }});
    out = {{ value: text(b.kids[0]), under: text(b.kids[1]), title: b.attrs.title, aria: b.attrs["aria-label"] || null }};
    """)
    assert out == {"value": "C major", "under": "8B · major", "aria": None,
                   "title": "Click to show tracks in 8B and the keys that mix with it (7B, 9B, 8A)."}


def test_the_label_bar_offers_the_other_mode_second():
    src = (APP / "label-bar.js").read_text()
    start = src.index("function keyRow")
    row = src[start:src.index("\n}\n", start)]
    order = [row.index(s) for s in ("✓ ${keyName(key)}", "Other mode: ", "Relative: ", "Fifth up: ", "Fifth down: ")]
    assert order == sorted(order)
    assert "key.mode_uncertain" in row and "camelotParallel(cam0)" in row
    # pressed state and the 🔊 buttons follow the same list, so the extra button lines up with its key
    assert "cam === keyCams[i]" in row and "hearBtn(() => keyCams[i]" in row
