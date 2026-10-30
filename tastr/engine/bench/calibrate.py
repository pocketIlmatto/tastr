"""Fit the confidence calibration tables and write tastr/engine/calibration_table.py.

Reads per-track rows from benchmark output and fits, for BPM and for key, a non-decreasing map from the
raw confidence to the share of tracks that were right (isotonic regression, pooled into blocks of at
least MIN_BLOCK tracks). Inputs:

- BPM: `tastr bench <tempo set> --stages core --out r.json` reports (`bpm_confidence`, `acc1`) and
  `bench.tempo_spec --out r.json` results (same fields).
- Key: `train_key --holdout --calibration-rows k.json`, the held-out (out-of-fold) key model's
  `key_confidence` and `key_correct`. Bench reports on the training sets would be in-sample and
  overconfident; key reports on other labeled sets are fine.

Check on alternating halves first (fit on one, score the other), then write the module:

    python -m tastr.engine.bench.calibrate --bpm bp-tempo.json hjdb.json --key key-oof.json
    python -m tastr.engine.bench.calibrate --bpm ... --key ... --write
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

import numpy as np

MIN_BLOCK = 40
FIELDS = {"bpm": ("bpm_confidence", "acc1"), "key": ("key_confidence", "key_correct")}
MODULE = Path(__file__).resolve().parent.parent / "calibration_table.py"


def read_pairs(paths: list[str], kind: str) -> tuple[np.ndarray, np.ndarray]:
    """(raw confidence, 1/0 right) for every row with both fields, from report-shaped JSON files
    (rows under `rows`, or `tracks` for tempo_spec results)."""
    conf_field, ok_field = FIELDS[kind]
    xs, ys = [], []
    for p in paths:
        d = json.loads(Path(p).expanduser().read_text())
        for r in d.get("rows", d.get("tracks", [])):
            if r.get(conf_field) is not None and r.get(ok_field) is not None:
                xs.append(float(r[conf_field]))
                ys.append(float(r[ok_field]))
    return np.array(xs), np.array(ys)


def fit(x: np.ndarray, y: np.ndarray, min_block: int = MIN_BLOCK) -> list[tuple[float, float, int]]:
    """Knots (mean raw confidence, chance right, tracks) of the isotonic fit of y on x.

    Pool-adjacent-violators, then blocks smaller than `min_block` are merged into the neighbour with
    the closer rate (merging neighbours keeps the fit non-decreasing). Rates are smoothed towards 1/2
    by half a track each way so a small all-right or all-wrong block doesn't read as certain.
    """
    order = np.argsort(x, kind="stable")
    blocks: list[list[float]] = []  # [sum x, sum y, n]
    for xi, yi in zip(x[order], y[order]):
        blocks.append([float(xi), float(yi), 1])
        while len(blocks) > 1 and blocks[-2][1] / blocks[-2][2] >= blocks[-1][1] / blocks[-1][2]:
            sx, sy, n = blocks.pop()
            blocks[-1] = [blocks[-1][0] + sx, blocks[-1][1] + sy, blocks[-1][2] + n]
    while len(blocks) > 1:
        i = min(range(len(blocks)), key=lambda j: blocks[j][2])
        if blocks[i][2] >= min_block:
            break
        rate = blocks[i][1] / blocks[i][2]
        nbrs = [j for j in (i - 1, i + 1) if 0 <= j < len(blocks)]
        j = min(nbrs, key=lambda j: abs(blocks[j][1] / blocks[j][2] - rate))
        a, b = sorted((i, j))
        blocks[a:b + 1] = [[blocks[a][k] + blocks[b][k] for k in range(3)]]
    knots = [(sx / n, (sy + 0.5) / (n + 1), int(n)) for sx, sy, n in blocks]
    rates = np.maximum.accumulate([k[1] for k in knots])  # smoothing can dent monotonicity between blocks
    return [(round(k[0], 4), round(float(p), 4), k[2]) for k, p in zip(knots, rates)]


def predict(knots: list[tuple[float, float, int]], x: np.ndarray) -> np.ndarray:
    return np.interp(x, [k[0] for k in knots], [k[1] for k in knots])


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2)) if y.size else float("nan")


def holdout(x: np.ndarray, y: np.ndarray, min_block: int = MIN_BLOCK) -> dict:
    """Fit on alternating halves and score the other half: Brier score of the raw confidence read as a
    probability, of the overall accuracy for every track, and of the calibrated value."""
    ia, ib = np.arange(0, x.size, 2), np.arange(1, x.size, 2)
    out = {"n": int(x.size), "accuracy": float(y.mean()) if y.size else float("nan")}
    preds, base = np.empty_like(y), np.empty_like(y)
    for train, test in ((ia, ib), (ib, ia)):
        preds[test] = predict(fit(x[train], y[train], min_block), x[test])
        base[test] = y[train].mean()
    out |= {"brier_raw": brier(np.clip(x, 0, 1), y), "brier_flat": brier(base, y), "brier_calibrated": brier(preds, y)}
    return out


def reliability(knots, x: np.ndarray, y: np.ndarray, bins: int = 5) -> list[dict]:
    """Per raw-confidence quantile: tracks, raw range, predicted and actual share right."""
    if not x.size:
        return []
    edges = np.quantile(x, np.linspace(0, 1, bins + 1))
    idx = np.clip(np.searchsorted(edges, x, side="right") - 1, 0, bins - 1)
    p = predict(knots, x)
    return [{"n": int((idx == b).sum()), "raw": (float(x[idx == b].min()), float(x[idx == b].max())),
             "predicted": float(p[idx == b].mean()), "actual": float(y[idx == b].mean())}
            for b in range(bins) if (idx == b).any()]


def module_source(tables: dict[str, list], sources: dict[str, list[str]]) -> str:
    def table(kind):
        rows = "".join(f"\n    ({x!r}, {p!r}, {n}),"
                   for x, p, n in tables.get(kind, []))
        return f"{kind.upper()} = ({rows}\n)" if rows else f"{kind.upper()} = ()"

    fitted = "; ".join(f"{k}: {', '.join(Path(s).name for s in v)} ({sum(n for *_, n in tables[k])} tracks)"
                       for k, v in sources.items() if tables.get(k))
    return f'''"""Confidence calibration tables. Generated by tastr.engine.bench.calibrate; do not edit by hand.

