"""Copying tracks from one person's hosted library into another's (an admin sharing tracks with a friend).

A copy is a new track owned by the other person, with the same analysis (the card and the tables made from it) and
the same lookups. It points at the same audio file and shares its stems, which are kept per audio file: deleting
either copy leaves the file while the other still uses it (tracks.py `remove_upload`), and the expiry keeps a file
an admin's track still uses (hosted.expire_audio). Each person's own work on a track (edits, labels, reviews, cues,
playlists) stays with them. The card's waveform data, kept in the audio bucket by track id, is copied too.
"""

from __future__ import annotations

import logging
from typing import Any

from . import blobs, db

log = logging.getLogger(__name__)

# The tables the analysis fills, copied with each track. track_edits, track_labels, review_flags, pattern_reviews,
# track_cues and list_tracks are someone's own work, so they stay behind.
ANALYSIS_TABLES = ("sections", "tags", "suggestions", "features", "series", "cards", "lookups")


def _columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def plan(conn, src: str, dst: str, track_ids: list[int] | None = None, count: int = 10) -> list[dict[str, Any]]:
    """`src`'s tracks a copy to `dst` would look at: `track_ids` (only `src`'s own, each marked `they_have` when `dst`
    has the same audio already), or else the `count` most recently added that have audio and `dst` doesn't have.
    Each row says whether its audio is on the server (`has_audio`); a copy skips the ones without, and the ones
    `dst` has."""
    sql = ("SELECT id, title, artist, sha256, local_path, created_at, analyzed_at, "
           "EXISTS (SELECT 1 FROM tracks o WHERE o.owner = ? AND o.sha256 = t.sha256) FROM tracks t WHERE owner = ?")
    args: list[Any] = [dst, src]
    if track_ids is not None:
        if not track_ids:
            return []
        sql += f" AND id IN ({', '.join('?' * len(track_ids))}) ORDER BY id"
        args += track_ids
    else:
        sql += (" AND local_path IS NOT NULL AND NOT EXISTS (SELECT 1 FROM tracks o WHERE o.owner = ? AND o.sha256 = t.sha256)"
                " ORDER BY coalesce(created_at, analyzed_at) DESC, id DESC LIMIT ?")
        args += [dst, count]
    keys = ("id", "title", "artist", "sha256", "local_path", "created_at", "analyzed_at", "they_have")
    rows = [dict(zip(keys, r)) for r in conn.execute(sql, args).fetchall()]
    for r in rows:
        r["they_have"] = bool(r["they_have"])
        r["has_audio"] = blobs.exists(r.pop("local_path"))
    return rows


def copy_tracks(conn, track_ids: list[int], src: str, dst: str) -> dict[int, int]:
    """Copy `src`'s tracks `track_ids` into `dst`'s library, all in one transaction. Returns {old id: new id}; a
    track that isn't `src`'s, whose audio `dst` has already, or whose audio isn't on the server is skipped."""
    ids = [r["id"] for r in plan(conn, src, dst, track_ids) if r["has_audio"] and not r["they_have"]]
    track_cols = [c for c in _columns(conn, "tracks") if c not in ("id", "owner")]
    names = ", ".join(f'"{c}"' for c in track_cols)
    tables = {t: [c for c in _columns(conn, t) if c != "track_id"] for t in ANALYSIS_TABLES}
    made: dict[int, int] = {}

    def copy():
        for old in ids:
            cur = conn.execute(f'INSERT INTO tracks ({names}, owner) SELECT {names}, ? FROM tracks WHERE id = ?',
                               (dst, old))
            new = cur.lastrowid
            for t, cols in tables.items():
                cs = ", ".join(f'"{c}"' for c in cols)
                conn.execute(f'INSERT INTO "{t}" (track_id, {cs}) SELECT ?, {cs} FROM "{t}" WHERE track_id = ?',
                             (new, old))
            made[old] = new

    db.atomic(conn, copy)
    for old, new in made.items():
        _copy_views(conn, old, new)
    return made


def _copy_views(conn, old: int, new: int) -> None:
    """The card's waveform data, when it lives in the audio bucket (blobs.IN_BUCKET) rather than in the card."""
    if not blobs.enabled():
        return
    card = db.load_card(conn, new)
    if card is None or card.extras.get("views") != blobs.IN_BUCKET:
        return
    try:
        blobs.put_views(new, blobs.get_views(old))
    except Exception as e:  # noqa: BLE001 - the copy still has its card; only the waveform views would be missing
        log.warning("couldn't copy track %s's views to %s: %s", old, new, e)
