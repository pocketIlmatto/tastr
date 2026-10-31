"""The analysis engine: audio in, TrackCard out.

Everything needed to analyze a track lives in this package and imports
nothing from the rest of Tastr (storage, links, web), so it can later be
published as its own library. The rest of the app uses it through:

    from tastr.engine import analyze_file
    card = analyze_file("track.mp3")          # all stages
    card = analyze_file("track.mp3", ["core"])

`tastr.engine.bench` scores the engine against labeled datasets.
"""

from __future__ import annotations

import logging
import os

__version__ = "0.1.0"

from .audio import AUDIO_EXTENSIONS, DecodeError, load_audio  # noqa: E402,F401
from .card import Source, TrackCard  # noqa: E402,F401
from .pipeline import SR, register_stage, registered_stages, run_pipeline  # noqa: E402,F401
from . import rust  # noqa: E402

log = logging.getLogger(__name__)


def analyze_file(path: str | os.PathLike, stages: list[str] | None = None, source: Source | None = None,
                 options: dict | None = None, on_stage=None, signals: bytes | None = None) -> TrackCard:
    """Decode `path` and run the pipeline over it. A failing stage is recorded in
    card.errors rather than raised; a file that can't be decoded raises DecodeError.
    `options` reach the stages (e.g. {"beats_per_bar": 3} sets the meter instead of detecting it).
    `on_stage(name, i, n)` hears "decode" first, then each pipeline stage as it starts (see run_pipeline).
    With TASTR_ENGINE=rust (or options["engine"] = "rust"), the Rust engine makes the whole card when it can
    (rust.py says when it can't); extras["engine"] then says so, and extras["engine_fallback"] why it didn't.
    `signals`: what the browser measured from this file (browser_signals.py). When they check out, the engine
    analyzes the audio they carry and takes HPSS from them; otherwise it decodes the file as usual.
    extras["browser_signals"] says which: true, or why they were set aside."""
    from pathlib import Path

    from . import browser_signals

    p = Path(path)
    source = source or Source(kind="file", uri=str(p), local_path=str(p), title=p.stem)
    if on_stage:
        on_stage("decode", 0, 1)
    card, cache, y = TrackCard(source=source), {}, None
    if rust.chosen(options) == "rust":
        made, why = _rust_card(p, stages, options, on_stage, source, signals)
        if made is not None:
            return made
        log.info("Python engine for %s: %s", p.name, why)
        card.extras["engine_fallback"] = why
    if signals is not None:
        try:
            sig = browser_signals.load(signals, p, SR)
            y, cache["hpss"] = sig.y, sig.hpss()
            card.extras["browser_signals"] = True
        except browser_signals.SignalsError as e:
            log.warning("browser signals for %s set aside: %s", p.name, e)
            card.extras["browser_signals"] = f"set aside: {e}"
    if y is None:
        y = load_audio(p, SR)
    return run_pipeline(y, SR, card, stages=stages, options=options, on_stage=on_stage, cache=cache)


def _rust_card(path, stages, options, on_stage, source, signals):
    """(card, None) from the Rust engine, or (None, why not)."""
    from dataclasses import asdict

    if signals is not None:
        return None, "browser signals came with the file"
    if stages is not None and set(stages) != set(registered_stages()):
        return None, "only some stages were asked for"
    from . import adt

    if adt.wanted(options):
        return None, "the drum model was asked for"
    try:
        if on_stage:
            on_stage("core", 0, len(registered_stages()))
        d = rust.card_from_file(path, (options or {}).get("beats_per_bar"))
        version = rust.version()
    except rust.RustEngineError as e:
        return None, str(e)
    d["source"] = asdict(source)
    card = TrackCard.from_dict(d)
    card.extras["engine"] = f"rust {version}"
    return card, None
