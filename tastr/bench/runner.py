"""Run the analysis pipeline over a labeled dataset and score it.

    report = run(datasets.load("giantsteps-key", "~/data/giantsteps-key"), jobs=4)
    print(format_summary(report))

A report is plain JSON: `summary` (mean of each metric over the tracks that
have that label), `rows` (per-track estimates and scores) and `review` (the
tracks most worth a human look: wrong answers first, then low confidence).
Save reports and pass one back as `baseline` to see what a change moved.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from .. import __version__
from ..card import Source, TrackCard
from ..pipeline import SR, registered_stages, run_pipeline
from . import metrics
from .datasets import Item

DEFAULT_STAGES = ["core", "sections"]


def analyze_item(item: Item, stages: list[str] | None = None) -> TrackCard:
    if item.synth is not None:
        y = item.synth()
    else:
        from ..ingest import load_audio

        y = load_audio(item.path, SR)
    card = TrackCard(source=Source(kind="file", uri=item.path or item.id, local_path=item.path, title=item.id))
    return run_pipeline(y, SR, card, stages=stages or DEFAULT_STAGES)


def score_item(item: Item, card: TrackCard) -> dict:
    """Estimates, confidences and scores for one track, as a flat dict."""
    t, c = item.truth, card.core
    row: dict = {"id": item.id, "path": item.path, "errors": dict(card.errors)}
    row["est_bpm"] = c.bpm
    row["bpm_confidence"] = c.bpm_confidence
    row["est_key"] = c.key.name if c.key else None
    row["key_confidence"] = c.key.confidence if c.key else None
    if t.bpm:
        row["ref_bpm"] = t.bpm
        row.update(metrics.tempo_scores(c.bpm, t.bpm))
    if t.tonic is not None and t.mode:
        from ..music import note_name

        row["ref_key"] = f"{note_name(t.tonic)} {t.mode}"
        row.update(metrics.key_scores(c.key.tonic if c.key else None, c.key.mode if c.key else None, t.tonic, t.mode))
    if t.beats:
        row.update(metrics.beat_scores(c.beat_times, t.beats, "beat"))
    if t.downbeats:
        row.update(metrics.beat_scores(c.downbeat_times, t.downbeats, "downbeat"))
    if t.boundaries:
        row.update(metrics.boundary_scores([s.start for s in card.sections], t.boundaries, card.duration))
    return row


def _work(args) -> dict:
    item, stages = args
    t0 = time.time()
    try:
        row = score_item(item, analyze_item(item, stages))
    except Exception as e:  # one unreadable file shouldn't sink a 600-track run
        row = {"id": item.id, "path": item.path, "errors": {"bench": f"{type(e).__name__}: {e}"}}
    row["seconds"] = round(time.time() - t0, 2)
    return row


SUMMARY_METRICS = ["acc1", "acc2", "key_weighted", "key_correct", "beat_f", "downbeat_f", "boundary_f_0.5", "boundary_f_3"]


def summarize(rows: list[dict]) -> dict:
    out: dict = {"tracks": len(rows), "failed": sum(1 for r in rows if "bench" in r.get("errors", {}))}
    for m in SUMMARY_METRICS:
        vals = [r[m] for r in rows if m in r]
        if vals:
            out[m] = round(metrics.mean(vals), 4)
            out[f"{m}_n"] = len(vals)
    cats = [r["key_category"] for r in rows if "key_category" in r]
    if cats:
        out["key_categories"] = {k: cats.count(k) for k in sorted(set(cats))}
    octaves = [r["octave"] for r in rows if r.get("octave")]
    if octaves:
        out["tempo_octave_errors"] = {k: octaves.count(k) for k in sorted(set(octaves))}
    return out


def review_queue(rows: list[dict], n: int = 20) -> list[dict]:
    """Tracks to look at first: wrong answers (most confident wrong ones on top,
    they show systematic bugs), then right answers the engine was unsure of."""

    def wrong(r):
        return r.get("acc1") == 0.0 or r.get("key_correct") == 0.0

    def conf(r):
        cs = [r.get("bpm_confidence") if "acc1" in r else None, r.get("key_confidence") if "key_correct" in r else None]
        cs = [x for x in cs if x is not None]
        return min(cs) if cs else 0.0

    bad = sorted((r for r in rows if wrong(r)), key=conf, reverse=True)
    unsure = sorted((r for r in rows if not wrong(r)), key=conf)
    keep = ("id", "path", "est_bpm", "ref_bpm", "est_key", "ref_key", "key_category", "octave", "bpm_confidence", "key_confidence")
    return [{k: r[k] for k in keep if k in r} | {"reason": "wrong" if wrong(r) else "low confidence"}
            for r in (bad + unsure)[:n]]


def run(items: list[Item], stages: list[str] | None = None, jobs: int = 1, limit: int | None = None,
        progress=None) -> dict:
    items = items[:limit] if limit else items
    work = [(it, stages) for it in items]
    rows: list[dict] = []
    t0 = time.time()
    if jobs > 1:
        with ProcessPoolExecutor(jobs) as pool:
            for row in pool.map(_work, work):
                rows.append(row)
                if progress:
                    progress(len(rows), len(items), row)
    else:
        for w in work:
            rows.append(_work(w))
            if progress:
                progress(len(rows), len(items), rows[-1])
    versions = {name: st.version for name, st in registered_stages().items()}
    return {
        "tastr_version": __version__,
        "stage_versions": versions,
        "stages": stages or DEFAULT_STAGES,
        "seconds": round(time.time() - t0, 1),
        "summary": summarize(rows),
        "review": review_queue(rows),
        "rows": rows,
    }


LABELS = {
    "acc1": "BPM within 4%",
    "acc2": "BPM incl. x2 / half",
    "key_weighted": "Key (MIREX weighted)",
    "key_correct": "Key exactly right",
    "beat_f": "Beat F (70 ms)",
    "downbeat_f": "Downbeat F (70 ms)",
    "boundary_f_0.5": "Sections F (0.5 s)",
    "boundary_f_3": "Sections F (3 s)",
}


def format_summary(report: dict, baseline: dict | None = None) -> str:
    s = report["summary"]
    b = (baseline or {}).get("summary", {})
    lines = [f"{s['tracks']} tracks ({s['failed']} failed) in {report['seconds']} s"]
    for m in SUMMARY_METRICS:
        if m not in s:
            continue
        delta = f"   ({s[m] - b[m]:+.3f} vs baseline)" if m in b else ""
        lines.append(f"  {LABELS[m]:24} {s[m]:.3f}  n={s[m + '_n']}{delta}")
    if "key_categories" in s:
        lines.append("  key results: " + ", ".join(f"{k} {v}" for k, v in s["key_categories"].items()))
    if "tempo_octave_errors" in s:
        lines.append("  BPM octave errors: " + ", ".join(f"{k} {v}" for k, v in s["tempo_octave_errors"].items()))
    if report["review"]:
        lines.append("  review first:")
        for r in report["review"][:5]:
            est = " ".join(str(x) for x in (r.get("est_bpm"), r.get("est_key")) if x is not None)
            ref = " ".join(str(x) for x in (r.get("ref_bpm"), r.get("ref_key")) if x is not None)
            lines.append(f"    {r['id']}: got {est or '?'}, expected {ref or '?'} ({r['reason']})")
    return "\n".join(lines)


def save(report: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(report, indent=2, default=str))


def load_report(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())
