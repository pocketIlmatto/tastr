"""Drum lanes on synthetic loops with known patterns (tastr.engine.drums via the suggestions stage's grid)."""

import json

import pytest

from tastr.engine import drums
from tastr.engine.bench.drums import _synth_ctx, run_synth
from tastr.engine.bench.synth import make_drum_track
from tastr.engine.stages.suggestions import _drum_grid, _step_grid

pytestmark = pytest.mark.audio


def grid_for(patterns, bpm=120):
    y, _ = make_drum_track(patterns, bpm=bpm)
    ctx = _synth_ctx(y, bpm)
    return _drum_grid(ctx, _step_grid(ctx, 1 + len(y) // ctx.hop))


def hits(g):
    return g["pattern"].replace("o", "x") if g["detected"] else "." * 16


def test_hats_do_not_leak_into_snare():
    # The old band split read off-beat hats as snare hits.
    g = grid_for({"kick": "x...x...x...x...", "hat": "..x...x...x...x."})
    assert hits(g["kick"]) == "x...x...x...x..."
    assert hits(g["hat"]) == "..x...x...x...x."
    assert not g["snare"]["detected"] and not g["clap"]["detected"]


def test_snare_and_clap_told_apart():
    snare = grid_for({"kick": "x.......x.......", "snare": "....x.......x...", "hat": "x.x.x.x.x.x.x.x."})
    assert hits(snare["snare"]) == "....x.......x..."
    assert not snare["clap"]["detected"]
    clap = grid_for({"kick": "x.......x.......", "clap": "....x.......x...", "hat": "x.x.x.x.x.x.x.x."})
    assert hits(clap["clap"]) == "....x.......x..."


def test_open_and_closed_hats():
    g = grid_for({"kick": "x...x...x...x...", "hat": "x...x...x...x...", "open_hat": "..x...x...x...x."})
    assert hits(g["open_hat"]) == "..x...x...x...x."
    assert hits(g["hat"]) == "x...x...x...x..."


def test_toms_are_not_kicks():
    g = grid_for({"kick": "x.......x.......", "snare": "....x.......x...", "tom": "..........x...x."})
    assert hits(g["kick"]) == "x.......x......."
    assert g["tom"]["detected"] and hits(g["tom"])[10] == "x"


def test_lane_help_covers_every_lane():
    assert set(drums.LANE_HELP) == set(drums.LANES)


def test_synth_suite_floor():
    """Guards against regressions; the numbers in docs/bench.md are the current scores."""
    s = run_synth(bpms=(120,))["summary"]
    json.dumps(s)
    assert s["kick"]["f"] >= 0.9
    assert s["clap"]["f"] >= 0.8
    assert s["hat"]["f"] >= 0.7
    for lane in ("snare", "open_hat", "tom", "perc"):
        assert s[lane]["f"] >= 0.4, (lane, s[lane])
