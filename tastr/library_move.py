"""Moving the hosted library from Fly to the Cloudflare Worker (TAS-51): a copy of the database through the audio
bucket.

1. On Fly, `tastr library-to-bucket` writes a consistent copy of the database (SQLite's backup API, so the
   running server doesn't have to stop) to the audio bucket as `import/library.db`. Audio and stems are in the
   bucket already, under the same names on both servers.
2. When the Worker starts (worker/src/entry.py) with a library that has never been imported and has no tracks,
   and every few minutes after that while it is still `waiting` (the copy may land after the Worker starts),
   `import_from_bucket` copies every table's rows from that file into the Durable Object's database, in one
   transaction, and records the import in `meta` so it never runs twice.

Jobs a server left half-done (`job_markers`) aren't copied: the Worker starts with none running.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
import time
from pathlib import Path

from . import blobs, db

log = logging.getLogger(__name__)

KEY = "import/library.db"
DONE = "library_imported"  # meta: when, and from what, the library was imported
TRIES = "library_import_tries"  # meta: imports started; one cut off (the Worker's CPU limit) never finishes
MAX_TRIES = 3
SKIP_TABLES = {"job_markers"}
SKIP_META = {"db_schema_version", DONE, TRIES}


def to_bucket(db_path: str | Path | None) -> int:
    """Copy the database to the bucket as KEY; returns its size in bytes."""
    if not blobs.enabled():
        raise RuntimeError("the audio bucket is off (TASTR_AUDIO_BUCKET)")
    src = sqlite3.connect(Path(db_path or db.default_db_path()))
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "library.db"
        dst = sqlite3.connect(copy)
        with dst:
            src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")  # Fly's is WAL; the copy must open from memory (import_rows)
        dst.close()
        src.close()
        blobs.client().put(KEY, copy, "application/vnd.sqlite3")
        return copy.stat().st_size


def import_rows(conn, data: bytes) -> dict[str, int]:
    """Copy every table's rows from the SQLite file `data` into `conn` (an empty, migrated library), for the
    columns both have. Returns {table: rows}. One transaction: all of it or none."""
    if data[18:20] == b"\x02\x02":
        # A WAL-mode file (Fly's database is, and a backup keeps the mark). SQLite can't open a WAL database
        # from memory ("unable to open database file"); bytes 18 and 19 set to 1 make it a plain one, which it is
        # once copied (the backup leaves nothing in a WAL file).
        data = data[:18] + b"\x01\x01" + data[20:]
    src = sqlite3.connect(":memory:")
    src.deserialize(data)
    theirs = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    # Our tables in the order the schema makes them, parents before the tables that point at them.
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                                         "ORDER BY rowid") if r[0] in theirs and r[0] not in SKIP_TABLES]
    keep_meta = f" WHERE key NOT IN ({', '.join('?' * len(SKIP_META))})"
    storage = getattr(conn, "storage", None)  # tastr/do_sqlite.py: each adapter call costs CPU the Worker caps
    counts: dict[str, int] = {}

    def copy():
        for t in reversed(tables):  # what startup wrote (an admin log line, say) goes, children first
            conn.execute(f'DELETE FROM "{t}"' + (keep_meta if t == "meta" else ""), tuple(SKIP_META) if t == "meta" else ())
        for t in tables:
            ours = {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')}
            cols = [r[1] for r in src.execute(f'PRAGMA table_info("{t}")') if r[1] in ours]
            names = ", ".join(f'"{c}"' for c in cols)
            rows = src.execute(f'SELECT {names} FROM "{t}"' + (keep_meta if t == "meta" else ""),
                               tuple(SKIP_META) if t == "meta" else ()).fetchall()
            sql = f'INSERT INTO "{t}" ({names}) VALUES ({", ".join("?" * len(cols))})'
            if storage is not None:  # the Durable Object: straight to its SQL, without the adapter's row counts
                for row in rows:
                    storage.exec(sql, *row)
            elif rows:
                conn.executemany(sql, rows)
            counts[t] = len(rows)

    db.atomic(conn, copy)
    src.close()
    return counts


def waiting(conn) -> bool:
    """Whether this library could still take an import: the bucket is on, it never imported, and it has no tracks
    (a library that is in use already is never overwritten)."""
    return (blobs.enabled() and not db.get_meta(conn, DONE) and tries(conn) < MAX_TRIES
            and not conn.execute("SELECT 1 FROM tracks LIMIT 1").fetchone())


def tries(conn) -> int:
    return int(db.get_meta(conn, TRIES) or 0)


def import_from_bucket(conn) -> dict[str, int] | None:
    """Import KEY into `conn` once, if the library is still empty; returns the counts, or None if nothing ran."""
    if not waiting(conn):
        return None
    size = blobs.client().head(KEY)
    if size is None:
        return None
    # Counted before it starts, in its own transaction: if the Worker is cut off mid-import (its CPU limit), the
    # import's transaction is rolled back but this stays, so it stops after MAX_TRIES instead of looping.
    db.set_meta(conn, TRIES, str(tries(conn) + 1))
    started = time.monotonic()
    counts = import_rows(conn, blobs.client().get(KEY).read())
    db.set_meta(conn, DONE, f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} from {KEY} ({size} bytes)")
    log.info("Imported the library from %s in %.1f s: %s", KEY, time.monotonic() - started, counts)
    return counts
