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
    sha256          TEXT,
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
    analyzed_at     TEXT NOT NULL,
    owner           TEXT,  -- who uploaded it on a hosted server, as an Access email; NULL on your own machine
    UNIQUE (sha256, owner)
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

-- The user's edits to a track's drums, bass and chords (the "Drums, bass & chords" view). The row with
-- name NULL is the current, autosaved state; named rows are versions saved to come back to. edits_json is
-- {drums|bass|chords|drumsrc: {section idx or "all": value}}. Re-analyzing keeps them (see remap_edits).
CREATE TABLE IF NOT EXISTS track_edits (
    id         INTEGER PRIMARY KEY,
    track_id   INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    name       TEXT,
    edits_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_track_edits_current ON track_edits(track_id) WHERE name IS NULL;
CREATE INDEX IF NOT EXISTS idx_track_edits_track ON track_edits(track_id);

-- What online catalogs list for a track (tastr.lookup), one row per provider, kept across re-analysis.
-- status: found | not_found | pending | no_tags | error. key_name is the key as listed; key_tonic / key_mode /
-- camelot are parsed from it, so they compare with tracks. result_json is the full LookupResult.
CREATE TABLE IF NOT EXISTS lookups (
    track_id    INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    provider    TEXT NOT NULL,
    status      TEXT NOT NULL,
    bpm         REAL,
    key_name    TEXT,
    key_tonic   INTEGER,
    key_mode    TEXT,
    camelot     TEXT,
    fetched_at  TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (track_id, provider)
);

-- Requests sent to each lookup provider per UTC day, for the guardrails in tastr/lookup/guard.py.
CREATE TABLE IF NOT EXISTS lookup_usage (
    provider TEXT NOT NULL,
    day      TEXT NOT NULL,
    requests INTEGER NOT NULL,
    PRIMARY KEY (provider, day)
);

-- Tracks uploaded and stem jobs started per person per UTC day, for the hosted-mode limits (tastr/web/hosted.py).
CREATE TABLE IF NOT EXISTS user_usage (
    user  TEXT NOT NULL,
    day   TEXT NOT NULL,
    kind  TEXT NOT NULL,
    n     INTEGER NOT NULL,
    PRIMARY KEY (user, day, kind)
);

CREATE INDEX IF NOT EXISTS idx_tracks_bpm ON tracks(bpm);
CREATE INDEX IF NOT EXISTS idx_tracks_owner ON tracks(owner);
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
        _add_owner_column(conn)
        conn.executescript(SCHEMA)
        if "album" not in {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}:
            conn.execute("ALTER TABLE tracks ADD COLUMN album TEXT")  # databases created before album existed
        conn.execute("INSERT OR IGNORE INTO meta VALUES ('db_schema_version', ?)", (str(DB_SCHEMA_VERSION),))
        conn.commit()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _add_owner_column(conn: sqlite3.Connection) -> None:
    """Databases from before per-person libraries had sha256 UNIQUE on its own, so two people couldn't upload
    the same file. SQLite can't drop that constraint in place, so the tracks table is rebuilt (ids kept)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tracks)")]
    if not cols or "owner" in cols:
        return
    ddl = SCHEMA.split("CREATE TABLE IF NOT EXISTS tracks", 1)[1].split(");", 1)[0] + ");"
    keep = ", ".join(cols)
    conn.executescript(f"""
        PRAGMA foreign_keys = OFF;
        BEGIN;
        CREATE TABLE tracks_new {ddl}
        INSERT INTO tracks_new ({keep}) SELECT {keep} FROM tracks;
        DROP TABLE tracks;
        ALTER TABLE tracks_new RENAME TO tracks;
        COMMIT;
    """)


def save_card(conn: sqlite3.Connection, card: TrackCard, owner: str | None = None) -> int:
    """Insert or replace a card (deduped on sha256, else on source_uri, within one owner's library). Returns
    track id. `owner` is who uploaded it on a hosted server; None on your own machine."""
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
        existing = find_track_id(conn, sha256=s.sha256, source_uri=s.uri, owner=owner)
        if existing is not None:
            sets = ", ".join(f"{col} = :{col}" for col in row)
            conn.execute(f"UPDATE tracks SET {sets} WHERE id = :id", {**row, "id": existing})
            track_id = existing
            for table in ("sections", "tags", "suggestions", "features", "series", "cards"):
                conn.execute(f"DELETE FROM {table} WHERE track_id = ?", (track_id,))
        else:
            row["owner"] = owner
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


def find_track_id(conn: sqlite3.Connection, sha256: str | None = None, source_uri: str | None = None,
                  owner: str | None = None) -> int | None:
    if sha256:
        r = conn.execute("SELECT id FROM tracks WHERE sha256 = ? AND owner IS ?", (sha256, owner)).fetchone()
        if r:
            return r[0]
    if source_uri:
        r = conn.execute("SELECT id FROM tracks WHERE source_uri = ? AND owner IS ?", (source_uri, owner)).fetchone()
        if r:
            return r[0]
    return None


def track_owner(conn: sqlite3.Connection, track_id: int) -> tuple[bool, str | None]:
    """(exists, owner) for one track."""
    r = conn.execute("SELECT owner FROM tracks WHERE id = ?", (track_id,)).fetchone()
    return (r is not None, r[0] if r else None)


def set_owner(conn: sqlite3.Connection, owner: str) -> int:
    """Give every track with no owner (analyzed on your own machine) to `owner`. Returns how many."""
    with conn:
        return conn.execute("UPDATE tracks SET owner = ? WHERE owner IS NULL", (owner,)).rowcount


def load_card(conn: sqlite3.Connection, track_id: int) -> TrackCard | None:
    r = conn.execute("SELECT json FROM cards WHERE track_id = ?", (track_id,)).fetchone()
    return TrackCard.from_dict(json.loads(r[0])) if r else None


def list_tracks(conn: sqlite3.Connection, owner: str | None = None) -> list[dict[str, Any]]:
    """All tracks, or only `owner`'s on a hosted server."""
    where, args = ("WHERE owner = ?", (owner,)) if owner else ("", ())
    rows = conn.execute(
        f"SELECT id, title, artist, duration, bpm, key_name, camelot, energy, analyzed_at FROM tracks {where} ORDER BY id",
        args,
    ).fetchall()
    return [dict(r) for r in rows]


def delete_track(conn: sqlite3.Connection, track_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,)).rowcount > 0


def save_lookup(conn: sqlite3.Connection, track_id: int, result: dict[str, Any]) -> None:
    """Store one provider's lookup result (a LookupResult dict, or {provider, status, error})."""
    with conn:
        conn.execute("INSERT OR REPLACE INTO lookups VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (track_id, result["provider"], result["status"], result.get("bpm"), result.get("key"),
                      result.get("tonic"), result.get("mode"), result.get("camelot"), _now(), json.dumps(result)))


