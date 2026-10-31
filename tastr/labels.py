"""Your own labels: corrected or confirmed BPM, key and section starts, and the queue of tracks worth a look.

The engine is tuned on public datasets that are mostly EDM previews and short clips. Labels on the tracks
you actually use are the only check on full-length tracks, drops, and conventions like drum & bass at 174
vs 87. They live in the `track_labels` table, separate from the analysis, so re-analyzing never touches
them. `tastr labels export` writes them as a `tastr bench manifest` file, so every engine change can be
scored against them as well.

The review queue (`tastr review`) ranks unlabeled tracks by how likely their analysis is wrong: a low
"likely right" score on BPM or key, or a catalog listing (tastr.lookup) that disagrees with it.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import db

BPM_RANGE = (30.0, 300.0)


class LabelError(ValueError):
    """A label that can't be stored as given."""


def clean(labels: dict[str, Any]) -> dict[str, Any]:
    """Check and normalize label fields: bpm a number in BPM_RANGE, key one parse_key reads, boundaries as
    seconds or m:ss separated by spaces, commas or semicolons. Empty strings mean "not labeled"."""
    from .engine.bench.datasets import parse_key, parse_time

    out: dict[str, Any] = {}
    bpm = labels.get("bpm")
    if bpm not in (None, ""):
        try:
            bpm = float(bpm)
        except (TypeError, ValueError):
            raise LabelError(f"BPM {bpm!r} isn't a number") from None
        if not BPM_RANGE[0] <= bpm <= BPM_RANGE[1]:
            raise LabelError(f"BPM {bpm:g} is outside {BPM_RANGE[0]:g}-{BPM_RANGE[1]:g}")
        out["bpm"] = bpm
    key = str(labels.get("key_name") or labels.get("key") or "").strip()
    if key:
        if parse_key(key) is None:
            raise LabelError(f"key {key!r} isn't one Tastr reads (try \"A minor\", \"Am\" or \"8A\")")
        out["key_name"] = key
    b = labels.get("boundaries")
    if isinstance(b, (list, tuple)):
        b = " ".join(str(x) for x in b)
    if b not in (None, ""):
        try:
            times = sorted(parse_time(x) for x in re.split(r"[;\s,]+", str(b).strip()) if x)
        except ValueError:
            raise LabelError(f"section starts {b!r} aren't seconds or m:ss") from None
        out["boundaries"] = " ".join(f"{t:g}" for t in times)
    if labels.get("note"):
        out["note"] = str(labels["note"]).strip()[:500]
    return out


def export_manifest(conn: sqlite3.Connection, out: str | Path, owner: str | None = None) -> int:
    """Write every labeled track with its audio on disk as a `tastr bench manifest` CSV; returns the count."""
    rows = [r for r in db.list_labels(conn, owner) if r["path"] and Path(r["path"]).is_file()]
    with Path(out).open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "path", "bpm", "key", "boundaries"])
        for r in rows:
            w.writerow([f"track-{r['track_id']}", str(Path(r["path"]).resolve()),
                        f"{r['bpm']:g}" if r["bpm"] else "", r["key_name"] or "", r["boundaries"] or ""])
    return len(rows)


def review_queue(conn: sqlite3.Connection, limit: int = 20, owner: str | None = None) -> list[dict[str, Any]]:
    """Unlabeled tracks most worth a look, most suspect first. `chance_right` is the lower of the BPM and key
    "likely right" scores (None on cards analyzed before calibration); `reasons` says why each is listed."""
    from . import lookup

    labeled = {r["track_id"] for r in db.list_labels(conn, owner)}
    out = []
    for t in db.list_tracks(conn, owner):
        if t["id"] in labeled:
            continue
        card = db.load_card(conn, t["id"])
        if card is None:
            continue
        reasons, scores = [], []
        bpm_p = card.core.bpm_likely_correct
        key_p = card.core.key.likely_correct if card.core.key else None
        for name, p in (("BPM", bpm_p), ("key", key_p)):
            if p is not None:
                scores.append(p)
                if p < 0.6:
                    reasons.append(f"{name} {round(100 * p)}% likely right")
        disagree = 0
        for r in db.get_lookups(conn, t["id"]):
            if r.get("status") != "found":
                continue
            cmp = lookup.compare(card, r)
            for what in ("bpm", "key"):
                if cmp[what] not in (None, "agrees"):
                    disagree += 1
                    reasons.append(f"{r['provider']} lists {what} {cmp[what]}")
        if not reasons:
            continue
        chance = min(scores) if scores else None
        out.append({"track_id": t["id"], "title": card.source.title, "artist": card.source.artist,
                    "bpm": card.core.bpm, "key": card.core.key.name if card.core.key else None,
                    "chance_right": chance, "disagreements": disagree, "reasons": reasons})
    out.sort(key=lambda r: (-r["disagreements"], r["chance_right"] if r["chance_right"] is not None else 1.0))
    return out[:limit]
