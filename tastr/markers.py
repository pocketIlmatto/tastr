"""Unfinished background jobs, kept in the database so a restarted server picks them up.

Stem separations, drum model runs and background analyses each leave a marker while they are queued or
running. These used to be small files under $TASTR_HOME; in the database they travel with it (Litestream on a
hosted server), so a server that comes back on a fresh machine with no disk of its own still finds them.

`Markers(db_path, kind)` is one kind's set: `put(key, data)`, `get(key)`, `remove(key)`, `items()`.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class Markers:
    def __init__(self, db_path: str | os.PathLike | None, kind: str):
        self.db_path, self.kind = db_path, kind

    def _conn(self) -> sqlite3.Connection:
        from . import db

        return db.connect(self.db_path)

    def put(self, key: str, data: dict[str, Any] | None = None) -> None:
        try:
            with closing(self._conn()) as c, c:
                c.execute("INSERT OR REPLACE INTO job_markers VALUES (?, ?, ?, ?)",
                          (self.kind, str(key), json.dumps(data or {}), time.time()))
        except sqlite3.Error as e:  # the job still runs; only a restart would forget it
            log.warning("couldn't save the %s job marker %s: %s", self.kind, key, e)

    def get(self, key: str) -> dict[str, Any] | None:
        with closing(self._conn()) as c:
            r = c.execute("SELECT data FROM job_markers WHERE kind = ? AND key = ?", (self.kind, str(key))).fetchone()
        return json.loads(r[0]) if r else None

    def remove(self, key: str) -> None:
        try:
            with closing(self._conn()) as c, c:
                c.execute("DELETE FROM job_markers WHERE kind = ? AND key = ?", (self.kind, str(key)))
        except sqlite3.Error as e:
            log.warning("couldn't remove the %s job marker %s: %s", self.kind, key, e)

    def items(self) -> list[tuple[str, dict[str, Any], float]]:
        """(key, data, last written as a Unix time) for every marker of this kind, oldest key first."""
        with closing(self._conn()) as c:
            rows = c.execute("SELECT key, data, updated_at FROM job_markers WHERE kind = ? ORDER BY key",
                             (self.kind,)).fetchall()
        return [(k, json.loads(d), t) for k, d, t in rows]

    def prune(self, before: float) -> int:
        """Delete markers last written before `before` (a Unix time); returns how many."""
        with closing(self._conn()) as c, c:
            return c.execute("DELETE FROM job_markers WHERE kind = ? AND updated_at < ?", (self.kind, before)).rowcount


def legacy_files(folder: Path, pattern: str) -> list[Path]:
    """Marker files an older server left on disk (read once by `resume()`, then deleted)."""
    return sorted(folder.glob(pattern)) if folder.is_dir() else []
