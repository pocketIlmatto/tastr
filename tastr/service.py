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
from .engine import analyze_file, browser_card
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
                   options: dict | None = None, owner: str | None = None, on_stage=None,
                   card: str | Path | None = None) -> AnalyzeResult:
    """`on_stage(name, i, n)`: told as each step starts ("decode", then the engine's stages), for progress.
    `card`: a file holding the card the browser made from this audio (tastr/engine/browser_card.py)."""
    try:
        if not source.local_path:
            raise ValueError(f"source {source.uri} has no local audio file")
        source.sha256 = source.sha256 or file_sha256(source.local_path)
        card = _analyze(source, stages, options, on_stage, card)
        if (options or {}).get("drum_model_required") and not card.extras.get("drum_model"):
            # Asked for the drum model but it didn't run (not installed, or no network for the weights):
            # fail rather than quietly store the built-in lanes.
            raise RuntimeError(card.extras.get("drum_model_error") or "the drum model isn't installed here")
        track_id = db.save_card(conn, card, owner)
        _lookup(conn, track_id, card)
        return AnalyzeResult(source, track_id, card)
    except Exception as e:
        log.warning("failed to analyze %s: %s", source.uri, e)
        return AnalyzeResult(source, None, None, f"{type(e).__name__}: {e}")


def _analyze(source: Source, stages: list[str] | None, options: dict | None, on_stage=None, browser=None):
    """The card the browser made, when it sent one that checks out (engine/browser_card.py); otherwise the
    engine, here or (TASTR_ANALYSIS_BACKEND=modal, a hosted server) on Modal."""
    data = Path(browser).read_bytes() if browser else None
    set_aside = None
    if browser_card.is_card(data):  # the page made the whole card (TASTR_BROWSER_ENGINE)
        try:
            return browser_card.load(data, source)
        except browser_card.BrowserCardError as e:
            log.warning("browser card for %s set aside: %s", source.uri, e)
            set_aside = f"set aside: {e}"
    card = _analyze_audio(source, stages, options, on_stage)
    if set_aside:
        card.extras["browser_card"] = set_aside
    return card


def _analyze_audio(source: Source, stages: list[str] | None, options: dict | None, on_stage):
    """The engine, here or on Modal, where only the audio goes. TASTR_ENGINE travels in the options, so Modal
    uses the engine this server is set to."""
    engine = os.environ.get("TASTR_ENGINE", "").strip().lower()
    if engine and "engine" not in (options or {}):  # so it reaches Modal too, where the setting isn't
        options = {**(options or {}), "engine": engine}
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
                   owner: str | None = None, cards: dict[str, str] | None = None) -> list[AnalyzeResult]:
    """`owner`: whose library the tracks go in on a hosted server (None on your own machine).
    `cards`: {audio file path: the card file the browser made} for uploads that came with one."""
    cards = {str(Path(k).resolve()): v for k, v in (cards or {}).items()}
    return [analyze_source(conn, s, stages, owner=owner, card=cards.get(s.local_path)) for s in resolve_inputs(inputs)]


KEEP = "keep"


def reanalyze_track(conn: sqlite3.Connection, track_id: int, beats_per_bar: int | None | str = KEEP,
                    drum_model: bool | None = None) -> AnalyzeResult | None:
    """Run the full engine again on a stored track's audio file, so it picks up engine changes.

    Keeps the track's id and source (links stay links). File tracks also get their tags
    re-read. Returns None if there is no such track; raises FileNotFoundError if its
    audio file is no longer on disk. A failed analysis leaves the stored card as it was.
    Saved edits follow their section to the new one with the same bars; the rest are dropped
    (counted in `edits_dropped`). A meter change drops them all, since bars change length.

    `beats_per_bar`: 3 or 4 sets the meter, None goes back to detecting it, and the default keeps a
    meter the user set before.

    `drum_model`: True runs the optional pretrained drum model (engine/adt.py) this time; the default keeps
    using it on a track it ran on before (where it is installed). A requested run that couldn't use it fails
    and leaves the stored card as it was.
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
    options = {k: v for k, v in (("beats_per_bar", beats_per_bar),
                                 ("drum_model", drum_model or (drum_model is None and card.extras.get("drum_model"))),
                                 ("drum_model_required", drum_model)) if v}
    result = analyze_source(conn, source, options=options or None, owner=db.track_owner(conn, track_id)[1])
    if result.track_id is not None:
        same_meter = result.card.core.beats_per_bar == (card.core.beats_per_bar or 4)
        result.edits_dropped = db.remap_edits(conn, result.track_id, old_bars, _section_bars(result.card),
                                              keep_all=same_meter)
        if source.kind == "file":
            db.retag_track(conn, result.track_id)
    return result


def _section_bars(card: TrackCard) -> dict[int, tuple[int, int]]:
    return {i: (s.start_bar, s.end_bar) for i, s in enumerate(card.sections) if s.start_bar is not None}