def get_lookups(conn: sqlite3.Connection, track_id: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT result_json, fetched_at FROM lookups WHERE track_id = ? ORDER BY provider", (track_id,))
    return [json.loads(r[0]) | {"fetched_at": r[1]} for r in rows]


EDIT_PARTS = ("drums", "bass", "chords", "drumsrc")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def edited_sections(edits: dict[str, Any]) -> int:
    """How many sections (or "all") have an edit in any part."""
    return len({k for p in EDIT_PARTS for k in (edits.get(p) or {})})


def get_edits(conn: sqlite3.Connection, track_id: int) -> dict[str, Any]:
    r = conn.execute("SELECT edits_json FROM track_edits WHERE track_id = ? AND name IS NULL", (track_id,)).fetchone()
    return json.loads(r[0]) if r else {}


def set_edits(conn: sqlite3.Connection, track_id: int, edits: dict[str, Any]) -> None:
    """Replace the track's current edits; an object with no edited section clears them."""
    with conn:
        conn.execute("DELETE FROM track_edits WHERE track_id = ? AND name IS NULL", (track_id,))
        if edited_sections(edits):
            conn.execute("INSERT INTO track_edits (track_id, name, edits_json, created_at) VALUES (?, NULL, ?, ?)",
                         (track_id, json.dumps(edits), _now()))


def _version(r: sqlite3.Row, with_edits: bool = False) -> dict[str, Any]:
    edits = json.loads(r["edits_json"])
    v = {"id": r["id"], "name": r["name"], "created_at": r["created_at"], "sections": edited_sections(edits)}
    return {**v, "edits": edits} if with_edits else v


def list_edit_versions(conn: sqlite3.Connection, track_id: int) -> list[dict[str, Any]]:
    """Saved versions, newest first."""
    rows = conn.execute("SELECT * FROM track_edits WHERE track_id = ? AND name IS NOT NULL ORDER BY id DESC", (track_id,))
    return [_version(r) for r in rows]


def add_edit_version(conn: sqlite3.Connection, track_id: int, name: str, edits: dict[str, Any]) -> dict[str, Any]:
    with conn:
        vid = conn.execute("INSERT INTO track_edits (track_id, name, edits_json, created_at) VALUES (?, ?, ?, ?)",
                           (track_id, name, json.dumps(edits), _now())).lastrowid
    return get_edit_version(conn, track_id, vid)


def get_edit_version(conn: sqlite3.Connection, track_id: int, version_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM track_edits WHERE id = ? AND track_id = ? AND name IS NOT NULL", (version_id, track_id)).fetchone()
    return _version(r, with_edits=True) if r else None


def delete_edit_version(conn: sqlite3.Connection, track_id: int, version_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM track_edits WHERE id = ? AND track_id = ? AND name IS NOT NULL",
                            (version_id, track_id)).rowcount > 0


def remap_edits(conn: sqlite3.Connection, track_id: int, old_bars: dict[int, tuple], new_bars: dict[int, tuple],
                keep_all: bool = True) -> int:
    """After a re-analysis, move edits (current and saved versions) to the new section with the same bars.

    Edits are keyed by section index. `old_bars` and `new_bars` map index -> (start_bar, end_bar) before and
    after. An edit whose section's bars no longer exist is dropped; "all" (cards without sections) is kept.
    With keep_all False (the meter changed, so every bar has a different length) every edit is dropped.
    Returns how many section edits were dropped.
    """
    to_new = {bars: idx for idx, bars in new_bars.items()} if keep_all else {}
    dropped = 0
    rows = conn.execute("SELECT id, edits_json FROM track_edits WHERE track_id = ?", (track_id,)).fetchall()
    with conn:
        for row_id, raw in rows:
            edits, out = json.loads(raw), {}
            for part, by_key in edits.items():
                if not isinstance(by_key, dict):
                    continue
                kept = {}
                for k, v in by_key.items():
                    if k == "all" and keep_all:
                        kept[k] = v
                        continue
                    try:
                        bars = old_bars.get(int(k))
                    except ValueError:
                        bars = None
                    if bars is not None and bars in to_new:
                        kept[str(to_new[bars])] = v
                    elif part != "drumsrc":  # the template pick goes with the drums edit; count each section edit once
                        dropped += 1
                out[part] = kept
            conn.execute("UPDATE track_edits SET edits_json = ? WHERE id = ?", (json.dumps(out), row_id))
    return dropped


def query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def refresh_file_meta(conn: sqlite3.Connection, only_if_stale: bool = False) -> int:
    """Re-read embedded tags and audio info for file tracks whose file is still on disk.

    No audio analysis, so it fixes cards saved by an older tag reader. With
    `only_if_stale`, it does nothing unless the tag reader changed since the last
    refresh. Returns how many tracks changed.
    """
    from . import filemeta

    stored = conn.execute("SELECT value FROM meta WHERE key = 'filemeta_version'").fetchone()
    if only_if_stale and stored and stored[0] == str(filemeta.READER_VERSION):
        return 0
    rows = conn.execute("SELECT t.id FROM tracks t JOIN cards c ON c.track_id = t.id WHERE t.source_kind = 'file'").fetchall()
    changed = sum(retag_track(conn, track_id) == "changed" for (track_id,) in rows)
    if filemeta.mutagen is not None:  # without mutagen nothing was really read, so try again once it is installed
        with conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('filemeta_version', ?)", (str(filemeta.READER_VERSION),))
    return changed


def retag_track(conn: sqlite3.Connection, track_id: int) -> str:
    """Re-read one track's embedded tags and audio info, without re-analyzing.

    Returns "changed", "unchanged", "missing" (no card), "not_file" (a link
    track, nothing to re-read) or "no_file" (the file is no longer on disk).
    """
    from .filemeta import read_file_meta

    row = conn.execute("SELECT t.source_kind, t.local_path FROM tracks t JOIN cards c ON c.track_id = t.id WHERE t.id = ?", (track_id,)).fetchone()
    if row is None:
        return "missing"
    kind, local_path = row
    if kind != "file":
        return "not_file"
    if not local_path or not Path(local_path).is_file():
        return "no_file"
    card = load_card(conn, track_id)
    tags, info = read_file_meta(local_path)
    s = card.source
    title = tags.get("title") or Path(local_path).stem
    if (s.file_tags, s.audio_info, s.title, s.artist, s.album) == (tags, info, title, tags.get("artist"), tags.get("album")):
        return "unchanged"
    s.file_tags, s.audio_info, s.title, s.artist, s.album = tags, info, title, tags.get("artist"), tags.get("album")
    with conn:
        conn.execute("UPDATE tracks SET title = ?, artist = ?, album = ? WHERE id = ?", (s.title, s.artist, s.album, track_id))
        conn.execute("UPDATE cards SET json = ? WHERE track_id = ?", (card.to_json(), track_id))
    return "changed"


def usage_today(conn: sqlite3.Connection, user: str, kind: str, day: str) -> int:
    row = conn.execute("SELECT n FROM user_usage WHERE user = ? AND day = ? AND kind = ?", (user, day, kind)).fetchone()
    return row[0] if row else 0


def add_usage(conn: sqlite3.Connection, user: str, kind: str, day: str, n: int = 1) -> None:
    conn.execute("INSERT INTO user_usage VALUES (?, ?, ?, ?) ON CONFLICT (user, day, kind) DO UPDATE SET n = n + ?",
                 (user, day, kind, n, n))
    conn.commit()
