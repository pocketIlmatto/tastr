"""Compare the monthly benchmark smoke run (.github/workflows/bench-smoke.yml, docs/ci.md#benchmark-smoke-run)
with the last good run and with the scores the app quotes.

    python3 .github/scripts/bench_smoke.py REPORTS PREVIOUS [--quoted tastr/engine/bench_scores.json]

REPORTS holds this run's `tastr bench --out` reports, one `<dataset>.json` per dataset. PREVIOUS holds the
same files from the last successful run (it may be empty or missing: the first run, or the artifact expired).
Prints a Markdown report and, under GitHub Actions, writes `drop=true|false` to $GITHUB_OUTPUT. Exits 0 either
way; the workflow decides what a drop means.

A score counts as dropped when it is more than DROP_VS_LAST below the last good run on the same tracks (the
subset is the first N tracks, so the same N means the same tracks), or more than DROP_VS_QUOTED below the full
milestone score in bench_scores.json (a 100-track subset sits within a few points of the full set). A dataset
whose run is missing or that failed on more than MAX_FAILED of its tracks also counts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# dataset -> summary metric, the same ones tastr/engine/bench/scores.py quotes
METRICS = {
    "giantsteps-key": ("key_correct", "exact key"),
    "beatport-key": ("key_correct", "exact key"),
    "fma-keys": ("key_correct", "exact key"),
    "giantsteps-tempo": ("acc1", "BPM within 4%"),
    "beatport-tempo": ("acc1", "BPM within 4%"),
}
DROP_VS_LAST = 0.05
DROP_VS_QUOTED = 0.12
MAX_FAILED = 0.10


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def compare(reports: dict[str, dict | None], previous: dict[str, dict], quoted: dict) -> tuple[bool, list[str]]:
    """(any drop, Markdown table rows) for {dataset: report or None when the run produced none}."""
    drop = False
    rows = []
    for dataset in sorted(reports):
        metric, what = METRICS[dataset]
        report = reports[dataset]
        if report is None or metric not in report.get("summary", {}):
            drop = True
            rows.append(f"| {dataset} | {what} | no score: the run failed (see the log) | | | **problem** |")
            continue
        s = report["summary"]
        score, n = s[metric], s.get(f"{metric}_n", s["tracks"])
        notes = []
        failed = s.get("failed", 0) / max(s["tracks"], 1)
        if failed > MAX_FAILED:
            notes.append(f"{s['failed']} of {s['tracks']} tracks failed")
        last = previous.get(dataset, {}).get("summary", {})
        last_txt = ""
        if metric in last and last.get("tracks") == s["tracks"]:
            last_txt = f"{last[metric]:.3f}"
            if score < last[metric] - DROP_VS_LAST:
                notes.append(f"down {last[metric] - score:.3f} since the last good run")
        q = quoted.get("scores", {}).get(dataset)
        q_txt = ""
        if q:
            q_txt = f"{q['exact']:.3f} ({q['tracks']} tracks, {q['run']})"
            if score < q["exact"] - DROP_VS_QUOTED:
                notes.append(f"{q['exact'] - score:.3f} under the quoted full score")
        drop = drop or bool(notes)
        verdict = "**drop**: " + "; ".join(notes) if notes else "ok"
        rows.append(f"| {dataset} | {what} | {score:.3f} on {n} | {last_txt or 'none yet'} | {q_txt or 'none'} | {verdict} |")
    return drop, rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("reports", type=Path)
    ap.add_argument("previous", type=Path)
    ap.add_argument("--quoted", type=Path, default=Path("tastr/engine/bench_scores.json"))
    ap.add_argument("--datasets", default=",".join(METRICS), help="datasets this run was meant to cover")
    args = ap.parse_args(argv)

    reports = {d: _load(args.reports / f"{d}.json") for d in args.datasets.split(",") if d}
    previous = {d: r for d in reports if (r := _load(args.previous / f"{d}.json"))}
    drop, rows = compare(reports, previous, _load(args.quoted) or {})
    print("| Dataset | Score | This run | Last good run | Quoted (full set) | Result |")
    print("|---|---|---|---|---|---|")
    print("\n".join(rows))
    print()
    print(f"A drop is more than {DROP_VS_LAST} below the last good run on the same tracks, more than "
          f"{DROP_VS_QUOTED} below the quoted full score, or over {MAX_FAILED:.0%} failed tracks.")
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a") as f:
            f.write(f"drop={'true' if drop else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
