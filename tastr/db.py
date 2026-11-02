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
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import blobs, do_sqlite
from .engine.card import TrackCard

log = logging.getLogger(__name__)

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
    -- created_at: when it was first added (its upload), kept when it is re-analyzed
    created_at      TEXT,
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

-- What a track's BPM, key and section starts really are, set by hand (a correction or a confirmation;
-- tastr/labels.py). Analysis never changes them, and `tastr labels export` writes them as a bench manifest.
CREATE TABLE IF NOT EXISTS track_labels (
    track_id   INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    bpm        REAL,
    key_name   TEXT,     -- as given, e.g. "A minor"; checked by tastr.engine.bench.datasets.parse_key
    boundaries TEXT,     -- section starts in seconds, space-separated
    note       TEXT,
    updated_at TEXT NOT NULL,
    kept       INTEGER NOT NULL DEFAULT 0,  -- 1 when saved with "Keep as is": Tastr's own reading, confirmed
    tastr_bpm  REAL,     -- Tastr's BPM and key when the label was saved, so the review log can say what changed
    tastr_key  TEXT
);

-- Tracks you put in the review list yourself (flagged) or took out of it (skipped); see labels.review_queue.
-- Per track, so per person on a hosted server (a track has one owner). Re-analyzing keeps them.
CREATE TABLE IF NOT EXISTS review_flags (
    track_id   INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    state      TEXT NOT NULL CHECK (state IN ('flagged', 'skipped')),
    updated_at TEXT NOT NULL
);

-- Your verdict on a generated drum, bass or chord pattern in one section (tastr/labels.py, "Pattern reviews").
-- Keyed by the section's bars (0, 0 on a card without sections) so it outlives a re-analysis, like edits do.
-- judged_json is the detected pattern you judged and stage_version the suggestions stage that made it, so a
-- re-analysis that changes the pattern shows the mark as stale. truth_json is the right pattern, when you saved
-- your edit as the answer: what `tastr labels export --patterns` writes for a pattern bench.
CREATE TABLE IF NOT EXISTS pattern_reviews (
    track_id      INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    part          TEXT NOT NULL CHECK (part IN ('drums', 'bass', 'chords')),
    start_bar     INTEGER NOT NULL,
    end_bar       INTEGER NOT NULL,
    verdict       TEXT CHECK (verdict IN ('right', 'close', 'way_off')),
    truth_json    TEXT,
    judged_json   TEXT,
    stage_version TEXT,
    note          TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (track_id, part, start_bar, end_bar)
);

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

-- The same counts per UTC hour ('YYYY-MM-DDTHH'), kept two days, for the admin view's Today chart.
CREATE TABLE IF NOT EXISTS usage_hours (
    user  TEXT NOT NULL,
    hour  TEXT NOT NULL,
    kind  TEXT NOT NULL,
    n     INTEGER NOT NULL,
    PRIMARY KEY (user, hour, kind)
);

-- An admin's change to one person's daily limits (hosted only; daily_asks = "Ask Claude" in Strudel). NULL limit = the server default;
-- until_day = the UTC day a "today only" change ends after, NULL = until an admin changes it.
CREATE TABLE IF NOT EXISTS limit_overrides (
    email        TEXT PRIMARY KEY,
    daily_tracks INTEGER,
    daily_stems  INTEGER,
    until_day    TEXT,
    set_by       TEXT,
    set_at       TEXT NOT NULL
);

-- Bytes stored per owner ('' = no owner) at the last snapshot of each UTC day, for the admin view's Storage chart.
CREATE TABLE IF NOT EXISTS storage_days (
    day   TEXT NOT NULL,
    owner TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    PRIMARY KEY (day, owner)
);

-- What people did on a hosted server, for the admin view's activity log (kept about 90 days, see add_event).
-- kind: upload, stems, drums, reanalyze, signin, limit, failed, restart, deploy or admin; ok = 0 for limits hit and
-- failures. type: the admin filter's group for the kind (EVENT_TYPES); source: where it happened (EVENT_SOURCES).
-- link: where to read more (a deploy's GitHub Actions run).
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY,
    at     TEXT NOT NULL,
    user   TEXT,
    kind   TEXT NOT NULL,
    ok     INTEGER NOT NULL DEFAULT 1,
    detail TEXT,
    link   TEXT,
    type   TEXT,
    source TEXT NOT NULL DEFAULT 'app'
);

