"""Fit the learned key model and write tastr/engine/key_model.py.

The model is a 24-class logistic regression on the 12-bin key profile (`music.key_profile`), class =
tonic * 2 + (1 for minor). Every training track is used at all 12 transpositions, so the model learns
what a key sounds like relative to its tonic, not which keys a dataset happens to contain.
Sixteen synthetic progression profiles (`synthetic_tracks`, weighted SYNTH_WEIGHT times a real track) are
added so plain triad progressions are not read as their IV key as often; see docs/bench.md.
scikit-learn is needed only here (librosa already depends on it); the engine uses the fitted constants.

Train from feature caches written by `key_features` (one --data per dataset), check on held-out
halves first, then write the module:

    python -m tastr.engine.bench.train_key \\
        --data giantsteps-key ~/data/giantsteps-key ~/data/features/giantsteps-key \\
        --data beatport-key ~/data/beatport-key ~/data/features/beatport-key --holdout
    python -m tastr.engine.bench.train_key --data ... --data ... --write
"""

from __future__ import annotations

import argparse
import collections
import datetime
import sys
from pathlib import Path

import numpy as np

from .. import music
from . import key_features, metrics

C = 1.0
SYNTH_WEIGHT = 5.0
# Chords per bar as (root, third) in semitones from the tonic.
SYNTH_PROGRESSIONS = {
    "I-V-vi-IV": ("major", [(0, 4), (7, 4), (9, 3), (5, 4)]),
    "I-IV-V-I": ("major", [(0, 4), (5, 4), (7, 4), (0, 4)]),
    "i-VI-III-VII": ("minor", [(0, 3), (8, 4), (3, 4), (10, 4)]),
    "i-iv-v-i": ("minor", [(0, 3), (5, 3), (7, 3), (0, 3)]),
}
# Tempos and section layouts kept apart from the tests' (124 BPM, 4/8/4 bars), so the key tests stay a check.
SYNTH_RENDERS = [(118, (6, 12, 6)), (128, (3, 6, 3))]
MODULE = Path(__file__).resolve().parent.parent / "key_model.py"


def usable(tracks: list[dict]) -> list[dict]:
    """Drop silent tracks and labels the annotators marked as unsure (Beatport confidence 0 or 1)."""
    return [t for t in tracks if not t["silent"] and np.any(t["profile"] > 0) and t["label_conf"] in (-1, 2)]


def synthetic_tracks() -> list[dict]:
    """Key profiles of synthetic triad progressions (tonics C and A, two tempos each), weighted SYNTH_WEIGHT."""
    from .. import signals
    from ..pipeline import SR, AnalysisContext
    from .synth import make_track

    out = []
    for name, (mode, prog) in SYNTH_PROGRESSIONS.items():
        for tonic in (0, 9):
            for bpm, layout in SYNTH_RENDERS:
                y, _ = make_track(bpm=bpm, tonic=tonic, mode=mode, bars_per_section=layout, progression=prog)
                chroma = AnalysisContext(y=y, sr=SR, card=None).get("chroma", signals.chroma)
                out.append({"id": f"synth-{name}-{tonic}-{bpm}", "dataset": "synth", "profile": music.key_profile(chroma),
                            "silent": False, "tonic": tonic, "mode": mode, "label_conf": -1, "weight": SYNTH_WEIGHT})
    return out


def fit(tracks: list[dict], c: float = C) -> tuple[np.ndarray, np.ndarray]:
    """(weights 24 x 12, bias 24) fitted on every track at all 12 transpositions (track weight: t["weight"], default 1)."""
    from sklearn.linear_model import LogisticRegression

    X, y, w = [], [], []
    for t in tracks:
        for s in range(12):
            X.append(music.KEY_INPUT_SCALE * np.roll(t["profile"], s))
            y.append(((t["tonic"] + s) % 12) * 2 + (t["mode"] == "minor"))
            w.append(t.get("weight", 1.0))
    clf = LogisticRegression(C=c, max_iter=3000)
    clf.fit(np.array(X), np.array(y), sample_weight=np.array(w))
    assert list(clf.classes_) == list(range(24))
    return clf.coef_.copy(), clf.intercept_.copy()


