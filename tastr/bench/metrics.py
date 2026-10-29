"""Scoring estimates against reference labels.

These follow the usual MIR evaluation conventions (as in mir_eval / MIREX) so
numbers are comparable with published results, but are implemented here to
avoid another dependency.
"""

from __future__ import annotations

import numpy as np

TEMPO_TOLERANCE = 0.04  # 4% either side, the standard Acc1/Acc2 tolerance
TEMPO_FACTORS = (1.0, 2.0, 0.5, 3.0, 1 / 3)
BEAT_WINDOW = 0.07  # seconds, the standard beat F-measure window

# MIREX weighted key score per error category.
KEY_WEIGHTS = {"correct": 1.0, "fifth": 0.5, "relative": 0.3, "parallel": 0.2, "other": 0.0}


def tempo_scores(est: float | None, ref: float) -> dict:
    """Acc1: within 4% of the reference. Acc2: also allows double/half/triple/third.

    `octave` names the factor that matched when only Acc2 holds (e.g. "x2" means
    the estimate is twice the reference), which is the most common BPM error.
    """
    if not est or not ref:
        return {"acc1": 0.0, "acc2": 0.0, "octave": None}
    hits = [f for f in TEMPO_FACTORS if abs(est - f * ref) <= TEMPO_TOLERANCE * f * ref]
    acc1 = 1.0 in hits
    octave = None
    if hits and not acc1:
        f = hits[0]
        octave = f"x{int(f)}" if f >= 1 else f"/{round(1 / f)}"
    return {"acc1": float(acc1), "acc2": float(bool(hits)), "octave": octave}


def key_category(est_tonic: int, est_mode: str, ref_tonic: int, ref_mode: str) -> str:
    """MIREX key error category of an estimate relative to the reference."""
    d = (est_tonic - ref_tonic) % 12
    if est_mode == ref_mode:
        if d == 0:
            return "correct"
        if d in (5, 7):
            return "fifth"
        return "other"
    if ref_mode == "minor" and d == 3:  # e.g. ref A minor, est C major
        return "relative"
    if ref_mode == "major" and d == 9:  # e.g. ref C major, est A minor
        return "relative"
    if d == 0:
        return "parallel"
    return "other"


def key_scores(est_tonic: int | None, est_mode: str | None, ref_tonic: int, ref_mode: str) -> dict:
    if est_tonic is None or est_mode is None:
        return {"key_weighted": 0.0, "key_correct": 0.0, "key_category": "missing"}
    cat = key_category(est_tonic, est_mode, ref_tonic, ref_mode)
    return {"key_weighted": KEY_WEIGHTS[cat], "key_correct": float(cat == "correct"), "key_category": cat}


def match_events(est: list[float], ref: list[float], window: float) -> int:
    """Greedy one-to-one matching of event times within `window` seconds.

    Both lists are sorted, so a two-pointer walk gives the maximum matching
    whenever the window is smaller than half the gap between events, which holds
    for beats and section boundaries.
    """
    est, ref = sorted(est), sorted(ref)
    i = j = hits = 0
    while i < len(est) and j < len(ref):
        if abs(est[i] - ref[j]) <= window:
            hits += 1
            i += 1
            j += 1
        elif est[i] < ref[j]:
            i += 1
        else:
            j += 1
    return hits


def f_measure(est: list[float], ref: list[float], window: float) -> dict:
    if not ref:
        return {"precision": 0.0, "recall": 0.0, "f": 0.0}
    if not est:
        return {"precision": 0.0, "recall": 0.0, "f": 0.0}
    hits = match_events(est, ref, window)
    p, r = hits / len(est), hits / len(ref)
    return {"precision": p, "recall": r, "f": 2 * p * r / (p + r) if p + r else 0.0}


def beat_scores(est: list[float], ref: list[float], prefix: str = "beat", skip: float = 5.0) -> dict:
    """Beat F-measure at 70 ms, ignoring the first `skip` seconds as MIREX does."""
    est = [t for t in est if t >= skip]
    ref = [t for t in ref if t >= skip]
    return {f"{prefix}_f": f_measure(est, ref, BEAT_WINDOW)["f"]}


def boundary_scores(est: list[float], ref: list[float], duration: float | None = None) -> dict:
    """Section boundary hit rate at 0.5 s and 3 s.

    Only internal boundaries count: the track start and end are free hits for
    any method, so including them inflates scores on short tracks.
    """
    end = duration if duration else max(ref + est + [0.0])

    def inner(ts):
        return sorted(t for t in ts if 1.0 < t < end - 1.0)

    e, r = inner(est), inner(ref)
    return {
        "boundary_f_0.5": f_measure(e, r, 0.5)["f"],
        "boundary_f_3": f_measure(e, r, 3.0)["f"],
        "n_sections_est": len(e) + 1,
        "n_sections_ref": len(r) + 1,
    }


def mean(values) -> float | None:
    vals = [v for v in values if v is not None]
    return float(np.mean(vals)) if vals else None
