"""Turn a raw BPM or key confidence into the chance the answer is right.

The raw scores only say how clear the evidence was in one track: the BPM score is how steady and
pronounced the beat is, the key score how far the top key is ahead of the runner-up. Neither is a
probability. The tables in `calibration_table.py` map a raw score to the share of benchmark tracks with
that score the engine got right (BPM within 4% of the label, the exact key), so "70%" means about 7 in
10 such tracks were right. They are fitted by `tastr.engine.bench.calibrate`; see docs/bench.md
"Confidence calibration". Refit them after changing the tempo or key logic.
"""

from __future__ import annotations

import numpy as np


def likely_correct(kind: str, raw: float | None) -> float | None:
    """Calibrated chance (0..1) that a `kind` ("bpm" or "key") answer with raw confidence `raw` is right.

    None when there is no score or no fitted table. Scores between knots are interpolated; scores
    outside the fitted range take the nearest knot's value.
    """
    from . import calibration_table

    knots = getattr(calibration_table, kind.upper(), ())
    if raw is None or not knots:
        return None
    xs = [k[0] for k in knots]
    ys = [k[1] for k in knots]
    return round(float(np.interp(raw, xs, ys)), 3)
