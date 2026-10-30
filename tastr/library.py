"""Move a library, with its audio and stems, from your machine to a hosted server (docs/deploy.md).

`tastr pack OUT.tar --owner EMAIL` writes one archive: a copy of the database with every track given to
EMAIL, each track's audio file (wherever it is on disk) and its stems. `tastr unpack OUT.tar` on the server
puts the files under TASTR_HOME and copies the database into the server's, which must have no tracks yet.
The copy goes through SQLite, so Litestream backs it up like any other change.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from contextlib import closing
from pathlib import Path

from . import db, stems

DB_NAME = "tastr.db"
FOLDERS = ("uploads", "stems")


def _set_local_path(conn: sqlite3.Connection, track_id: int, path: str | None) -> None:
    """Both places that hold it: the tracks row and the card's source."""
    conn.execute("UPDATE tracks SET local_path = ? WHERE id = ?", (path, track_id))
    row = conn.execute("SELECT json FROM cards WHERE track_id = ?", (track_id,)).fetchone()
    if row:
        card = json.loads(row[0])
        card["source"]["local_path"] = path
        conn.execute("UPDATE cards SET json = ? WHERE track_id = ?", (json.dumps(card), track_id))


def pack(db_path: str | os.PathLike | None, out: str | os.PathLike, owner: str) -> dict:
    """Write the archive. Audio paths in it are relative (uploads/<id>/<name>); `unpack` makes them absolute."""
    owner = owner.strip().lower()
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / DB_NAME
        with closing(db.connect(db_path)) as src, closing(sqlite3.connect(copy)) as dst:
            src.backup(dst)
        files: list[tuple[Path, str]] = []
        missing = 0
        with closing(sqlite3.connect(copy)) as c:
            for tid, path, sha in c.execute("SELECT id, local_path, sha256 FROM tracks").fetchall():
                if path and Path(path).is_file():
                    arc = f"uploads/{uuid.uuid4().hex[:8]}/{Path(path).name}"
                    files.append((Path(path), arc))
                    _set_local_path(c, tid, arc)
                else:
                    missing += path is not None
                    _set_local_path(c, tid, None)
                folder = stems.stems_dir(sha, tid)
                if stems.is_ready(folder):
                    files += [(stems.stem_path(folder, n), f"stems/{folder.name}/{n}.flac") for n in stems.STEM_NAMES]
            tracks = c.execute("UPDATE tracks SET owner = ? WHERE owner IS NULL", (owner,)).rowcount
            c.commit()
        added: set[str] = set()
        with tarfile.open(out, "w") as tar:
            tar.add(copy, DB_NAME)
            for path, arc in files:
                if arc not in added:  # tracks of the same file share their stems
                    added.add(arc)
                    tar.add(path, arc)
    return {"tracks": tracks, "files": len(added), "missing_audio": missing, "bytes": Path(out).stat().st_size}


def unpack(archive: str | os.PathLike, home: str | os.PathLike, db_path: str | os.PathLike | None = None) -> dict:
    """Put an archive from `pack` into this server's TASTR_HOME and database (which must have no tracks)."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    with closing(db.connect(db_path)) as live:
        if live.execute("SELECT 1 FROM tracks LIMIT 1").fetchone():
            raise ValueError("This server's library already has tracks; unpack only goes into an empty one.")
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(archive) as tar:
        copy = Path(tmp) / DB_NAME
        n = 0
        for m in tar:
            parts = Path(m.name).parts
            if m.name == DB_NAME and m.isfile():
                dest = copy
            elif m.isfile() and len(parts) >= 2 and parts[0] in FOLDERS and ".." not in parts and not Path(m.name).is_absolute():
                dest = home / m.name
                n += 1
            else:
                raise ValueError(f"unexpected entry in the archive: {m.name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(m) as f, open(dest, "wb") as out:
                shutil.copyfileobj(f, out)
        if not copy.is_file():
            raise ValueError(f"no {DB_NAME} in the archive")
        with closing(sqlite3.connect(copy)) as c:
            for tid, path in c.execute("SELECT id, local_path FROM tracks WHERE local_path IS NOT NULL").fetchall():
                _set_local_path(c, tid, str(home / path))
            c.commit()
            with closing(db.connect(db_path)) as live:
                c.backup(live)
            tracks = c.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    return {"tracks": tracks, "files": n}
