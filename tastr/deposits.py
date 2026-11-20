"""The bank: deposits, the things a person puts in to come back to.

A deposit is source material, its context (an optional note, where it came from) and what the tooling extracted,
kept together. It has a size and no type:

- a **moment**: a time range inside a source (a transition, a line, a sound); it can touch two or more tracks
- a **track**: one file or link; it points at the track card the library already keeps
- a **collection**: a playlist, a DJ set, a library; it holds tracks, and moments that span them

The library's tracks stay where they are. `sync_tracks` gives each one a track-sized deposit that points at its
card, so the bank starts with the whole library in it and the track pages keep reading their own tables.

Everything here is per person on a hosted server (`owner`, as in the library) and takes an open connection.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import db

SIZES = ("moment", "track", "collection")
LINK_KINDS = ("holds", "touches")
# 'ready': nothing is waiting. 'needs-source': it names something whose audio isn't here yet.
# 'working': a breakdown is running. 'failed': the breakdown gave up (extracted["error"] says why).
STATUSES = ("ready", "needs-source", "working", "failed")
MAX_NOTE, MAX_TITLE, MAX_TAG, MAX_TAGS = 4000, 300, 60, 100


class DepositError(ValueError):
    """A deposit that can't be saved as asked; the message is fit to show the person."""


def _clean_tags(tags) -> list[tuple[str, str]]:
    """[(tag, origin)] from tags given as strings (origin 'user') or {tag, origin}, trimmed, without repeats."""
    out: dict[tuple[str, str], None] = {}
    for t in tags or []:
        tag, origin = (t.get("tag"), t.get("origin") or "user") if isinstance(t, dict) else (t, "user")
        tag, origin = str(tag or "").strip(), str(origin).strip() or "user"
        if not tag:
            continue
        if len(tag) > MAX_TAG or len(origin) > MAX_TAG:
            raise DepositError(f"A tag can be at most {MAX_TAG} characters.")
        out[(tag, origin)] = None
    if len(out) > MAX_TAGS:
        raise DepositError(f"A deposit can have at most {MAX_TAGS} tags.")
    return list(out)


def _check_range(start_s, end_s) -> None:
    if start_s is None and end_s is None:
        return
    if start_s is None or end_s is None:
        raise DepositError("Give both a start and an end, or neither.")
    if start_s < 0 or end_s <= start_s:
        raise DepositError("The end has to come after the start.")


def _text(value, limit: int, what: str) -> str | None:
    value = (value or "").strip() or None
    if value and len(value) > limit:
        raise DepositError(f"The {what} can be at most {limit} characters.")
    return value


def _mine(conn, owner: str | None, deposit_id: int):
    return conn.execute("SELECT * FROM deposits WHERE id = ? AND owner IS ?", (deposit_id, owner)).fetchone()


def add(conn: sqlite3.Connection, owner: str | None, size: str, *, title: str | None = None, note: str | None = None,
        source_kind: str | None = None, source_uri: str | None = None, start_s: float | None = None,
        end_s: float | None = None, tags=None, context: dict[str, Any] | None = None,
        extracted: dict[str, Any] | None = None, status: str = "ready", held_by: int | None = None,
        position: int | None = None, touches: list[int] | None = None) -> dict[str, Any]:
    """Save a new deposit and return it as `get` does. `held_by`: the deposit (a track or collection) it sits in,
    at `position`; a moment's start_s and end_s are then times in that holder. `touches`: track deposits a moment
    spans. Raises DepositError for what can't be saved."""
    if size not in SIZES:
        raise DepositError(f"size is one of: {', '.join(SIZES)}")
    if status not in STATUSES:
        raise DepositError(f"status is one of: {', '.join(STATUSES)}")
    if size == "track":
        raise DepositError("Tracks come into the bank by being analyzed; add a moment or a collection here.")
    title, note = _text(title, MAX_TITLE, "title"), _text(note, MAX_NOTE, "note")
    _check_range(start_s, end_s)
    if size != "moment" and start_s is not None:
        raise DepositError("Only a moment has a start and an end.")
    tag_rows = _clean_tags(tags)
    touches = list(dict.fromkeys(touches or []))
    if touches and size != "moment":
        raise DepositError("Only a moment touches tracks.")
    if held_by is not None:
        holder = _mine(conn, owner, held_by)
        if holder is None:
            raise DepositError(f"No deposit {held_by} to put it in.")
        if holder["size"] == "moment" or (holder["size"] == "track" and size != "moment"):
            raise DepositError(f"A {holder['size']} can't hold a {size}.")
    for t in touches:
        row = _mine(conn, owner, t)
        if row is None or row["size"] != "track":
            raise DepositError(f"Deposit {t} isn't a track.")
    now = db._now()

    def write() -> int:
        did = conn.execute(
            "INSERT INTO deposits (owner, size, title, note, source_kind, source_uri, start_s, end_s, status, "
            "context_json, extracted_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (owner, size, title, note, source_kind, source_uri, start_s, end_s, status, json.dumps(context or {}),
             json.dumps(extracted or {}), now, now)).lastrowid
        conn.executemany("INSERT INTO deposit_tags (deposit_id, tag, origin) VALUES (?, ?, ?)",
                         [(did, tag, origin) for tag, origin in tag_rows])
        if held_by is not None:
            conn.execute("INSERT INTO deposit_links (from_id, to_id, kind, position, start_s, end_s) "
                         "VALUES (?, ?, 'holds', ?, ?, ?)", (held_by, did, position, start_s, end_s))
        conn.executemany("INSERT INTO deposit_links (from_id, to_id, kind) VALUES (?, ?, 'touches')",
                         [(did, t) for t in touches])
        return did

    return get(conn, owner, db.atomic(conn, write))


