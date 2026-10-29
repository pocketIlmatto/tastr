"""SQLite storage for track cards.

Layout (see docs/ARCHITECTURE.md): one row per track in `tracks` with the
headline numbers as real columns, child tables for sections / tags /
suggestions / features / series, and the full card JSON in `cards` so nothing
a stage produces is lost. Re-saving a track replaces all its child rows.

DuckDB can read this file directly for heavier analysis:
    INSTALL sqlite; LOAD sqlite; ATTACH 'tastr.db' AS t (TYPE sqlite);
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .engine.card import TrackCard

DB_SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS tracks (
    id              INTEGER PRIMARY KEY,
    sha256          TEXT UNIQUE,
    source_kind     TEXT NOT NULL,
    source_uri      TEXT NOT NULL,
    local_path      TEXT,
    title           TEXT,
    artist          TEXT,
    album           TEXT,
    duration        REAL,
    sample_rate     INTEGER,
    bpm             REAL,
    bpm_confidence  REAL,
    key_tonic       INTEGER,
    key_mode        TEXT,
    key_name        TEXT,
    key_scale       TEXT,
    camelot         TEXT,
    key_confidence  REAL,
    energy          REAL,
    loudness_db     REAL,
    stages_json     TEXT,
    errors_json     TEXT,
    analyzed_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sections (
    track_id   INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    start      REAL NOT NULL,
    "end"      REAL NOT NULL,
    label      TEXT,
    letter     TEXT,
    start_bar  INTEGER,
    end_bar    INTEGER,
    energy     REAL,
    confidence REAL,
    PRIMARY KEY (track_id, idx)
);

CREATE TABLE IF NOT EXISTS tags (
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    tag      TEXT NOT NULL,
    score    REAL,
    PRIMARY KEY (track_id, tag)
);

CREATE TABLE IF NOT EXISTS suggestions (
    track_id  INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    category  TEXT,
    title     TEXT,
    body      TEXT,
    data_json TEXT,
    PRIMARY KEY (track_id, idx)
);

CREATE TABLE IF NOT EXISTS features (
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    value    REAL,
    PRIMARY KEY (track_id, name)
);

CREATE TABLE IF NOT EXISTS series (
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    idx      INTEGER NOT NULL,
    t        REAL NOT NULL,
    value    REAL,
    PRIMARY KEY (track_id, name, idx)
);

CREATE TABLE IF NOT EXISTS cards (
    track_id       INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    schema_version INTEGER NOT NULL,
    json           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tracks_bpm ON tracks(bpm);
CREATE INDEX IF NOT EXISTS idx_tracks_camelot ON tracks(camelot);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
CREATE INDEX IF NOT EXISTS idx_features_name ON features(name, value);
"""


def default_db_path() -> Path:
    env = os.environ.get("TASTR_DB")
    if env:
        return Path(env)
    home = Path(os.environ.get("TASTR_HOME", Path.home() / ".tastr"))
    home.mkdir(parents=True, exist_ok=True)
    return home / "tastr.db"


def connect(path: str | os.PathLike | None = None, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path or default_db_path())
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        if "album" not in {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}:
            conn.execute("ALTER TABLE tracks ADD COLUMN album TEXT")  # databases created before album existed
        conn.execute("INSERT OR IGNORE INTO meta VALUES ('db_schema_version', ?)", (str(DB_SCHEMA_VERSION),))
        conn.commit()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def save_card(conn: sqlite3.Connection, card: TrackCard) -> int:
    """Insert or replace a card (deduped on sha256, else on source_uri). Returns track id."""
    s, c = card.source, card.core
    k = c.key
    row = {
        "sha256": s.sha256,
        "source_kind": s.kind,
        "source_uri": s.uri,
        "local_path": s.local_path,
        "title": s.title,
        "artist": s.artist,
        "album": s.album,
        "duration": card.duration,
        "sample_rate": card.sample_rate,
        "bpm": c.bpm,
        "bpm_confidence": c.bpm_confidence,
        "key_tonic": k.tonic if k else None,
        "key_mode": k.mode if k else None,
        "key_name": k.name if k else None,
        "key_scale": k.scale if k else None,
        "camelot": k.camelot if k else None,
        "key_confidence": k.confidence if k else None,
        "energy": c.energy,
        "loudness_db": c.loudness_db,
        "stages_json": json.dumps(card.stages_run),
        "errors_json": json.dumps(card.errors),
        "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with conn:
        existing = find_track_id(conn, sha256=s.sha256, source_uri=s.uri)
        if existing is not None:
            sets = ", ".join(f"{col} = :{col}" for col in row)
            conn.execute(f"UPDATE tracks SET {sets} WHERE id = :id", {**row, "id": existing})
            track_id = existing
            for table in ("sections", "tags", "suggestions", "features", "series", "cards"):
                conn.execute(f"DELETE FROM {table} WHERE track_id = ?", (track_id,))
        else:
            cols = ", ".join(row)
            vals = ", ".join(f":{col}" for col in row)
            track_id = conn.execute(f"INSERT INTO tracks ({cols}) VALUES ({vals})", row).lastrowid

        conn.executemany(
            'INSERT INTO sections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            [(track_id, i, x.start, x.end, x.label, x.letter, x.start_bar, x.end_bar, x.energy, x.confidence)
             for i, x in enumerate(card.sections)],
        )
        conn.executemany("INSERT OR REPLACE INTO tags VALUES (?, ?, ?)", [(track_id, t.tag, t.score) for t in card.tags])
        conn.executemany(
            "INSERT INTO suggestions VALUES (?, ?, ?, ?, ?, ?)",
            [(track_id, i, x.category, x.title, x.body, json.dumps(x.data)) for i, x in enumerate(card.suggestions)],
        )
        conn.executemany(
            "INSERT INTO features VALUES (?, ?, ?)",
            [(track_id, name, float(v)) for name, v in card.features.items() if v is not None],
        )
        conn.executemany(
            "INSERT INTO series VALUES (?, ?, ?, ?, ?)",
            [(track_id, name, i, t, v)
             for name, ser in card.series.items() for i, (t, v) in enumerate(zip(ser.times, ser.values))],
        )
        conn.execute("INSERT INTO cards VALUES (?, ?, ?)", (track_id, card.schema_version, card.to_json()))
    return track_id


def find_track_id(conn: sqlite3.Connection, sha256: str | None = None, source_uri: str | None = None) -> int | None:
    if sha256:
        r = conn.execute("SELECT id FROM tracks WHERE sha256 = ?", (sha256,)).fetchone()
        if r:
            return r[0]
    if source_uri:
        r = conn.execute("SELECT id FROM tracks WHERE source_uri = ?", (source_uri,)).fetchone()
        if r:
            return r[0]
    return None


def load_card(conn: sqlite3.Connection, track_id: int) -> TrackCard | None:
    r = conn.execute("SELECT json FROM cards WHERE track_id = ?", (track_id,)).fetchone()
    return TrackCard.from_dict(json.loads(r[0])) if r else None


def list_tracks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, title, artist, duration, bpm, key_name, camelot, energy, analyzed_at FROM tracks ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def delete_track(conn: sqlite3.Connection, track_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,)).rowcount > 0


def query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
