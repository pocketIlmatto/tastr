"""End-to-end: inputs -> sources -> decoded audio -> pipeline -> stored cards.

This is the entry point CLIs and web handlers should call.
"""

from __future__ import annotations

import logging
import os
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


def analyze_source(conn: sqlite3.Connection, source: Source, stages: list[str] | None = None,
                   options: dict | None = None, owner: str | None = None, on_stage=None) -> AnalyzeResult:
    """`on_stage(name, i, n)`: told as each step starts ("decode", then the engine's stages), for progress."""
    try:
        if not source.local_path:
            raise ValueError(f"source {source.uri} has no local audio file")
        source.sha256 = source.sha256 or file_sha256(source.local_path)
        card = _analyze(source, stages, options, on_stage)
        track_id = db.save_card(conn, card, owner)
        _lookup(conn, track_id, card)
        return AnalyzeResult(source, track_id, card)
    except Exception as e:
        log.warning("failed to analyze %s: %s", source.uri, e)
        return AnalyzeResult(source, None, None, f"{type(e).__name__}: {e}")


def _analyze(source: Source, stages: list[str] | None, options: dict | None, on_stage=None):
    """The engine, here or (TASTR_ANALYSIS_BACKEND=modal, a hosted server) on Modal, where only the audio goes."""
    if os.environ.get("TASTR_ANALYSIS_BACKEND", "local") == "modal":
        from .cloud import remote

        card = remote.analyze(source.local_path, stages, options, on_stage)
        card.source = source
        return card
    return analyze_file(source.local_path, stages, source=source, options=options, on_stage=on_stage)


def _lookup(conn: sqlite3.Connection, track_id: int, card: TrackCard) -> None:
    """Opt-in catalog lookups (TASTR_LOOKUP) right after analysis; never fails the analysis."""
    from . import lookup

    providers = lookup.enabled()
    if providers:
        try:
            lookup.lookup_track(conn, track_id, card, providers, only_missing=True)
        except Exception as e:
            log.warning("metadata lookup failed for track %s: %s", track_id, e)


def analyze_inputs(conn: sqlite3.Connection, inputs: list[str], stages: list[str] | None = None,
                   owner: str | None = None) -> list[AnalyzeResult]:
    """`owner`: whose library the tracks go in on a hosted server (None on your own machine)."""
    return [analyze_source(conn, s, stages, owner=owner) for s in resolve_inputs(inputs)]


KEEP = "keep"


def reanalyze_track(conn: sqlite3.Connection, track_id: int, beats_per_bar: int | None | str = KEEP) -> AnalyzeResult | None:
    """Run the full engine again on a stored track's audio file, so it picks up engine changes.

    Keeps the track's id and source (links stay links). File tracks also get their tags
    re-read. Returns None if there is no such track; raises FileNotFoundError if its
    audio file is no longer on disk. A failed analysis leaves the stored card as it was.
    Saved edits follow their section to the new one with the same bars; the rest are dropped
    (counted in `edits_dropped`). A meter change drops them all, since bars change length.

    `beats_per_bar`: 3 or 4 sets the meter, None goes back to detecting it, and the default keeps a
    meter the user set before.
    """
    card = db.load_card(conn, track_id)
    if card is None:
        return None
    source = card.source
    if not source.local_path or not Path(source.local_path).is_file():
        raise FileNotFoundError(source.local_path or source.uri)
    old_bars = _section_bars(card)
    if beats_per_bar == KEEP:
        beats_per_bar = card.core.beats_per_bar if card.core.meter_set else None
    result = analyze_source(conn, source, options={"beats_per_bar": beats_per_bar} if beats_per_bar else None,
                            owner=db.track_owner(conn, track_id)[1])
    if result.track_id is not None:
        same_meter = result.card.core.beats_per_bar == (card.core.beats_per_bar or 4)
        result.edits_dropped = db.remap_edits(conn, result.track_id, old_bars, _section_bars(result.card),
                                              keep_all=same_meter)
        if source.kind == "file":
            db.retag_track(conn, result.track_id)
    return result


def _section_bars(card: TrackCard) -> dict[int, tuple[int, int]]:
    return {i: (s.start_bar, s.end_bar) for i, s in enumerate(card.sections) if s.start_bar is not None}