def hold(conn: sqlite3.Connection, owner: str | None, holder_id: int, deposit_ids: list[int]) -> int:
    """Put deposits into a collection, after what it already holds, in the order given; returns how many were
    added (ones it holds already, and ids that aren't this person's, are skipped)."""
    holder = _mine(conn, owner, holder_id)
    if holder is None or holder["size"] != "collection":
        raise DepositError(f"Deposit {holder_id} isn't a collection.")
    have = {r[0] for r in conn.execute("SELECT to_id FROM deposit_links WHERE from_id = ? AND kind = 'holds'",
                                       (holder_id,))}
    last = conn.execute("SELECT COALESCE(MAX(position), -1) FROM deposit_links WHERE from_id = ? AND kind = 'holds'",
                        (holder_id,)).fetchone()[0]
    new = []
    for d in dict.fromkeys(deposit_ids):
        row = _mine(conn, owner, d)
        if row is not None and d not in have and d != holder_id and row["size"] != "collection":
            new.append(d)

    def write() -> None:
        conn.executemany("INSERT INTO deposit_links (from_id, to_id, kind, position) VALUES (?, ?, 'holds', ?)",
                         [(holder_id, d, last + 1 + i) for i, d in enumerate(new)])
        conn.execute("UPDATE deposits SET updated_at = ? WHERE id = ?", (db._now(), holder_id))

    if new:
        db.atomic(conn, write)
    return len(new)


def sync_tracks(conn: sqlite3.Connection, owner: str | None = None) -> int:
    """Give every track in the library that has no deposit yet a track-sized one pointing at its card (its title
    stays the card's unless the person gives the deposit one of its own), and drop
    the bare deposits of tracks that were deleted since (ones with no note, tags or links; the rest stay, marked
    'needs-source'). `owner` None: every library. Returns how many deposits were added. Cheap when nothing changed."""
    where, args = (" AND t.owner = ?", (owner,)) if owner is not None else ("", ())
    mine, margs = (" AND owner = ?", (owner,)) if owner is not None else ("", ())
    orphan = f"size = 'track' AND source_kind = 'track' AND track_id IS NULL{mine}"

    def write() -> int:
        # a track handed to someone else (`tastr set-owner`, a copied library) takes its deposit along
        conn.execute("UPDATE deposits SET owner = (SELECT t.owner FROM tracks t WHERE t.id = deposits.track_id) "
                     "WHERE track_id IS NOT NULL AND owner IS NOT (SELECT t.owner FROM tracks t WHERE t.id = "
                     "deposits.track_id)")
        added = conn.execute(
            "INSERT INTO deposits (owner, size, title, source_kind, source_uri, track_id, created_at, updated_at) "
            "SELECT t.owner, 'track', NULL, 'track', NULL, t.id, COALESCE(t.created_at, t.analyzed_at), "
            "COALESCE(t.created_at, t.analyzed_at) FROM tracks t "
            f"WHERE NOT EXISTS (SELECT 1 FROM deposits d WHERE d.track_id = t.id){where}", args).rowcount
        conn.execute(
            f"DELETE FROM deposits WHERE {orphan} AND note IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM deposit_tags g WHERE g.deposit_id = deposits.id) "
            "AND NOT EXISTS (SELECT 1 FROM deposit_links l WHERE l.from_id = deposits.id OR l.to_id = deposits.id)",
            margs)
        conn.execute(f"UPDATE deposits SET status = 'needs-source' WHERE {orphan} AND status = 'ready'", margs)
        return added

    return db.atomic(conn, write)


