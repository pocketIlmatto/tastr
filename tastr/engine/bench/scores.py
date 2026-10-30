"""The headline benchmark scores the UI quotes (Glossary), kept in tastr/engine/bench_scores.json.

A full milestone run with `--record` (`tastr bench ... --record`, `tastr spec-bench downbeats ... --record`)
rewrites its dataset's entry; runs on a subset (`--limit`) never do. The web app serves the file at
GET /api/bench, so the Glossary text follows the latest numbers without a UI change.

Shape: {"scores": {"<key>": {"exact": 0.599, "tracks": 599, "what": "tracks, exact key", "run": "2026-10-29"}}}
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

SCORES_PATH = Path(__file__).resolve().parent.parent / "bench_scores.json"

# `tastr bench` dataset -> (summary metric, hover text after the track count)
BENCH_METRICS = {
    "giantsteps-key": ("key_correct", "tracks, exact key"),
    "beatport-key": ("key_correct", "tracks, exact key"),
    "giantsteps-tempo": ("acc1", "tracks, BPM within 4%"),
    "beatport-tempo": ("acc1", "tracks, BPM within 4%"),
}
# `tastr spec-bench downbeats` dataset -> score key
DOWNBEAT_KEYS = {"gtzan": "downbeat"}
# `tastr spec-bench sections` dataset -> score key (F at 3 s of the stage's current settings, both halves)
SECTION_KEYS = {"harmonix": "sections"}


def load(path: Path | None = None) -> dict:
    """The recorded scores, or no scores when the file is missing or unreadable."""
    try:
        data = json.loads(Path(path or SCORES_PATH).read_text())
    except (OSError, ValueError):
        return {"scores": {}}
    return data if isinstance(data, dict) and isinstance(data.get("scores"), dict) else {"scores": {}}


def recordable(bench: str, dataset: str) -> bool:
    keys = {"bench": BENCH_METRICS, "downbeats": DOWNBEAT_KEYS, "sections": SECTION_KEYS}.get(bench, {})
    return dataset in keys


def entry_from_report(bench: str, dataset: str, report: dict, run: str | None = None) -> tuple[str, dict]:
    """(score key, entry) for a finished full run: `bench` is "bench" (runner report), "downbeats" or "sections"."""
    run = run or dt.datetime.now().astimezone().date().isoformat()
    if bench == "sections":
        cur, n = report["current"], report["tracks"]
        n_tune = report["tune_tracks"]
        f3 = (cur["tune"]["f_3"] * n_tune + cur["check"]["f_3"] * (n - n_tune)) / n
        return SECTION_KEYS[dataset], {"exact": round(f3, 3), "tracks": n, "what": "songs, boundary F within 3 s",
                                       "run": run}
    if bench == "downbeats":
        return DOWNBEAT_KEYS[dataset], {"exact": report["right"], "tracks": report["tracks"],
                                        "what": "songs, bar 1 right", "run": run}
    metric, what = BENCH_METRICS[dataset]
    s = report["summary"]
    if metric not in s:
        raise ValueError(f"the report has no {metric} score to record (were the core stages run?)")
    return dataset, {"exact": round(s[metric], 3), "tracks": s.get(f"{metric}_n", s["tracks"]), "what": what, "run": run}


def record(key: str, entry: dict, path: Path | None = None) -> None:
    path = path or SCORES_PATH
    data = load(path)
    data["scores"][key] = entry
    data["scores"] = dict(sorted(data["scores"].items()))
    Path(path).write_text(json.dumps(data, indent=2) + "\n")