-- Searches people saved in Explore > Find tracks, per person (owner is the Access email on a hosted server,
-- NULL on your own machine). mode: simple (json is {groups, join, sort}) or sql (json is {sql}).
-- A name is unique per owner and mode, ignoring case: saving under the same name replaces it.
CREATE TABLE IF NOT EXISTS saved_searches (
    id         INTEGER PRIMARY KEY,
    owner      TEXT,
    mode       TEXT NOT NULL,
    name       TEXT NOT NULL,
    json       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_saved_searches_owner ON saved_searches(owner);

-- Lists of tracks people save from Library > Actions or Explore > Blend, per person (owner as in saved_searches).
-- A name is unique per owner, ignoring case. Deleting a track takes it out of every list.
CREATE TABLE IF NOT EXISTS lists (
    id         INTEGER PRIMARY KEY,
    owner      TEXT,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lists_owner ON lists(owner);
CREATE TABLE IF NOT EXISTS list_tracks (
    list_id  INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    pos      INTEGER NOT NULL,
    PRIMARY KEY (list_id, track_id)
);
CREATE INDEX IF NOT EXISTS idx_list_tracks_track ON list_tracks(track_id);

-- Strudel templates people write in "Try this in > Strudel" (TAS-67), per person (owner as in saved_searches).
-- text is Strudel code with {fill-ins} the page fills from the open track; Tastr never runs it.
CREATE TABLE IF NOT EXISTS strudel_templates (
    id         INTEGER PRIMARY KEY,
    owner      TEXT,
    name       TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strudel_templates_owner ON strudel_templates(owner);

-- Cue points people mark on a track's chart (GitHub #3), per person (owner as in saved_searches), at most 9
-- per track (tastr/web/routes/edits.py). t is seconds; color indexes the page's 9 cue colors. Re-analyzing keeps
-- them; deleting the track deletes them.
CREATE TABLE IF NOT EXISTS track_cues (
    owner    TEXT,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    t        REAL NOT NULL,
    name     TEXT NOT NULL,
    color    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_track_cues_track ON track_cues(track_id, owner);

-- Requests a hosted server refused for a missing or invalid Cloudflare Access token, per UTC day.
CREATE TABLE IF NOT EXISTS blocked_requests (
    day     TEXT PRIMARY KEY,
    n       INTEGER NOT NULL,
    last_at TEXT
);

-- Background jobs still queued or running (tastr/markers.py): stems, drum model runs, analyses.
CREATE TABLE IF NOT EXISTS job_markers (
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    data       TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (kind, key)
);

CREATE INDEX IF NOT EXISTS idx_tracks_bpm ON tracks(bpm);
CREATE INDEX IF NOT EXISTS idx_tracks_owner ON tracks(owner);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);
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


# On a Cloudflare Durable Object the library lives in the object's own SQL storage, not a file (tastr/do_sqlite.py):
# `use_storage(factory)` makes connect() hand out adapters over factory(path) instead. None: a SQLite file.
_storage_for = None
_migrated: set[Path] = set()  # with use_storage: databases whose schema is up to date (connect runs per request)


def use_storage(factory) -> None:
    global _storage_for
    _storage_for = factory
    _migrated.clear()


def atomic(conn, fn):
    """fn()'s writes all together or not at all: a transaction on a file, transactionSync on a Durable Object
    (where `with conn:` can't roll back; see tastr/do_sqlite.py)."""
    if isinstance(conn, do_sqlite.Connection):
        return conn.atomic(fn)
    with conn:
        return fn()


def connect(path: str | os.PathLike | None = None, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path or default_db_path())
    if _storage_for is not None:
        conn = do_sqlite.Connection(_storage_for(path))
    elif readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    if not readonly and not (_storage_for is not None and path in _migrated):
        _add_owner_column(conn)
        conn.executescript(SCHEMA)
        if "album" not in {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}:
            conn.execute("ALTER TABLE tracks ADD COLUMN album TEXT")  # databases created before album existed
        event_cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
        if "link" not in event_cols:
            conn.execute("ALTER TABLE events ADD COLUMN link TEXT")  # databases created before deploy links
        if "type" not in event_cols:  # databases from before the admin log's filters
            conn.execute("ALTER TABLE events ADD COLUMN type TEXT")
            conn.execute("ALTER TABLE events ADD COLUMN source TEXT NOT NULL DEFAULT 'app'")
            for kind, typ in EVENT_TYPE_OF.items():
                conn.execute("UPDATE events SET type = ? WHERE kind = ?", (typ, kind))
            conn.execute("UPDATE events SET source = 'flyio' WHERE kind IN ('deploy', 'restart')")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, at)")
        if "last_at" not in {r[1] for r in conn.execute("PRAGMA table_info(blocked_requests)")}:
            conn.execute("ALTER TABLE blocked_requests ADD COLUMN last_at TEXT")
        if "created_at" not in {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}:
            conn.execute("ALTER TABLE tracks ADD COLUMN created_at TEXT")
        # tracks from before upload times were kept: their last analysis is the best guess there is
        conn.execute("UPDATE tracks SET created_at = analyzed_at WHERE created_at IS NULL")
        if "daily_asks" not in {r[1] for r in conn.execute("PRAGMA table_info(limit_overrides)")}:
            conn.execute("ALTER TABLE limit_overrides ADD COLUMN daily_asks INTEGER")  # from before Ask Claude (TAS-67)
        label_cols = {r[1] for r in conn.execute("PRAGMA table_info(track_labels)")}
        for col, ddl in (("kept", "INTEGER NOT NULL DEFAULT 0"), ("tastr_bpm", "REAL"), ("tastr_key", "TEXT")):
            if col not in label_cols:
                conn.execute(f"ALTER TABLE track_labels ADD COLUMN {col} {ddl}")  # databases from before the review log
        conn.execute("INSERT OR IGNORE INTO meta VALUES ('db_schema_version', ?)", (str(DB_SCHEMA_VERSION),))
        conn.commit()
        if _storage_for is not None:
            _migrated.add(path)
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
    def write() -> int:
        existing = find_track_id(conn, sha256=s.sha256, source_uri=s.uri, owner=owner)
        if existing is not None:
            sets = ", ".join(f"{col} = :{col}" for col in row)
            conn.execute(f"UPDATE tracks SET {sets} WHERE id = :id", {**row, "id": existing})
            track_id = existing
            for table in ("sections", "tags", "suggestions", "features", "series", "cards"):
                conn.execute(f"DELETE FROM {table} WHERE track_id = ?", (track_id,))
        else:
            row["owner"] = owner
            row["created_at"] = row["analyzed_at"]
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
        conn.execute("INSERT INTO cards VALUES (?, ?, ?)", (track_id, card.schema_version, _card_json(card, track_id)))
        return track_id

    return atomic(conn, write)


def _card_json(card: TrackCard, track_id: int) -> str:
    """The card as stored. With the audio bucket on, its waveform and spectrogram data (about a megabyte) goes to
    the bucket and the card keeps only blobs.IN_BUCKET; if that upload fails the data stays in the card."""
    views = card.extras.get("views")
    if views is None or views == blobs.IN_BUCKET or not blobs.enabled():
        return card.to_json()
    try:
        blobs.put_views(track_id, views)
    except Exception as e:  # noqa: BLE001 - kept in the card instead; nothing is lost
        log.warning("couldn't store track %s's views in the bucket, keeping them in the card: %s", track_id, e)
        return card.to_json()
    d = card.to_dict()  # a deep copy: the caller's card keeps its data
    d["extras"]["views"] = blobs.IN_BUCKET
    return json.dumps(d)


D1_ROW_LIMIT = 2_000_000  # the largest row Cloudflare D1 takes, in bytes


def size_report(path: str | os.PathLike | None = None) -> dict[str, Any]:
    """How big the database is and what takes the room, for the move to D1 (`tastr db-size`): the file, each
    table and index, and the cards (the largest rows), with how much of them is inline waveform data."""
    path = Path(path or default_db_path())
    with closing(connect(path, readonly=True)) as conn:
        try:
            tables = dict(conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC"))
        except sqlite3.OperationalError:  # an SQLite built without the dbstat table
            tables = {}
        n, total, largest = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(json)), 0), COALESCE(MAX(LENGTH(json)), 0) FROM cards").fetchone()
        inline, inline_bytes = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(json_extract(json, '$.extras.views'))), 0) FROM cards "
            "WHERE json_type(json, '$.extras.views') = 'object' "
            "AND json_extract(json, '$.extras.views.in_bucket') IS NULL").fetchone()
        over = conn.execute("SELECT COUNT(*) FROM cards WHERE LENGTH(json) > ?", (D1_ROW_LIMIT,)).fetchone()[0]
    return {"file_bytes": path.stat().st_size, "tables": tables, "cards": n, "card_bytes": total,
            "largest_card_bytes": largest, "cards_over_d1_row_limit": over, "inline_views": inline,
            "inline_view_bytes": inline_bytes}


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
        f"SELECT id, title, artist, duration, bpm, key_name, camelot, energy, analyzed_at, created_at FROM tracks {where} "
        "ORDER BY id",
        args,
    ).fetchall()
    return [dict(r) for r in rows]


