"""The chart's frequency-colored waveform, section loop, cue points and keys (GitHub #3): the math in
static/app/chart-math.js (run in node), the engine's stored bands, and the cue points' API."""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tastr import db
from tastr.web.hosted import Settings
from tastr.web.server import create_app
from test_foundation import full_card
from test_hosted import GOOD, make_client
from ui_src import app_js

MATH = Path(__file__).resolve().parents[1] / "tastr" / "web" / "static" / "app" / "chart-math.js"
INDEX = Path(__file__).resolve().parents[1] / "tastr" / "web" / "static" / "index.html"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run(body: str):
    """Run `body` (an ES module that ends by setting `out`) with chart-math.js imported as M; returns out."""
    script = f"import * as M from {json.dumps(MATH.as_uri())};\nlet out;\n{body}\nconsole.log(JSON.stringify(out));"
    r = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ---- the frequency-colored waveform ----

@needs_node
def test_a_low_sine_draws_red_and_a_high_one_blue():
    out = run("""
    const sr = 22050, n = sr * 2, d = new Float32Array(n);
    // first second: 60 Hz; second second: 6 kHz
    for (let i = 0; i < n; i++) d[i] = 0.5 * Math.sin(2 * Math.PI * (i < sr ? 60 : 6000) * i / sr);
    const bands = M.splitBands(d, sr, 256), avg = M.bandAvg(bands), nb = bands[0].length, half = Math.floor(nb / 2);
    out = { n: nb, low: M.columnColor(bands, avg, 10, half - 10, true), high: M.columnColor(bands, avg, half + 10, nb - 10, true),
            lowLight: M.columnColor(bands, avg, 10, half - 10, false) };
    """)
    assert out["n"] == -(-44100 // 256)
    r, g, b = out["low"]
    assert r == 255 and g < 100 and b < 100, out["low"]
    r, g, b = out["high"]
    assert b == 255 and r < 100, out["high"]
    assert all(v % 15 == 0 for v in out["low"] + out["high"])
    r, g, b = out["lowLight"]  # red stays red on a light page (its luma is under 125)
    assert r >= 240


@needs_node
def test_bright_mixes_are_darkened_on_a_light_page():
    out = run("""
    const flat = [new Float32Array([1, 1]), new Float32Array([1, 1]), new Float32Array([1, 1])], avg = M.bandAvg(flat);
    out = { dark: M.columnColor(flat, avg, 0, 1, true), light: M.columnColor(flat, avg, 0, 1, false) };
    """)
    assert min(out["dark"]) >= 150  # everything at once: near white on a dark plot
    luma = 0.299 * out["light"][0] + 0.587 * out["light"][1] + 0.114 * out["light"][2]
    assert luma <= 130  # grey on a light page (125, rounded to steps of 15)


@needs_node
def test_stored_bands_decode_to_energies():
    out = run("out = Array.from(M.bandsFromBytes(new Uint8Array([255, 0, 128, 0, 255, 255]))[0]);")
    assert out[0] == pytest.approx(1) and out[1] == 0


@needs_node
def test_is_dark_color():
    out = run('out = ["rgb(18, 18, 24)", "rgb(18 18 24)", "rgb(245, 240, 225)", "", "red"].map(M.isDarkColor);')
    assert out == [True, True, False, False, False]


def test_the_engine_stores_bands_per_waveform_block():
    from tastr.engine import rust

    if not rust.available():
        pytest.skip("the Rust engine isn't built")
    sr = 22050
    t = np.arange(sr * 8) / sr
    y = np.where(t < 4, np.sin(2 * np.pi * 60 * t), np.sin(2 * np.pi * 6000 * t)).astype(np.float32) * 0.5
    wf = rust.card_from_samples(y)["extras"]["views"]["waveform"]
    import base64

    mm, bands = base64.b64decode(wf["data"]), np.frombuffer(base64.b64decode(wf["bands"]), np.uint8).reshape(-1, 3)
    assert len(bands) == len(mm) // 2
    half = len(bands) // 2
    lo, hi = bands[10:half - 10].mean(axis=0), bands[half + 10:-10].mean(axis=0)
    assert lo[0] > 200 and lo[2] < 60  # the low sine: lows loud, highs quiet
    assert hi[2] > 200 and hi[0] < 60


# ---- the loop ----

@needs_node
def test_loop_wraps_the_playhead_and_edges_keep_a_bar():
    out = run("""
    const loop = [10, 20];
    out = {
      before: M.loopPos(15, loop, 100), wrap: M.loopPos(23, loop, 100), twice: M.loopPos(35, loop, 100), off: M.loopPos(120, null, 100),
      a: M.dragLoopEdge("a", 19.5, 10, 20, 2, 100), aLow: M.dragLoopEdge("a", -3, 10, 20, 2, 100),
      b: M.dragLoopEdge("b", 10.5, 10, 20, 2, 100), bHigh: M.dragLoopEdge("b", 130, 10, 20, 2, 100), bOk: M.dragLoopEdge("b", 30, 10, 20, 2, 100),
      bar: M.barLen([0, 2, 4, 6]), one: M.barLen([]), barAt: [M.barAt([0, 2, 4, 6], 0), M.barAt([0, 2, 4, 6], 4.5), M.barAt([0, 2, 4, 6], 3.96)],
      snap: [M.beatSnap([0, 0.5, 1], 0.6), M.beatSnap([0, 10], 5)],
    };
    """)
    assert out["before"] == 15 and out["wrap"] == 13 and out["twice"] == 15 and out["off"] == 100
    assert out["a"] == [18, 20] and out["aLow"] == [0, 20]  # one bar (2 s) at least; never before the start
    assert out["b"] == [10, 12] and out["bHigh"] == [10, 100] and out["bOk"] == [10, 30]
    assert out["bar"] == 2 and out["one"] == 2 and out["barAt"] == [1, 3, 3]
    assert out["snap"] == [0.5, 5]


# ---- cue points ----

@needs_node
def test_adding_cues_names_colors_and_caps_them():
    out = run("""
    let list = [];
    const sec = { label: "drop", letter: "C" };
    list = M.addCueTo(list, 30, sec).cues;
    list = M.addCueTo(list, 10, null).cues;
    const dup = M.addCueTo(list, 30.02, sec);
    const rm = list.filter((q) => q.t !== 30);
    const reuse = M.addCueTo(rm, 50, null).cues;
    let full = [];
    for (let i = 0; i < 9; i++) full = M.addCueTo(full, i * 10, null).cues;
    out = { list, dup, reuse, full: full.map((q) => q.color), over: M.addCueTo(full, 200, null) };
    """)
    assert out["list"] == [{"t": 10, "name": "Cue 2", "color": 1}, {"t": 30, "name": "Drop C", "color": 0}]
    assert out["dup"] == {"error": "There's already a cue there."}
    assert [q["color"] for q in out["reuse"]] == [1, 0]  # the first free color
    assert out["full"] == list(range(9))
    assert out["over"]["error"].startswith("You have 9 cues on this track")


@needs_node
def test_keys_skip_typing_and_other_buttons():
    out = run("""
    const k = (key, focus = {}, marking = false, mods = {}) => M.chartKey({ key, ...mods }, focus, marking);
    out = {
      space: k(" "), spaceOnButton: k(" ", { tag: "BUTTON" }), spaceOnChartButton: k(" ", { tag: "BUTTON", inChart: true }),
      spaceOnTab: k(" ", { tag: "SPAN", role: "tab" }), spaceInInput: k(" ", { tag: "INPUT" }), lInTextarea: k("l", { tag: "TEXTAREA" }),
      editable: k("c", { tag: "DIV", editable: true }), loop: k("L"), loopMarking: k("l", {}, true), cue: k("c"),
      cueExact: k("ç", {}, false, { altKey: true, code: "KeyC" }), jump: k("3"), zero: k("0"), ctrl: k("c", {}, false, { ctrlKey: true }),
      meta: k(" ", {}, false, { metaKey: true }), other: k("x"),
    };
    """)
    assert out["space"] == {"act": "toggle"} and out["spaceOnChartButton"] == {"act": "toggle"}
    for k in ("spaceOnButton", "spaceOnTab", "spaceInInput", "lInTextarea", "editable", "loopMarking", "zero", "ctrl", "meta", "other"):
        assert out[k] is None, k
    assert out["loop"] == {"act": "loop"} and out["cue"] == {"act": "cue"} and out["cueExact"] == {"act": "cue-exact"}
    assert out["jump"] == {"act": "jump", "n": 2}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))


