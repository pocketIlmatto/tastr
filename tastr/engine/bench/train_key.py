"""Fit the learned key model and write it into engine-rs/src/consts.rs (KEY_WEIGHTS, KEY_BIAS).

The model is a 24-class logistic regression on the 12-bin key profile (the engine's `music::key_profile`), class =
tonic * 2 + (1 for minor). Every training track is used at all 12 transpositions, so the model learns
what a key sounds like relative to its tonic, not which keys a dataset happens to contain.
Sixteen synthetic progression profiles (`synthetic_tracks`, weighted SYNTH_WEIGHT times a real track) are
added so plain triad progressions are not read as their IV key as often; see docs/bench.md.
scikit-learn is needed only here (librosa already depends on it); the engine uses the fitted constants.

Train from feature caches written by `key_features` (one --data per dataset), check on held-out
halves first, then write the constants:

    python -m tastr.engine.bench.train_key \\
        --data giantsteps-key ~/data/giantsteps-key ~/data/features/giantsteps-key \\
        --data beatport-key ~/data/beatport-key ~/data/features/beatport-key --holdout
    python -m tastr.engine.bench.train_key --data ... --data ... --write

Or from key-profile files (`tastr key-profiles`, which can run on Modal), no audio or cache needed:

    python -m tastr.engine.bench.train_key --profiles gs-key-profiles.json --profiles bp-key-profiles.json --holdout
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import sys
from pathlib import Path

import numpy as np

from . import consts_rs, key_features, metrics

C = 1.0
SYNTH_WEIGHT = 5.0
# Added to the 12 major keys' bias after fitting. GiantSteps and Beatport are 85% and 70% minor, so the fit
# leans minor and reads many non-EDM major songs as minor (FMA Keys); see docs/bench.md "Minor lean".
MAJOR_BONUS = 0.35
# Chords per bar as (root, third) in semitones from the tonic.
SYNTH_PROGRESSIONS = {
    "I-V-vi-IV": ("major", [(0, 4), (7, 4), (9, 3), (5, 4)]),
    "I-IV-V-I": ("major", [(0, 4), (5, 4), (7, 4), (0, 4)]),
    "i-VI-III-VII": ("minor", [(0, 3), (8, 4), (3, 4), (10, 4)]),
    "i-iv-v-i": ("minor", [(0, 3), (5, 3), (7, 3), (0, 3)]),
}
# Tempos and section layouts kept apart from the tests' (124 BPM, 4/8/4 bars), so the key tests stay a check.
SYNTH_RENDERS = [(118, (6, 12, 6)), (128, (3, 6, 3))]
# The key model's input is KEY_INPUT_SCALE * key profile: the profile sums to about 1, so this puts each bin near 1
# for the fitted weights (engine-rs music.rs uses the same scale).
KEY_INPUT_SCALE = 12.0


def key_probabilities(profile: np.ndarray, weights: np.ndarray, bias: np.ndarray) -> np.ndarray:
    """Probability of each of the 24 keys (index tonic * 2 + 1 for minor) from a 12-bin key profile: a softmax
    over the key model, as engine-rs music.rs estimate_key computes it."""
    z = weights @ (KEY_INPUT_SCALE * np.asarray(profile, dtype=float)) + bias
    p = np.exp(z - z.max())
    return p / p.sum()


def estimate_key(profile: np.ndarray, weights: np.ndarray, bias: np.ndarray) -> dict:
    """The most likely key under these weights, and how far it is ahead of the runner-up (the card's key
    confidence)."""
    profile = np.asarray(profile, dtype=float)
    if not np.any(profile > 0):
        return {"tonic": 0, "mode": "major", "confidence": 0.0}
    p = key_probabilities(profile, weights, bias)
    best, second = (int(i) for i in np.argsort(-p)[:2])
    return {"tonic": best // 2, "mode": "minor" if best % 2 else "major", "confidence": round(float(p[best] - p[second]), 3)}


def usable(tracks: list[dict]) -> list[dict]:
    """Drop silent tracks and labels the annotators marked as unsure (Beatport confidence 0 or 1)."""
    return [t for t in tracks if not t["silent"] and np.any(t["profile"] > 0) and t["label_conf"] in (-1, 2)]


def synthetic_tracks() -> list[dict]:
    """Key profiles of synthetic triad progressions (tonics C and A, two tempos each), weighted SYNTH_WEIGHT."""
    from .key_features import features
    from .synth import make_track

    out = []
    for name, (mode, prog) in SYNTH_PROGRESSIONS.items():
        for tonic in (0, 9):
            for bpm, layout in SYNTH_RENDERS:
                y, _ = make_track(bpm=bpm, tonic=tonic, mode=mode, bars_per_section=layout, progression=prog)
                out.append({"id": f"synth-{name}-{tonic}-{bpm}", "dataset": "synth", "profile": features(y)["profile"],
                            "silent": False, "tonic": tonic, "mode": mode, "label_conf": -1, "weight": SYNTH_WEIGHT})
    return out


def fit(tracks: list[dict], c: float = C, major_bonus: float = MAJOR_BONUS) -> tuple[np.ndarray, np.ndarray]:
    """(weights 24 x 12, bias 24) fitted on every track at all 12 transpositions (track weight: t["weight"], default 1),
    with `major_bonus` added to the major keys' bias."""
    from sklearn.linear_model import LogisticRegression

    X, y, w = [], [], []
    for t in tracks:
        for s in range(12):
            X.append(KEY_INPUT_SCALE * np.roll(t["profile"], s))
            y.append(((t["tonic"] + s) % 12) * 2 + (t["mode"] == "minor"))
            w.append(t.get("weight", 1.0))
    clf = LogisticRegression(C=c, max_iter=3000)
    clf.fit(np.array(X), np.array(y), sample_weight=np.array(w))
    assert list(clf.classes_) == list(range(24))
    bias = clf.intercept_.copy()
    bias[0::2] += major_bonus
    return clf.coef_.copy(), bias


