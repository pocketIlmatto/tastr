"""Moving what a hosted server had before the audio bucket was on into it (`tastr bucket-backfill`, TAS-64).

Uploads and stems on the disk go up as they are; each card's inline waveform data moves to `views/<track id>.json`
and the card keeps blobs.IN_BUCKET, as new cards do (tastr/blobs.py). The sweep (blobs.evict) then deletes the
local copies.
"""

from __future__ import annotations

import json
import logging
from contextlib import closing

from . import blobs, db

log = logging.getLogger(__name__)


def backfill(db_path=None, dry_run: bool = False, log_each=None) -> dict[str, int]:
    """Copy every file under uploads/ and stems/ that the bucket lacks (or has at another size) up, and move each
    card's inline waveform data to the bucket, leaving blobs.IN_BUCKET in the card. Safe to run again: what's
    already there is skipped. The six-hourly sweep (evict) then deletes the local copies. `dry_run` only counts.
    Returns {"files", "file_bytes", "views", "view_bytes", "failed"}."""
    if not blobs.enabled():
        raise RuntimeError("The audio bucket is off: set TASTR_AUDIO_BUCKET and its settings first (docs/hosting.md).")
    out = {"files": 0, "file_bytes": 0, "views": 0, "view_bytes": 0, "failed": 0}
    have = {k: size for f in blobs.FOLDERS for k, (size, _) in blobs.listing(f + "/", fresh=True).items()}
    for folder in blobs.FOLDERS:
        root = blobs.home() / folder
        for f in sorted(root.rglob("*")) if root.is_dir() else []:
            k = blobs.key(f)
            if not f.is_file() or k is None or f.name.endswith(".part") or have.get(k) == f.stat().st_size:
                continue
            size = f.stat().st_size
            try:
                if not dry_run:
                    blobs.push(f)
            except Exception as e:  # noqa: BLE001 - counted and reported; the next run tries again
                out["failed"] += 1
                log.warning("couldn't copy %s to the bucket: %s", f, e)
                continue
            out["files"] += 1
            out["file_bytes"] += size
            if log_each:
                log_each(f"{'would copy' if dry_run else 'copied'} {k} ({size / 1e6:.1f} MB)")
    with closing(db.connect(db_path)) as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT track_id FROM cards WHERE json_type(json, '$.extras.views') = 'object' "
            "AND json_extract(json, '$.extras.views.in_bucket') IS NULL ORDER BY track_id")]
        for tid in ids:
            text = conn.execute("SELECT json FROM cards WHERE track_id = ?", (tid,)).fetchone()[0]
            card = json.loads(text)
            views = card["extras"]["views"]
            size = len(json.dumps(views, separators=(",", ":")))
            try:
                if not dry_run:
                    blobs.put_views(tid, views)
                    card["extras"]["views"] = blobs.IN_BUCKET
                    with conn:  # only if the card is as read: a re-analysis meanwhile already wrote its own
                        conn.execute("UPDATE cards SET json = ? WHERE track_id = ? AND json = ?",
                                     (json.dumps(card), tid, text))
            except Exception as e:  # noqa: BLE001
                out["failed"] += 1
                log.warning("couldn't move track %s's waveform data to the bucket: %s", tid, e)
                continue
            out["views"] += 1
            out["view_bytes"] += size
            if log_each:
                log_each(f"{'would move' if dry_run else 'moved'} track {tid}'s waveform data ({size / 1e6:.1f} MB)")
        if not dry_run and out["views"]:
            conn.execute("VACUUM")  # give the freed pages back, so the database file (and Litestream) shrinks
    return out