def score(tracks: list[dict], weights: np.ndarray, bias: np.ndarray) -> dict:
    rows = []
    for t in tracks:
        est = music.estimate_key(t["profile"], weights, bias)
        rows.append(metrics.key_scores(est["tonic"], est["mode"], t["tonic"], t["mode"]))
    n = len(rows) or 1
    return {"n": len(rows), "mirex": sum(r["key_weighted"] for r in rows) / n,
            "exact": sum(r["key_correct"] for r in rows) / n,
            "categories": collections.Counter(r["key_category"] for r in rows)}


def holdout(by_dataset: dict[str, list[dict]], extra: list[dict], c: float = C) -> dict[str, dict]:
    """Split every dataset into alternating halves A/B; train on all A halves (+ `extra`), score the B halves,
    and the reverse."""
    A = {k: v[0::2] for k, v in by_dataset.items()}
    B = {k: v[1::2] for k, v in by_dataset.items()}
    wa, ba = fit([t for v in A.values() for t in v] + extra, c)
    wb, bb = fit([t for v in B.values() for t in v] + extra, c)
    out = {}
    for k in by_dataset:
        ra, rb = score(A[k], wb, bb), score(B[k], wa, ba)
        n = ra["n"] + rb["n"]
        out[k] = {"A": ra["mirex"], "B": rb["mirex"], "n": n,
                  "mirex": (ra["mirex"] * ra["n"] + rb["mirex"] * rb["n"]) / n,
                  "exact": (ra["exact"] * ra["n"] + rb["exact"] * rb["n"]) / n,
                  "categories": ra["categories"] + rb["categories"]}
    return out


def module_source(weights: np.ndarray, bias: np.ndarray, sources: dict[str, int], c: float = C,
                  synth: int = 0) -> str:
    rows = ",\n".join("    [" + ", ".join(repr(float(v)) for v in r) + "]" for r in weights)
    trained = ", ".join(f"{k} ({n} tracks)" for k, n in sources.items())
    if synth:
        trained += f" and {synth} synthetic progressions weighted x{SYNTH_WEIGHT:g}"
    return f'''"""Learned key model constants. Generated by tastr.engine.bench.train_key; do not edit by hand.

Generated {datetime.date.today().isoformat()} from {trained};
logistic regression C={c}, every track at all 12 transpositions. See music.estimate_key and docs/bench.md "Key detection notes".
"""

import numpy as np

# 24 x 12: one row per key (index tonic * 2 + 1 for minor), applied to KEY_INPUT_SCALE * key profile.
WEIGHTS = np.array([
{rows},
])

BIAS = np.array([{", ".join(repr(float(v)) for v in bias)}])
'''


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", nargs=3, action="append", required=True, metavar=("DATASET", "ROOT", "FEATURES"))
    ap.add_argument("--holdout", action="store_true", help="report alternating-half held-out scores")
    ap.add_argument("--write", action="store_true", help=f"fit on everything and write {MODULE.name}")
    ap.add_argument("-C", type=float, default=C)
    ap.add_argument("--no-synth", action="store_true", help="leave out the synthetic progressions")
    a = ap.parse_args(argv)

    by_dataset = {name: usable(key_features.load(name, root, feats)) for name, root, feats in a.data}
    extra = [] if a.no_synth else synthetic_tracks()
    for k, v in by_dataset.items():
        print(f"{k}: {len(v)} training tracks")
    print(f"synthetic: {len(extra)} progressions")
    if a.holdout:
        for k, r in holdout(by_dataset, extra, a.C).items():
            cats = " ".join(f"{c} {r['categories'][c]}" for c in ("fifth", "relative", "parallel", "other"))
            print(f"held out {k}: A {r['A']:.3f}, B {r['B']:.3f}, all {r['mirex']:.3f} (n={r['n']}), "
                  f"exact {r['exact']:.3f} | {cats}")
    if a.write:
        weights, bias = fit([t for v in by_dataset.values() for t in v] + extra, a.C)
        MODULE.write_text(module_source(weights, bias, {k: len(v) for k, v in by_dataset.items()}, a.C, len(extra)))
        print(f"wrote {MODULE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