def _row(r, tags: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "id": r["id"], "size": r["size"], "title": r["title"] or r["track_title"], "note": r["note"],
        "source_kind": r["source_kind"], "source_uri": r["source_uri"], "track_id": r["track_id"],
        "start_s": r["start_s"], "end_s": r["end_s"], "status": r["status"], "tags": tags,
        "context": db._json_or(r["context_json"], {}), "extracted": db._json_or(r["extracted_json"], {}),
        "created_at": r["created_at"], "updated_at": r["updated_at"],
        "holds_count": r["holds_count"], "touches_count": r["touches_count"],
        "track": ({"id": r["track_id"], "title": r["track_title"], "artist": r["artist"], "bpm": r["bpm"],
                   "key": r["key_name"], "camelot": r["camelot"], "duration": r["duration"]}
                  if r["track_id"] is not None else None),
    }


_SELECT = ("SELECT d.*, t.title AS track_title, t.artist, t.bpm, t.key_name, t.camelot, t.duration, "
           "(SELECT COUNT(*) FROM deposit_links l WHERE l.from_id = d.id AND l.kind = 'holds') AS holds_count, "
           "(SELECT COUNT(*) FROM deposit_links l WHERE l.from_id = d.id AND l.kind = 'touches') AS touches_count "
           "FROM deposits d LEFT JOIN tracks t ON t.id = d.track_id")


def _tags_of(conn, ids: list[int]) -> dict[int, list[dict[str, str]]]:
    out: dict[int, list[dict[str, str]]] = {i: [] for i in ids}
    for i in range(0, len(ids), 500):  # under SQLite's limit on bound values
        chunk = ids[i:i + 500]
        for r in conn.execute(f"SELECT deposit_id, tag, origin FROM deposit_tags WHERE deposit_id IN "
                              f"({', '.join('?' * len(chunk))}) ORDER BY origin, tag", chunk):
            out[r[0]].append({"tag": r[1], "origin": r[2]})
    return out


def find(conn: sqlite3.Connection, owner: str | None, *, size: str | None = None, tag: str | None = None,
         q: str | None = None, status: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """One person's deposits, newest first: {total, deposits}. `q` matches the title, the note, the source and
    the track's title and artist; `tag` is an exact tag of any origin."""
    where, args = ["d.owner IS ?"], [owner]
    if size:
        where.append("d.size = ?")
        args.append(size)
    if status:
        where.append("d.status = ?")
        args.append(status)
    if tag:
        where.append("EXISTS (SELECT 1 FROM deposit_tags g WHERE g.deposit_id = d.id AND g.tag = ?)")
        args.append(tag)
    if q and q.strip():
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        cols = ("d.title", "d.note", "d.source_uri", "t.title", "t.artist")
        where.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in cols) + ")")
        args += [like] * len(cols)
    cond = " WHERE " + " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM deposits d LEFT JOIN tracks t ON t.id = d.track_id{cond}",
                         args).fetchone()[0]
    rows = conn.execute(f"{_SELECT}{cond} ORDER BY d.created_at DESC, d.id DESC LIMIT ? OFFSET ?",
                        (*args, max(1, min(int(limit), 500)), max(0, int(offset)))).fetchall()
    tags = _tags_of(conn, [r["id"] for r in rows])
    return {"total": total, "deposits": [_row(r, tags[r["id"]]) for r in rows]}


def _brief(r) -> dict[str, Any]:
    return {"id": r["id"], "size": r["size"], "title": r["title"] or r["track_title"], "track_id": r["track_id"],
            "position": r["position"], "start_s": r["link_start"], "end_s": r["link_end"]}


