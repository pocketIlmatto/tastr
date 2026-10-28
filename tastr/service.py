"""End-to-end: inputs -> sources -> decoded audio -> pipeline -> stored cards.

This is the entry point CLIs and web handlers should call.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from . import db
from .card import Source, TrackCard
from .ingest import file_sha256, load_audio, resolve_inputs
from .pipeline import SR, run_pipeline

log = logging.getLogger(__name__)


@dataclass
class AnalyzeResult:
    source: Source
    track_id: int | None
    card: TrackCard | None
    error: str | None = None


def analyze_source(conn: sqlite3.Connection, source: Source, stages: list[str] | None = None) -> AnalyzeResult:
    try:
        if not source.local_path:
            raise ValueError(f"source {source.uri} has no local audio file")
        source.sha256 = source.sha256 or file_sha256(source.local_path)
        y = load_audio(source.local_path, SR)
        card = run_pipeline(y, SR, TrackCard(source=source), stages=stages)
        return AnalyzeResult(source, db.save_card(conn, card), card)
    except Exception as e:
        log.warning("failed to analyze %s: %s", source.uri, e)
        return AnalyzeResult(source, None, None, f"{type(e).__name__}: {e}")


def analyze_inputs(conn: sqlite3.Connection, inputs: list[str], stages: list[str] | None = None) -> list[AnalyzeResult]:
    return [analyze_source(conn, s, stages) for s in resolve_inputs(inputs)]
