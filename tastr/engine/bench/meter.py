"""Meter benchmark: does core tell 3/4 from 4/4, and find bar 1 in 3/4?

Scores core's `_meter_score` and the 3-beat `_downbeat_phase` on the labeled beats of a "Beat This!"
dataset (see `bench.downbeats` for the files), so beat-tracking errors don't blur it. Tracks whose bars
are steadily 3 or 4 beats long are scored; meter changes, 2/4, 6/8 and the rest are left out.

    python -m tastr.engine.bench.meter ~/data/beat_this --dataset gtzan
"""

from __future__ import annotations

import argparse
import sys
from functools import partial

import numpy as np

from .downbeats import FPS, bar_length, mel_freqs
from .downbeats import load as _load

load = partial(_load, meters=(3, 4))


def run(items) -> dict:
    from ..stages.core import METER_THRESHOLD, _beat_cues, _downbeat_phase, _meter_score

    freqs = mel_freqs()
    rows = []
    for name, log_mel, beats, pos in items:
        frames = np.round(beats * FPS).astype(int)
        cues = _beat_cues(frames, log_mel, freqs)
        m = bar_length(pos)
        score = _meter_score(frames, log_mel, freqs, cues=cues)
        row = {"name": name, "meter": m, "score": round(score, 3), "guess": 3 if score > METER_THRESHOLD else 4}
        if m == 3:  # with the meter known, is bar 1 right?
            p = _downbeat_phase(frames, log_mel, freqs, 3, cues=cues)
            row["bar1_right"] = bool((pos[p::3] == 1).mean() > 0.5)
        rows.append(row)
    three = [r for r in rows if r["meter"] == 3]
    four = [r for r in rows if r["meter"] == 4]

    def share(rs, test):
        return round(sum(map(test, rs)) / len(rs), 3) if rs else None

    return {"tracks": len(rows), "three": len(three), "four": len(four), "threshold": METER_THRESHOLD,
            "three_found": share(three, lambda r: r["guess"] == 3),
            "four_kept": share(four, lambda r: r["guess"] == 4),
            "three_bar1_right": share(three, lambda r: r["bar1_right"]),
            "rows": rows}


def format_report(r: dict) -> str:
    def pct(v):
        return "n/a" if v is None else f"{v:.3f}"

    return (f"tracks {r['tracks']} ({r['three']} in 3/4, {r['four']} in 4/4), threshold {r['threshold']}\n"
            f"  3/4 found       {pct(r['three_found'])}\n"
            f"  4/4 kept as 4/4 {pct(r['four_kept'])}\n"
            f"  3/4 bar 1 right {pct(r['three_bar1_right'])} (meter given)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tastr.engine.bench.meter", description=__doc__.split("\n\n")[0])
    ap.add_argument("path", help="folder with <dataset>.npz and beat_this_annotations-1.0/")
    ap.add_argument("--dataset", default="gtzan")
    ap.add_argument("--limit", type=int, help="first N tracks (after a fixed shuffle)")
    args = ap.parse_args(argv)
    print(format_report(run(load(args.path, args.dataset, args.limit))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