Generated {datetime.date.today().isoformat()} from {fitted or "nothing"}.
See calibration.likely_correct and docs/bench.md "Confidence calibration".
"""

# (raw confidence, chance right, tracks) knots, raw ascending; empty = not calibrated.
{table("bpm")}

{table("key")}
'''


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bpm", nargs="+", default=[], metavar="JSON", help="tempo bench reports / tempo_spec results")
    ap.add_argument("--key", nargs="+", default=[], metavar="JSON", help="held-out key rows (train_key --calibration-rows)")
    ap.add_argument("--min-block", type=int, default=MIN_BLOCK)
    ap.add_argument("--write", action="store_true", help=f"fit on everything and write {MODULE.name}")
    a = ap.parse_args(argv)
    if a.write and not (a.bpm and a.key):
        ap.error("--write replaces both tables: pass --bpm and --key")

    sources = {"bpm": a.bpm, "key": a.key}
    tables = {}
    for kind, paths in sources.items():
        if not paths:
            continue
        x, y = read_pairs(paths, kind)
        if not x.size:
            print(f"{kind}: no rows with {' and '.join(FIELDS[kind])}")
            continue
        h = holdout(x, y, a.min_block)
        print(f"{kind}: {h['n']} tracks, {h['accuracy']:.1%} right; held-out Brier raw {h['brier_raw']:.3f}, "
              f"flat {h['brier_flat']:.3f}, calibrated {h['brier_calibrated']:.3f}")
        tables[kind] = fit(x, y, a.min_block)
        for r in reliability(tables[kind], x, y):
            print(f"  raw {r['raw'][0]:.2f}-{r['raw'][1]:.2f}  n {r['n']:4d}  predicted {r['predicted']:.0%}  "
                  f"actual {r['actual']:.0%}")
        print("  knots: " + ", ".join(f"{x:.2f}->{p:.0%}" for x, p, _ in tables[kind]))
    if a.write:
        MODULE.write_text(module_source(tables, sources))
        print(f"wrote {MODULE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