def score(tracks: list[dict], weights: np.ndarray, bias: np.ndarray, out: list | None = None,
          dataset: str = "") -> dict:
    rows = []
    for t in tracks:
        est = estimate_key(t["profile"], weights, bias)
        rows.append(metrics.key_scores(est["tonic"], est["mode"], t["tonic"], t["mode"]))
        if out is not None:
            out.append({"id": t.get("id"), "dataset": dataset, "key_confidence": est["confidence"]} | rows[-1])
    n = len(rows) or 1
    return {"n": len(rows), "mirex": sum(r["key_weighted"] for r in rows) / n,
            "exact": sum(r["key_correct"] for r in rows) / n,
            "categories": collections.Counter(r["key_category"] for r in rows)}


def holdout(by_dataset: dict[str, list[dict]], extra: list[dict], c: float = C,
            rows: list | None = None, major_bonus: float = MAJOR_BONUS) -> dict[str, dict]:
    """Split every dataset into alternating halves A/B; train on all A halves (+ `extra`), score the B halves,
    and the reverse. Per-track held-out results are appended to `rows` when given (for calibrate)."""
    A = {k: v[0::2] for k, v in by_dataset.items()}
    B = {k: v[1::2] for k, v in by_dataset.items()}
    wa, ba = fit([t for v in A.values() for t in v] + extra, c, major_bonus)
    wb, bb = fit([t for v in B.values() for t in v] + extra, c, major_bonus)
    out = {}
    for k in by_dataset:
        ra, rb = score(A[k], wb, bb, rows, k), score(B[k], wa, ba, rows, k)
        n = ra["n"] + rb["n"]
        out[k] = {"A": ra["mirex"], "B": rb["mirex"], "n": n,
                  "mirex": (ra["mirex"] * ra["n"] + rb["mirex"] * rb["n"]) / n,
                  "exact": (ra["exact"] * ra["n"] + rb["exact"] * rb["n"]) / n,
                  "categories": ra["categories"] + rb["categories"]}
    return out


