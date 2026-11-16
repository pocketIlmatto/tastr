"""The card's earlier analyses (card-history.js): the "Changed in the core 0.6.0 update" line when the last
re-analysis moved a number, and Metadata › Earlier analyses. Run in node on the module, with a tiny DOM stand-in."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


APP = Path(__file__).resolve().parents[1] / "tastr" / "web" / "static" / "app"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

STUBS = """
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const h = (tag, attrs, ...kids) => ({ tag, attrs: { ...attrs }, kids: kids.flat().filter((k) => k != null && k !== false) });
const text = (el) => (typeof el === "string" ? el : el.kids.map(text).join(""));
const find = (el, pred, out = []) => { if (typeof el !== "string") { if (pred(el)) out.push(el); el.kids.forEach((k) => find(k, pred, out)); } return out; };
const rows = (el) => find(el, (e) => e.tag === "tr").slice(1).map((tr) => tr.kids.map((td) => text(td) + (td.attrs.class === "hist-diff" ? "*" : "")));
const $ = () => ({}); const api = async () => []; const document = {};
"""


def run(body: str):
    src = (APP / "card-history.js").read_text()
    src = re.sub(r"^import .*?;\n", "", src, flags=re.M).replace("export ", "")
    r = subprocess.run(["node", "-e", STUBS + src + f"\nlet out;\n{body}\nconsole.log(JSON.stringify(out));"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


CARD = """const card = (history, core = { bpm: 118.79, key: { camelot: "8A" }, energy: 0.61 }) => ({ id: 7, core,
  stages_run: { core: "0.6.0" }, metadata: { Analysis: { "Analyzed at": "2026-11-12T09:30:00+00:00" } }, history });
const old = (o) => ({ id: 1, analyzed_at: "2026-11-03T08:00:00+00:00", replaced_at: "2026-11-12T09:30:00+00:00",
  core_version: "0.5.0", bpm: 117.45, key_name: "A minor", camelot: "8A", energy: 0.61, ...o });
"""


def test_a_moved_bpm_shows_the_line_with_old_and_new():
    out = run(CARD + """const l = historyLine(card([old()]));
out = [text(l), l.attrs.hidden, l.attrs.title, find(l, (e) => e.tag === "s").map(text), find(l, (e) => e.tag === "button").map(text)];""")
    assert out[0] == "Changed in the core 0.6.0 update: BPM 117.45 → 118.79 · Earlier analyses"
    assert out[1] is None
    assert out[2] == "Re-analyzed Nov 12, 2026. The earlier card was from Nov 3, 2026, engine core 0.5.0."
    assert out[3] == ["117.45"] and out[4] == ["Earlier analyses"]


def test_key_and_energy_moves_and_a_same_core_reanalysis():
    out = run(CARD + 'out = text(historyLine(card([old({ core_version: "0.6.0", bpm: 118.8, camelot: "9A", energy: 0.5 })])));')
    assert out == "Changed in re-analysis on Nov 12, 2026: Key 9A → 8A · Energy 0.50 → 0.61 · Earlier analyses"


def test_no_line_when_nothing_moved_but_the_table_is_there():
    out = run(CARD + """const c = card([old({ bpm: 118.76, energy: 0.6, camelot: "8A" })]);
const l = historyLine(c), b = historyBlock(c); out = [l.attrs.hidden, b.attrs.hidden, rows(b), text(b).includes("Tastr keeps the last 5")];""")
    assert out[0] == ""  # hidden
    assert out[1] is None
    assert out[2] == [["Now", "Nov 12, 2026", "core 0.6.0", "118.79", "8A", "0.61"],
                      ["Earlier", "Nov 3, 2026", "core 0.5.0", "118.76*", "8A", "0.60*"]]
    assert out[3]


def test_table_marks_values_that_differ_from_the_row_above_but_never_a_dash():
    out = run(CARD + """const b = historyBlock(card([old({ energy: null }), old({ id: 0, core_version: "0.4.2", bpm: 234.9, camelot: "8A", energy: null })]));
out = rows(b);""")
    assert out == [["Now", "Nov 12, 2026", "core 0.6.0", "118.79", "8A", "0.61"],
                   ["Earlier", "Nov 3, 2026", "core 0.5.0", "117.45*", "8A", "–"],
                   ["Earlier", "Nov 3, 2026", "core 0.4.2", "234.90*", "8A", "–"]]


def test_no_history_says_none_yet_and_loading_shows_nothing():
    out = run(CARD + """const b = historyBlock(card([])); const l = historyLine(card([]));
out = [rows(b), text(b), l.attrs.hidden, historyBlock(card(undefined)).attrs.hidden, historyBlock(card(null)).attrs.hidden];""")
    assert out[0] == []
    assert out[1] == "Earlier analysesNone yet. When this track is re-analyzed, Tastr keeps the card it had, the last 5."
    assert out[2:] == ["", "", ""]


def test_card_wires_the_history_in():
    js = (APP / "card.js").read_text()
    for needle in ("historyLine(c)", "historyBlock(c), lookupTable(c)", "loadHistory(c)", "fetchHistory(id)"):
        assert needle in js, needle