def get(conn: sqlite3.Connection, owner: str | None, deposit_id: int) -> dict[str, Any] | None:
    """One deposit with what it holds, what holds it and the tracks it touches (each as a short row), or None."""
    r = conn.execute(f"{_SELECT} WHERE d.id = ? AND d.owner IS ?", (deposit_id, owner)).fetchone()
    if r is None:
        return None
    out = _row(r, _tags_of(conn, [deposit_id])[deposit_id])
    brief = ("SELECT d.id, d.size, d.title, d.track_id, t.title AS track_title, l.position, "
             "l.start_s AS link_start, l.end_s AS link_end FROM deposit_links l "
             "JOIN deposits d ON d.id = l.{other} LEFT JOIN tracks t ON t.id = d.track_id "
             "WHERE l.{this} = ? AND l.kind = ? ORDER BY l.position IS NULL, l.position, l.start_s, d.id")
    for key, this, other, kind in (("holds", "from_id", "to_id", "holds"), ("held_by", "to_id", "from_id", "holds"),
                                   ("touches", "from_id", "to_id", "touches"),
                                   ("touched_by", "to_id", "from_id", "touches")):
        out[key] = [_brief(x) for x in conn.execute(brief.format(this=this, other=other), (deposit_id, kind))]
    return out


def update(conn: sqlite3.Connection, owner: str | None, deposit_id: int, changes: dict[str, Any]) -> dict[str, Any] | None:
    """Change a deposit's title, note, tags, status, context or extracted data (the keys present in `changes`;
    tags replace the ones of the origins they name, and an empty list clears the person's own, context and extracted are merged key by key). None: no such deposit."""
    row = _mine(conn, owner, deposit_id)
    if row is None:
        return None
    sets, args = [], []
    if "title" in changes:
        sets.append("title = ?")
        args.append(_text(changes["title"], MAX_TITLE, "title"))
    if "note" in changes:
        sets.append("note = ?")
        args.append(_text(changes["note"], MAX_NOTE, "note"))
    if "status" in changes:
        if changes["status"] not in STATUSES:
            raise DepositError(f"status is one of: {', '.join(STATUSES)}")
        sets.append("status = ?")
        args.append(changes["status"])
    for key in ("context", "extracted"):
        if changes.get(key) is not None:
            merged = {**db._json_or(row[f"{key}_json"], {}), **changes[key]}
            sets.append(f"{key}_json = ?")
            args.append(json.dumps({k: v for k, v in merged.items() if v is not None}))
    tag_rows = _clean_tags(changes["tags"]) if "tags" in changes else None
    # only the origins named are replaced; an empty list clears the person's own tags and leaves imported ones
    origins = sorted({o for _, o in tag_rows} or {"user"}) if tag_rows is not None else []

    def write() -> None:
        conn.execute(f"UPDATE deposits SET {', '.join([*sets, 'updated_at = ?'])} WHERE id = ?",
                     (*args, db._now(), deposit_id))
        if tag_rows is not None:
            conn.execute(f"DELETE FROM deposit_tags WHERE deposit_id = ? AND origin IN ({', '.join('?' * len(origins))})",
                         (deposit_id, *origins))
            conn.executemany("INSERT INTO deposit_tags (deposit_id, tag, origin) VALUES (?, ?, ?)",
                             [(deposit_id, tag, origin) for tag, origin in tag_rows])

    db.atomic(conn, write)
    return get(conn, owner, deposit_id)


def delete(conn: sqlite3.Connection, owner: str | None, deposit_id: int) -> bool:
    """Remove a deposit and its tags and links; what it held stays in the bank. A track-sized deposit whose
    track is still in the library can't go this way (it would come straight back): delete the track instead."""
    row = _mine(conn, owner, deposit_id)
    if row is None:
        return False
    if row["track_id"] is not None:
        raise DepositError("This deposit is a track in your library. Delete the track to remove it.")

    with conn:  # its tags and links go with it (ON DELETE CASCADE)
        conn.execute("DELETE FROM deposits WHERE id = ?", (deposit_id,))
    return True


def parse_time(text: str) -> float:
    """Seconds from "938", "15:38" or "1:02:03.5"."""
    try:
        parts = [float(p) for p in str(text).strip().split(":")]
    except ValueError:
        parts = []
    if not 1 <= len(parts) <= 3 or any(p < 0 for p in parts) or any(p >= 60 for p in parts[1:]):
        raise DepositError(f'Can\'t read "{text}" as a time; write it like 15:38 or 938.')
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds
