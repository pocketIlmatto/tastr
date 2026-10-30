"""End-to-end: inputs -> sources -> decoded audio -> pipeline -> stored cards.

This is the entry point CLIs and web handlers should call.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import db
from .engine import analyze_file
from .engine.card import Source, TrackCard
from .ingest import file_sha256, resolve_inputs

log = logging.getLogger(__name__)


@dataclass
class AnalyzeResult:
    source: Source
    track_id: int | None
    card: TrackCard | None
    error: str | None = None
    edits_dropped: int = 0  # reanalyze_track: saved section edits whose section is gone


def analyze_source(conn: sqlite3.Connection, source: Source, stages: list[str] | None = None) -> AnalyzeResult:
    try:
        if not source.local_path:
            raise ValueError(f"source {source.uri} has no local audio file")
        source.sha256 = source.sha256 or file_sha256(source.local_path)
        card = analyze_file(source.local_path, stages, source=source)
        return AnalyzeResult(source, db.save_card(conn, card), card)
    except Exception as e:
        log.warning("failed to analyze %s: %s", source.uri, e)
        return AnalyzeResult(source, None, None, f"{type(e).__name__}: {e}")


def analyze_inputs(conn: sqlite3.Connection, inputs: list[str], stages: list[str] | None = None) -> list[AnalyzeResult]:
    return [analyze_source(conn, s, stages) for s in resolve_inputs(inputs)]


def reanalyze_track(conn: sqlite3.Connection, track_id: int) -> AnalyzeResult | None:
    """Run the full engine again on a stored track's audio file, so it picks up engine changes.

    Keeps the track's id and source (links stay links). File tracks also get their tags
    re-read. Returns None if there is no such track; raises FileNotFoundError if its
    audio file is no longer on disk. A failed analysis leaves the stored card as it was.
    Saved edits follow their section to the new one with the same bars; the rest are dropped
    (counted in `edits_dropped`).
    """
    card = db.load_card(conn, track_id)
    if card is None:
        return None
    source = card.source
    if not source.local_path or not Path(source.local_path).is_file():
        raise FileNotFoundError(source.local_path or source.uri)
    old_bars = _section_bars(card)
    result = analyze_source(conn, source)
    if result.track_id is not None:
        result.edits_dropped = db.remap_edits(conn, result.track_id, old_bars, _section_bars(result.card))
        if source.kind == "file":
            db.retag_track(conn, result.track_id)
    return result


def _section_bars(card: TrackCard) -> dict[int, tuple[int, int]]:
    return {i: (s.start_bar, s.end_bar) for i, s in enumerate(card.sections) if s.start_bar is not None}
