"""Explore > Blend's math (static/app/blend-space.js), run in node on a few fixed tracks."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SPACE = Path(__file__).resolve().parents[1] / "tastr" / "web" / "static" / "app" / "blend-space.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run(body: str):
    """Run `body` (an ES module that ends by setting `out`) with blend-space.js imported as S; returns out."""
    script = f"import * as S from {json.dumps(SPACE.as_uri())};\nlet out;\n{body}\nconsole.log(JSON.stringify(out));"
    r = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def track(i, bpm, cam, energy, **vibes):
    return {"id": i, "title": f"T{i}", "bpm": bpm, "camelot": cam, "energy": energy, "vibes": vibes}


LIB = [track(1, 70, "8A", 0.2, dark=0.9), track(2, 140, "8A", 0.25, dark=0.8), track(3, 128, "1A", 0.8, danceable=0.9),
       track(4, 126, "12A", 0.85, danceable=0.8), track(5, 100, "5B", 0.5), track(6, 174, "8A", 0.3, dark=0.7)]


def test_tempo_folds_by_octave_and_key_wraps_around_the_wheel():
    out = run(f"""
    const L = {json.dumps(LIB)}, d = (a, b) => S.dist(v[a], v[b]);
    let v = S.blendSpace(L, {{ tempo: 1 }});
    const tempo = {{ octave: d(0, 1), near: d(2, 3), far: d(0, 4) }};
    v = S.blendSpace(L, {{ key: 1 }});
    out = {{ tempo, key: {{ wrap: d(2, 3), same: d(0, 1), across: d(0, 2), mode: d(0, 4) }}, dims: v[0].length }};
    """)
    assert out["tempo"]["octave"] < 1e-9  # 70 sits on 140
    assert out["tempo"]["near"] < out["tempo"]["far"]
    assert out["key"]["same"] < 1e-9 and out["key"]["wrap"] < out["key"]["across"]  # 12A next to 1A, 8A far from 1A
    assert out["dims"] == 2 + 3 + 1 + 5  # every dimension keeps its parts; weight 0 just zeroes them


def test_kmeans_is_deterministic_and_groups_alike_tracks():
    out = run(f"""
    const L = {json.dumps(LIB)}, v = S.blendSpace(L, S.defaultWeights());
    out = [S.kmeans(v, 2), S.kmeans(v, 2), S.kmeans(v, 3)];
    """)
    assert out[0] == out[1]
    lab = out[0]
    assert lab[2] == lab[3] and lab[0] != lab[2]  # the two energetic 126-128 BPM tracks group together
    assert len(set(out[2])) == 3


def test_pca2_keeps_the_main_direction():
    out = run("""
    const v = [[0, 0, 0], [1, 0.1, 0], [2, -0.1, 0], [3, 0, 0]], P = S.pca2(v);
    out = { pts: P.pts, proj: P.project([1.5, 0, 0]) };
    """)
    xs = [p[0] for p in out["pts"]]
    assert abs(abs(xs[3] - xs[0]) - 3) < 1e-3  # the spread along the first axis is the line's length
    assert all(abs(p[1]) < 0.2 for p in out["pts"])
    assert abs(out["proj"][0] - (xs[0] + xs[3]) / 2) < 1e-3


def test_group_summary_names_what_stands_out():
    out = run(f"""
    const L = {json.dumps(LIB)}, w = S.defaultWeights();
    out = [S.groupSummary(L, [1, 1, 0, 0, 1, 1], 0, w), S.groupSummary(L, [0, 0, 0, 0, 0, 0], 0, w)];
    """)
    assert out[0].endswith("· 2 tracks · around 128 BPM") and "higher energy" in out[0]
    assert out[1].startswith("middle of the library · 6 tracks")


def test_path_takes_the_nearest_unused_track_at_each_step():
    out = run("""
    const v = [[0], [10], [5], [2], [8], [5.1], [1.5]];
    out = [S.makePath(v, 0, 1), S.makePath(v, 0, 1, 2)];
    """)
    # 5 steps at 1/6 .. 5/6 of the way (1.67, 3.33, 5, 6.67, 8.33): each the nearest track not used yet
    assert out[0] == [6, 3, 2, 4, 5]  # 6.67 takes 8 (closer than 5.1), so 8.33 gets what's left
    assert out[1] == [3, 4]  # 2 steps: 3.33 -> 2, then 6.67 -> 8


def test_compare_lists_reads_separation_and_differences():
    out = run(f"""
    const L = {json.dumps(LIB)}, w = {{ energy: 1, dark: 1 }}, v = S.blendSpace(L, w);
    const r = S.compareLists(L, v, [0, 1, 5], [2, 3], w, "Peak");
    out = {{ share: r.share, apart: r.apart, rows: r.rows.map((x) => [x.d.k, x.words]) }};
    """)
    assert out["share"] == 1 and out["apart"] == "Easy to tell apart"
    assert [k for k, _ in out["rows"]] == ["energy", "dark"] or [k for k, _ in out["rows"]] == ["dark", "energy"]
    assert dict(out["rows"])["energy"] == "Peak is a lot more energetic"