def delete_track(conn: sqlite3.Connection, track_id: int) -> bool:
    with conn:
        gone = conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,)).rowcount > 0
    if gone:
        try:
            blobs.remove_views(track_id)  # the card's waveform data, when it is in the audio bucket
        except Exception as e:  # noqa: BLE001 - an orphaned object costs a megabyte, not a failed delete
            log.warning("couldn't remove track %s's views from the bucket: %s", track_id, e)
    return gone


def save_lookup(conn: sqlite3.Connection, track_id: int, result: dict[str, Any]) -> None:
    """Store one provider's lookup result (a LookupResult dict, or {provider, status, error})."""
    with conn:
        conn.execute("INSERT OR REPLACE INTO lookups VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (track_id, result["provider"], result["status"], result.get("bpm"), result.get("key"),
                      result.get("tonic"), result.get("mode"), result.get("camelot"), _now(), json.dumps(result)))


def get_lookups(conn: sqlite3.Connection, track_id: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT result_json, fetched_at FROM lookups WHERE track_id = ? ORDER BY provider", (track_id,))
    return [json.loads(r[0]) | {"fetched_at": r[1]} for r in rows]


LABEL_FIELDS = ("bpm", "key_name", "boundaries", "note")
_LABEL_COLS = LABEL_FIELDS + ("updated_at", "kept", "tastr_bpm", "tastr_key")


def _label_row(r) -> dict[str, Any]:
    d = dict(zip(_LABEL_COLS, r))
    d["kept"] = bool(d["kept"])
    return d


def get_labels(conn: sqlite3.Connection, track_id: int) -> dict[str, Any]:
    r = conn.execute(f"SELECT {', '.join(_LABEL_COLS)} FROM track_labels WHERE track_id = ?", (track_id,)).fetchone()
    return _label_row(r) if r else {}


def set_labels(conn: sqlite3.Connection, track_id: int, labels: dict[str, Any]) -> dict[str, Any]:
    """Replace the track's labels with the given fields (checked by tastr.labels); none set clears them.
    `kept` marks a "Keep as is" (Tastr's own BPM and key, confirmed). Tastr's BPM and key at this moment are
    stored with the label, and saving one takes the track off the review list you put it on."""
    vals = [labels.get(f) for f in LABEL_FIELDS]
    with conn:
        conn.execute("DELETE FROM track_labels WHERE track_id = ?", (track_id,))
        if any(v not in (None, "") for v in vals[:3]):
            t = conn.execute("SELECT bpm, key_name FROM tracks WHERE id = ?", (track_id,)).fetchone()
            conn.execute(f"INSERT INTO track_labels (track_id, {', '.join(_LABEL_COLS)}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (track_id, *vals, _now(), int(bool(labels.get("kept"))), t[0] if t else None, t[1] if t else None))
            conn.execute("DELETE FROM review_flags WHERE track_id = ? AND state = 'flagged'", (track_id,))
    return get_labels(conn, track_id)


def list_labels(conn: sqlite3.Connection, owner: str | None = None) -> list[dict[str, Any]]:
    """Every labeled track with its audio path and title, oldest label first. tastr_bpm / tastr_key are Tastr's
    reading when the label was saved (its current one for labels saved before that was kept)."""
    sql = ("SELECT t.id, t.local_path, t.title, t.artist, l.bpm, l.key_name, l.boundaries, l.note, l.updated_at, l.kept, "
           "COALESCE(l.tastr_bpm, t.bpm), COALESCE(l.tastr_key, t.key_name) "
           "FROM track_labels l JOIN tracks t ON t.id = l.track_id")
    rows = conn.execute(sql + (" WHERE t.owner IS ?" if owner is not None else "") + " ORDER BY l.updated_at",
                        (owner,) if owner is not None else ())
    out = []
    for r in rows:
        d = dict(zip(("track_id", "path", "title", "artist", *_LABEL_COLS), r))
        d["kept"] = bool(d["kept"])
        out.append(d)
    return out


REVIEW_STATES = ("flagged", "skipped")


def review_flags(conn: sqlite3.Connection, owner: str | None = None) -> dict[int, str]:
    """Track id -> "flagged" (you put it in the review list) or "skipped" (you took it out)."""
    sql = "SELECT f.track_id, f.state FROM review_flags f JOIN tracks t ON t.id = f.track_id"
    rows = conn.execute(sql + (" WHERE t.owner IS ?" if owner is not None else ""), (owner,) if owner is not None else ())
    return dict(rows.fetchall())


def set_review_flag(conn: sqlite3.Connection, track_id: int, state: str | None) -> None:
    """Put a track in the review list ("flagged"), take it out ("skipped"), or leave it to the queue (None)."""
    if state is not None and state not in REVIEW_STATES:
        raise ValueError(f"state must be one of {', '.join(REVIEW_STATES)} or null")
    with conn:
        if state is None:
            conn.execute("DELETE FROM review_flags WHERE track_id = ?", (track_id,))
        else:
            conn.execute("INSERT OR REPLACE INTO review_flags VALUES (?, ?, ?)", (track_id, state, _now()))


PATTERN_PARTS = ("drums", "bass", "chords")
PATTERN_VERDICTS = ("right", "close", "way_off")
_PATTERN_COLS = ("part", "start_bar", "end_bar", "verdict", "truth_json", "judged_json", "stage_version", "note", "updated_at")


def _pattern_row(r) -> dict[str, Any]:
    d = dict(zip(_PATTERN_COLS, r))
    for k in ("truth", "judged"):
        raw = d.pop(f"{k}_json")
        d[k] = json.loads(raw) if raw else None
    return d


def get_pattern_reviews(conn: sqlite3.Connection, track_id: int) -> list[dict[str, Any]]:
    """The track's pattern reviews, in section order, drums before bass before chords."""
    rows = conn.execute(f"SELECT {', '.join(_PATTERN_COLS)} FROM pattern_reviews WHERE track_id = ? "
                        "ORDER BY start_bar, CASE part WHEN 'drums' THEN 0 WHEN 'bass' THEN 1 ELSE 2 END", (track_id,))
    return [_pattern_row(r) for r in rows]


def set_pattern_review(conn: sqlite3.Connection, track_id: int, part: str, start_bar: int, end_bar: int,
                       review: dict[str, Any] | None) -> None:
    """Store one section's review of one part (checked by tastr.labels.clean_pattern_review); None or a review
    with neither a verdict nor a right pattern removes it."""
    with conn:
        conn.execute("DELETE FROM pattern_reviews WHERE track_id = ? AND part = ? AND start_bar = ? AND end_bar = ?",
                     (track_id, part, start_bar, end_bar))
        if review and (review.get("verdict") or review.get("truth") is not None):
            def dump(v):
                return json.dumps(v) if v is not None else None
            conn.execute(f"INSERT INTO pattern_reviews (track_id, {', '.join(_PATTERN_COLS)}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (track_id, part, start_bar, end_bar, review.get("verdict"), dump(review.get("truth")),
                          dump(review.get("judged")), review.get("stage_version"), review.get("note"), _now()))


def list_pattern_reviews(conn: sqlite3.Connection, owner: str | None = None) -> list[dict[str, Any]]:
    """Every pattern review with its track's id, audio path and title, oldest first."""
    sql = (f"SELECT t.id, t.local_path, t.title, t.artist, {', '.join('p.' + c for c in _PATTERN_COLS)} "
           "FROM pattern_reviews p JOIN tracks t ON t.id = p.track_id")
    rows = conn.execute(sql + (" WHERE t.owner IS ?" if owner is not None else "") + " ORDER BY p.updated_at",
                        (owner,) if owner is not None else ())
    return [dict(zip(("track_id", "path", "title", "artist"), r[:4])) | _pattern_row(r[4:]) for r in map(tuple, rows)]


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


def list_searches(conn: sqlite3.Connection, owner: str | None) -> list[dict[str, Any]]:
    """One person's saved searches, oldest first (the order they show as chips)."""
    rows = conn.execute("SELECT id, mode, name, json FROM saved_searches WHERE owner IS ? ORDER BY id", (owner,))
    return [{"id": r["id"], "mode": r["mode"], "name": r["name"], "data": json.loads(r["json"])} for r in rows]


def save_search(conn: sqlite3.Connection, owner: str | None, mode: str, name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Save a search; one with the same name (any case) in the same mode is replaced, keeping its place."""
    with conn:
        r = conn.execute("SELECT id FROM saved_searches WHERE owner IS ? AND mode = ? AND lower(name) = lower(?)",
                         (owner, mode, name)).fetchone()
        if r:
            sid = r[0]
            conn.execute("UPDATE saved_searches SET name = ?, json = ?, updated_at = ? WHERE id = ?",
                         (name, json.dumps(data), _now(), sid))
        else:
            sid = conn.execute("INSERT INTO saved_searches (owner, mode, name, json, updated_at) VALUES (?, ?, ?, ?, ?)",
                               (owner, mode, name, json.dumps(data), _now())).lastrowid
    return {"id": sid, "mode": mode, "name": name, "data": data}


def delete_search(conn: sqlite3.Connection, owner: str | None, search_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM saved_searches WHERE id = ? AND owner IS ?", (search_id, owner)).rowcount > 0


def list_templates(conn: sqlite3.Connection, owner: str | None) -> list[dict[str, Any]]:
    """One person's Strudel templates, oldest first (the order they show as chips)."""
    rows = conn.execute("SELECT id, name, text, updated_at FROM strudel_templates WHERE owner IS ? ORDER BY id", (owner,))
    return [{"id": r["id"], "name": r["name"], "text": r["text"], "updated_at": r["updated_at"]} for r in rows]


def add_template(conn: sqlite3.Connection, owner: str | None, name: str, text: str) -> dict[str, Any]:
    now = _now()
    with conn:
        tid = conn.execute("INSERT INTO strudel_templates (owner, name, text, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                           (owner, name, text, now, now)).lastrowid
    return {"id": tid, "name": name, "text": text, "updated_at": now}


def update_template(conn: sqlite3.Connection, owner: str | None, template_id: int, name: str, text: str) -> dict[str, Any] | None:
    now = _now()
    with conn:
        n = conn.execute("UPDATE strudel_templates SET name = ?, text = ?, updated_at = ? WHERE id = ? AND owner IS ?",
                         (name, text, now, template_id, owner)).rowcount
    return {"id": template_id, "name": name, "text": text, "updated_at": now} if n else None


def delete_template(conn: sqlite3.Connection, owner: str | None, template_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM strudel_templates WHERE id = ? AND owner IS ?", (template_id, owner)).rowcount > 0


def get_cues(conn: sqlite3.Connection, owner: str | None, track_id: int) -> list[dict[str, Any]]:
    """One person's cue points on a track, by time."""
    rows = conn.execute("SELECT t, name, color FROM track_cues WHERE track_id = ? AND owner IS ? ORDER BY t",
                        (track_id, owner))
    return [{"t": r["t"], "name": r["name"], "color": r["color"]} for r in rows]


def set_cues(conn: sqlite3.Connection, owner: str | None, track_id: int, cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace one person's cue points on a track with `cues` ({t, name, color}, already checked)."""
    with conn:
        conn.execute("DELETE FROM track_cues WHERE track_id = ? AND owner IS ?", (track_id, owner))
        conn.executemany("INSERT INTO track_cues (owner, track_id, t, name, color) VALUES (?, ?, ?, ?, ?)",
                         [(owner, track_id, q["t"], q["name"], q["color"]) for q in cues])
    return get_cues(conn, owner, track_id)


def list_lists(conn: sqlite3.Connection, owner: str | None) -> list[dict[str, Any]]:
    """One person's lists, oldest first, each with its track ids in the order they were added."""
    rows = conn.execute("SELECT id, name, created_at FROM lists WHERE owner IS ? ORDER BY id", (owner,)).fetchall()
    ids: dict[int, list[int]] = {r["id"]: [] for r in rows}
    if ids:
        marks = ",".join("?" * len(ids))
        for lid, tid in conn.execute(f"SELECT list_id, track_id FROM list_tracks WHERE list_id IN ({marks}) "
                                     "ORDER BY list_id, pos", list(ids)):
            ids[lid].append(tid)
    return [{"id": r["id"], "name": r["name"], "created_at": r["created_at"], "track_ids": ids[r["id"]]} for r in rows]


def get_list(conn: sqlite3.Connection, owner: str | None, list_id: int) -> dict[str, Any] | None:
    return next((x for x in list_lists(conn, owner) if x["id"] == list_id), None)


def find_list(conn: sqlite3.Connection, owner: str | None, name: str) -> int | None:
    r = conn.execute("SELECT id FROM lists WHERE owner IS ? AND lower(name) = lower(?)", (owner, name)).fetchone()
    return r[0] if r else None


def create_list(conn: sqlite3.Connection, owner: str | None, name: str) -> int:
    with conn:
        return conn.execute("INSERT INTO lists (owner, name, created_at) VALUES (?, ?, ?)", (owner, name, _now())).lastrowid


def add_to_list(conn: sqlite3.Connection, list_id: int, track_ids: list[int]) -> int:
    """Append tracks not already in the list, in the given order; returns how many were added."""
    with conn:
        pos = conn.execute("SELECT coalesce(max(pos), -1) FROM list_tracks WHERE list_id = ?", (list_id,)).fetchone()[0]
        added = 0
        for tid in dict.fromkeys(track_ids):
            if conn.execute("INSERT OR IGNORE INTO list_tracks VALUES (?, ?, ?)", (list_id, tid, pos + 1)).rowcount:
                pos += 1
                added += 1
    return added


def set_list(conn: sqlite3.Connection, list_id: int, name: str | None, track_ids: list[int] | None) -> None:
    """Rename a list and/or replace its tracks."""
    with conn:
        if name is not None:
            conn.execute("UPDATE lists SET name = ? WHERE id = ?", (name, list_id))
        if track_ids is not None:
            conn.execute("DELETE FROM list_tracks WHERE list_id = ?", (list_id,))
            conn.executemany("INSERT INTO list_tracks VALUES (?, ?, ?)",
                             [(list_id, tid, i) for i, tid in enumerate(dict.fromkeys(track_ids))])


def delete_list(conn: sqlite3.Connection, owner: str | None, list_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM lists WHERE id = ? AND owner IS ?", (list_id, owner)).rowcount > 0


def vibe_scores(conn: sqlite3.Connection, owner: str | None = None) -> dict[int, dict[str, float]]:
    """Every vibe's 0-1 score per track, including the ones under the card's cut-off (Explore > Blend).

    The card keeps them all in extras["tags.vibe_scores"]; cards from before that fall back to the tags table."""
    where, args = ("WHERE t.owner = ?", (owner,)) if owner else ("", ())
    out: dict[int, dict[str, float]] = {}
    for tid, raw in conn.execute(
            "SELECT c.track_id, json_extract(c.json, '$.extras.\"tags.vibe_scores\"') FROM cards c "
            f"JOIN tracks t ON t.id = c.track_id {where}", args):
        if raw:
            out[tid] = {k: float(v) for k, v in json.loads(raw).items() if isinstance(v, (int, float))}
    for tid, tag, score in conn.execute(
            f"SELECT g.track_id, g.tag, g.score FROM tags g JOIN tracks t ON t.id = g.track_id {where}", args):
        if score is not None:
            out.setdefault(tid, {}).setdefault(tag, float(score))
    return out


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
    if blobs.ensure(local_path) is None:  # brought back from the audio bucket if only it has the file
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


# What a read-only query may do: read tables, call functions and use WITH RECURSIVE. Everything else (ATTACH, which
# could open or create another database file even on a read-only connection, PRAGMA, any write) is refused.
_QUERY_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}


# What a friend's search on a hosted server sees (library_snapshot): these tables, their own rows only, without the
# columns that name people or server paths.
LIBRARY_TABLES = ("tracks", "tags", "sections", "features", "series", "suggestions", "lookups", "track_labels",
                  "review_flags", "pattern_reviews")
_PRIVATE_COLUMNS = {"owner", "local_path", "source_uri", "stages_json", "errors_json"}


def library_snapshot(path: str | os.PathLike, owner: str | None) -> bytes:
    """A SQLite file of one person's library (LIBRARY_TABLES, their rows, no private columns), or with `owner`
    None every library's rows with all their columns (hosted admins), for the page to query in the browser with
    sql.js (TAS-49). Indexes on tracks are added so the page's searches stay quick. The rows are copied through a
    connection into a database in memory (no ATTACH or temporary file), so it works on a Durable Object too."""
    where, args = ("WHERE owner = ?", (owner,)) if owner is not None else ("", ())
    with closing(connect(path, readonly=True)) as src, closing(sqlite3.connect(":memory:")) as dst:
        for t in LIBRARY_TABLES:
            cols = [(r[1], r[2]) for r in src.execute(f"PRAGMA table_info({t})")
                    if t != "tracks" or owner is None or r[1] not in _PRIVATE_COLUMNS]
            names = ", ".join(f'"{c}"' for c, _ in cols)
            dst.execute(f"CREATE TABLE {t} ({', '.join(f'{chr(34)}{c}{chr(34)} {typ}' for c, typ in cols)})")
            sql = (f"SELECT {names} FROM tracks {where}" if t == "tracks" else
                   f"SELECT {names} FROM {t} WHERE track_id IN (SELECT id FROM tracks {where})")
            rows = [tuple(r) for r in src.execute(sql, args).fetchall()]
            if rows:
                dst.executemany(f"INSERT INTO {t} VALUES ({', '.join('?' * len(cols))})", rows)
        for name, cols in (("tracks", "id"), ("tracks", "bpm"), ("tracks", "camelot"), ("tags", "tag"),
                           ("tags", "track_id"), ("features", "name, value")):
            dst.execute(f"CREATE INDEX idx_{name}_{cols.split(',')[0]} ON {name}({cols})")
        dst.commit()
        return dst.serialize()


def run_query(path: str | os.PathLike, sql: str, max_rows: int, seconds: float = 5.0) -> tuple[list[str], list[list]]:
    """Run one SELECT (`tastr query`, and the SQL box on your own machine): only the actions above, stopped after
    `seconds`, at most `max_rows` rows. A hosted server runs searches in the browser instead (library_snapshot)."""
    deadline = time.monotonic() + seconds
    with closing(connect(path, readonly=True)) as conn:
        conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK if action in _QUERY_ACTIONS else sqlite3.SQLITE_DENY)
        conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)  # a true return interrupts the query
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        return columns, [list(r) for r in cur.fetchmany(max_rows)]


def usage_today(conn: sqlite3.Connection, user: str, kind: str, day: str) -> int:
    row = conn.execute("SELECT n FROM user_usage WHERE user = ? AND day = ? AND kind = ?", (user, day, kind)).fetchone()
    return row[0] if row else 0


HOUR_DAYS = 2  # usage_hours rows kept


def add_usage(conn: sqlite3.Connection, user: str, kind: str, day: str, n: int = 1, hour: str | None = None) -> None:
    """Count `n` of `kind` for `user` on `day`, and in the current UTC hour of that day (or `hour`, 'YYYY-MM-DDTHH')."""
    hour = hour or f"{day}T{datetime.now(timezone.utc):%H}"
    with conn:
        conn.execute("INSERT INTO user_usage VALUES (?, ?, ?, ?) ON CONFLICT (user, day, kind) DO UPDATE SET n = n + ?",
                     (user, day, kind, n, n))
        conn.execute("INSERT INTO usage_hours VALUES (?, ?, ?, ?) ON CONFLICT (user, hour, kind) DO UPDATE SET n = n + ?",
                     (user, hour, kind, n, n))
        conn.execute("DELETE FROM usage_hours WHERE hour < strftime('%Y-%m-%dT%H', 'now', ?)", (f"-{HOUR_DAYS} days",))


def usage_hours(conn: sqlite3.Connection, day: str) -> list[dict[str, Any]]:
    """Each person's counts per hour of `day`: [{user, hour (0-23), kind, n}]."""
    rows = conn.execute("SELECT user, hour, kind, n FROM usage_hours WHERE hour >= ? AND hour < ? ORDER BY hour",
                        (f"{day}T00", f"{day}T99"))
    return [{"user": u, "hour": int(h[-2:]), "kind": k, "n": n} for u, h, k, n in rows]


def has_usage(conn: sqlite3.Connection, user: str) -> bool:
    """Whether `user` has any usage row yet, i.e. has opened the app before."""
    return conn.execute("SELECT 1 FROM user_usage WHERE user = ? LIMIT 1", (user,)).fetchone() is not None


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


# ---- per-person daily limits (hosted) ----

def get_override(conn: sqlite3.Connection, email: str, day: str) -> dict[str, Any] | None:
    """`email`'s limit change that still applies on `day`, or None. A "today only" change ends after its day."""
    row = conn.execute(f"SELECT {_OVERRIDE_COLS} FROM limit_overrides "
                       "WHERE email = ? AND (until_day IS NULL OR until_day >= ?)", (email, day)).fetchone()
    return _override(row) if row else None


_OVERRIDE_COLS = "daily_tracks, daily_stems, daily_asks, until_day, set_by, set_at"


def _override(row) -> dict[str, Any]:
    tracks, stems_, asks, until, by, at = row
    return {"daily_tracks": tracks, "daily_stems": stems_, "daily_asks": asks, "until_day": until, "set_by": by, "set_at": at}


def overrides(conn: sqlite3.Connection, day: str) -> dict[str, dict[str, Any]]:
    """Every limit change that still applies on `day`, by email."""
    return {r[0]: _override(r[1:]) for r in conn.execute(
        f"SELECT email, {_OVERRIDE_COLS} FROM limit_overrides WHERE until_day IS NULL OR until_day >= ?", (day,))}


def set_override(conn: sqlite3.Connection, email: str, daily_tracks: int | None, daily_stems: int | None,
                 until_day: str | None, set_by: str | None, daily_asks: int | None = None) -> None:
    """Change `email`'s daily limits (None = the server default for that one); all None goes back to the defaults."""
    with conn:
        if daily_tracks is None and daily_stems is None and daily_asks is None:
            conn.execute("DELETE FROM limit_overrides WHERE email = ?", (email,))
            return
        conn.execute(f"INSERT OR REPLACE INTO limit_overrides (email, {_OVERRIDE_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (email, daily_tracks, daily_stems, daily_asks, until_day, set_by, _now()))


def daily_cap(conn: sqlite3.Connection, email: str, kind: str, day: str, default: int) -> int:
    """The daily cap on `kind` ("tracks", "stems" or "asks") for `email` on `day`: their override if one applies, else `default`."""
    o = get_override(conn, email, day)
    v = o and o.get(f"daily_{kind}")
    return default if v is None else v


# ---- storage history (hosted) ----

def snapshot_storage(conn: sqlite3.Connection, day: str, by_owner: dict[str | None, int]) -> None:
    """Record bytes stored per owner for `day`; a later snapshot the same day replaces it, so each day keeps its last."""
    with conn:
        conn.executemany("INSERT OR REPLACE INTO storage_days VALUES (?, ?, ?)",
                         [(day, owner or "", int(b)) for owner, b in by_owner.items()])


def storage_history(conn: sqlite3.Connection, since: str) -> list[dict[str, Any]]:
    return [{"day": d, "owner": o or None, "bytes": b} for d, o, b in conn.execute(
        "SELECT day, owner, bytes FROM storage_days WHERE day >= ? ORDER BY day", (since,))]


def first_storage_day(conn: sqlite3.Connection) -> str | None:
    return conn.execute("SELECT min(day) FROM storage_days").fetchone()[0]


# ---- admin activity log (hosted) ----

EVENT_DAYS = 90
# The admin log's filter groups (type) for each kind of event, and where events come from (source).
EVENT_TYPE_OF = {"upload": "upload", "reanalyze": "analysis", "drums": "analysis", "stems": "stems", "limit": "limit",
                 "failed": "error", "restart": "error", "signin": "signin", "deploy": "deploy", "ci": "deploy",
                 "backup": "backup", "admin": "admin"}
EVENT_TYPES = ("upload", "analysis", "stems", "limit", "error", "signin", "blocked", "deploy", "backup", "admin")
EVENT_SOURCES = ("app", "modal", "flyio", "github", "cloudflare", "r2")


def add_event(conn: sqlite3.Connection, user: str | None, kind: str, detail: str, ok: bool = True,
              link: str | None = None, source: str = "app") -> None:
    """One line for the admin activity log; rows older than EVENT_DAYS go at the same time."""
    with conn:
        conn.execute("INSERT INTO events (at, user, kind, ok, detail, link, type, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (_now(), user, kind, int(ok), detail, link, EVENT_TYPE_OF.get(kind, kind), source))
        conn.execute("DELETE FROM events WHERE at < strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)", (f"-{EVENT_DAYS} days",))


def _event_row(at, user, kind, ok, detail, link, typ, source) -> dict[str, Any]:
    return {"at": at, "user": user, "kind": kind, "ok": bool(ok), "detail": detail, "link": link,
            "type": typ or EVENT_TYPE_OF.get(kind, kind), "source": source or "app"}


EVENT_COLS = "at, user, kind, ok, detail, link, type, source"


def recent_events(conn: sqlite3.Connection, n: int = 20, problems: bool = False) -> list[dict[str, Any]]:
    """The last `n` admin log lines; with `problems`, only failures, limits hit and restarts that cut work off."""
    where = "WHERE ok = 0 " if problems else ""
    rows = conn.execute(f"SELECT {EVENT_COLS} FROM events {where}ORDER BY at DESC, id DESC LIMIT ?", (n,))
    return [_event_row(*r) for r in rows]


def _blocked_lines(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The blocked-request count per day, as Cloudflare log lines."""
    rows = conn.execute("SELECT day, n, last_at FROM blocked_requests WHERE day >= date('now', ?)", (f"-{EVENT_DAYS} days",))
    return [{"at": last_at or f"{day}T23:59:59+00:00", "user": None, "kind": "blocked", "ok": False,
             "detail": f"{n} request{'' if n == 1 else 's'} blocked without a valid Access token",
             "link": None, "type": "blocked", "source": "cloudflare"} for day, n, last_at in rows]


def _event_filters(type: str, who: str, source: str, since: str, until: str, problems: bool) -> tuple[list[str], list]:
    """SQL conditions (joined with AND) and their arguments for find_events' filters."""
    conds = [("type = ?", type), ("source = ?", source), ("at >= ?", since),
             ("at < ?", until and until + "T99")]  # until: through the end of that day
    if who == "-":
        conds.append(("user IS NULL", None))
    elif who:
        conds.append(("user = ?", who))
    if problems:
        conds.append(("ok = 0", None))
    where = [c for c, v in conds if v is None or v]
    return where, [v for _, v in conds if v]


def find_events(conn: sqlite3.Connection, type: str = "", who: str = "", source: str = "", since: str = "",
                until: str = "", problems: bool = False, limit: int = 200) -> dict[str, Any]:
    """Admin log lines matching every filter given, newest first, with the blocked-request count per day as
    Cloudflare lines. who "-" = lines with no person. since/until are UTC days, both included.
    Returns {events (at most `limit`), matched, total, people (everyone named in the log)}."""
    blocked = _blocked_lines(conn)
    total = conn.execute("SELECT count(*) FROM events").fetchone()[0] + len(blocked)
    where, args = _event_filters(type, who, source, since, until, problems)
    sql = f"SELECT {EVENT_COLS} FROM events {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY at DESC, id DESC"
    matched = conn.execute(f"SELECT count(*) FROM ({sql})", args).fetchone()[0]
    rows = [_event_row(*r) for r in conn.execute(sql + " LIMIT ?", (*args, limit))]
    extra = []
    if type in ("", "blocked") and source in ("", "cloudflare") and who in ("", "-"):
        extra = [b for b in blocked if b["at"] >= since and (not until or b["at"] < until + "T99")]
    rows = sorted(rows + extra, key=lambda e: e["at"], reverse=True)[:limit]
    people = [u for (u,) in conn.execute("SELECT DISTINCT user FROM events WHERE user IS NOT NULL ORDER BY user")]
    return {"events": rows, "matched": matched + len(extra), "total": total, "people": people}


def add_blocked(conn: sqlite3.Connection, day: str) -> None:
    with conn:
        conn.execute("INSERT INTO blocked_requests VALUES (?, 1, ?) ON CONFLICT (day) DO UPDATE SET n = n + 1, last_at = ?",
                     (day, _now(), _now()))


def blocked_on(conn: sqlite3.Connection, day: str) -> int:
    row = conn.execute("SELECT n FROM blocked_requests WHERE day = ?", (day,)).fetchone()
    return row[0] if row else 0