def test_cues_replace_list_capped_at_nine(db_path, home):
    conn = db.connect(db_path)
    tid = db.save_card(conn, full_card())
    conn.close()
    c = TestClient(create_app(db_path, Settings()))
    assert c.get(f"/api/tracks/{tid}/cues").json() == []
    cues = [{"t": 30.123, "name": " Drop C ", "color": 0}, {"t": 10, "name": "Intro", "color": 4}]
    got = c.put(f"/api/tracks/{tid}/cues", json=cues).json()
    assert got == [{"t": 10, "name": "Intro", "color": 4}, {"t": 30.12, "name": "Drop C", "color": 0}]
    assert c.get(f"/api/tracks/{tid}/cues").json() == got
    assert c.put(f"/api/tracks/{tid}/cues", json=[{"t": i, "name": "x", "color": 0} for i in range(10)]).status_code == 422
    for bad in ({"t": -1, "name": "x", "color": 0}, {"t": 1, "name": "", "color": 0}, {"t": 1, "name": "x" * 31, "color": 0},
                {"t": 1, "name": "x", "color": 9}, {"t": 1, "name": "x"}):
        assert c.put(f"/api/tracks/{tid}/cues", json=[bad]).status_code == 422, bad
    assert c.get(f"/api/tracks/{tid}/cues").json() == got  # refused lists change nothing
    assert c.put("/api/tracks/999/cues", json=[]).status_code == 404
    # re-analyzing keeps them; deleting the track deletes them
    conn = db.connect(db_path)
    assert db.save_card(conn, full_card()) == tid
    assert len(db.get_cues(conn, None, tid)) == 2
    db.delete_track(conn, tid)
    assert conn.execute("SELECT COUNT(*) FROM track_cues").fetchone()[0] == 0
    conn.close()