def consts_blocks(weights: np.ndarray, bias: np.ndarray, sources: dict[str, int], c: float = C, synth: int = 0,
                  major_bonus: float = MAJOR_BONUS) -> dict[str, str]:
    """KEY_WEIGHTS and KEY_BIAS as Rust source for engine-rs/src/consts.rs, with where they came from."""
    trained = ", ".join(f"{k} ({n} tracks)" for k, n in sources.items())
    if synth:
        trained += f" and {synth} synthetic progressions weighted x{SYNTH_WEIGHT:g}"
    doc = [f"The key model, fitted by tastr.engine.bench.train_key on {datetime.date.today().isoformat()} from {trained};",
           f"logistic regression C={c}, every track at all 12 transpositions, major keys' bias +{major_bonus:g}.",
           "24 x 12: one row per key (index tonic * 2 + 1 for minor), applied to 12 x the key profile."]
    return {"KEY_WEIGHTS": consts_rs.matrix("KEY_WEIGHTS", weights, doc),
            "KEY_BIAS": consts_rs.vector("KEY_BIAS", bias, [])}


def model() -> tuple[np.ndarray, np.ndarray]:
    """The engine's current key model (weights 24 x 12, bias 24), read from consts.rs."""
    return consts_rs.read("KEY_WEIGHTS").reshape(24, 12), consts_rs.read("KEY_BIAS")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", nargs=3, action="append", default=[], metavar=("DATASET", "ROOT", "FEATURES"))
    ap.add_argument("--profiles", action="append", default=[], metavar="FILE",
                    help="a key-profile file from `tastr key-profiles` (instead of --data)")
    ap.add_argument("--holdout", action="store_true", help="report alternating-half held-out scores")
    ap.add_argument("--calibration-rows", metavar="JSON",
                    help="with --holdout, save per-track held-out confidence and result here for bench.calibrate")
    ap.add_argument("--write", action="store_true", help="fit on everything and write the model into engine-rs/src/consts.rs")
    ap.add_argument("-C", type=float, default=C)
    ap.add_argument("--major-bonus", type=float, default=MAJOR_BONUS, help="added to the major keys' bias")
    ap.add_argument("--no-synth", action="store_true", help="leave out the synthetic progressions")
    a = ap.parse_args(argv)

    if not a.data and not a.profiles:
        ap.error("give --data or --profiles")
    by_dataset = {name: usable(key_features.load(name, root, feats)) for name, root, feats in a.data}
    for f in a.profiles:
        for name, rows in key_features.read_profiles(f).items():
            by_dataset[name] = by_dataset.get(name, []) + usable(rows)
    extra = [] if a.no_synth else synthetic_tracks()
    for k, v in by_dataset.items():
        print(f"{k}: {len(v)} training tracks")
    print(f"synthetic: {len(extra)} progressions")
    if a.holdout:
        rows: list[dict] = []
        for k, r in holdout(by_dataset, extra, a.C, rows, a.major_bonus).items():
            cats = " ".join(f"{c} {r['categories'][c]}" for c in ("fifth", "relative", "parallel", "other"))
            print(f"held out {k}: A {r['A']:.3f}, B {r['B']:.3f}, all {r['mirex']:.3f} (n={r['n']}), "
                  f"exact {r['exact']:.3f} | {cats}")
        if a.calibration_rows:
            Path(a.calibration_rows).expanduser().write_text(json.dumps({"rows": rows}, indent=1))
            print(f"wrote {len(rows)} held-out rows to {a.calibration_rows}")
    if a.write:
        weights, bias = fit([t for v in by_dataset.values() for t in v] + extra, a.C, a.major_bonus)
        consts_rs.write(consts_blocks(weights, bias, {k: len(v) for k, v in by_dataset.items()}, a.C, len(extra),
                                      a.major_bonus))
        print(f"wrote {consts_rs.PATH}; rebuild the engine (cd engine-rs && cargo build --release)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