def test_cues_are_per_person(db_path, home):
    conn = db.connect(db_path)
    tid = db.save_card(conn, full_card(), owner="friend@example.com")
    db.set_cues(conn, "someone@example.com", tid, [{"t": 1, "name": "Theirs", "color": 0}])
    conn.close()
    c = make_client(db_path)
    assert c.get(f"/api/tracks/{tid}/cues", headers=GOOD).json() == []
    c.put(f"/api/tracks/{tid}/cues", json=[{"t": 2, "name": "Mine", "color": 1}], headers=GOOD)
    conn = db.connect(db_path)
    assert db.get_cues(conn, "someone@example.com", tid) == [{"t": 1, "name": "Theirs", "color": 0}]
    assert db.get_cues(conn, "friend@example.com", tid) == [{"t": 2, "name": "Mine", "color": 1}]
    conn.close()


# ---- the page ----

def test_page_has_the_loop_cues_color_switch_and_help():
    js = app_js()
    for needle in ("⟲ Loop", "＋ Cue", "By frequency", "One color", "tastr-wave-color", "Stop looping", "/cues`",
                   "No cue points yet", "src.loop = true", "initChartKeys", " jump to a cue"):
        assert needle in js, needle
    html = INDEX.read_text()
    for needle in ("<dt><kbd>Space</kbd></dt>", "⟲ Loop", "＋ Cue", "<kbd>1</kbd>–<kbd>9</kbd>"):
        assert needle in html, needle
