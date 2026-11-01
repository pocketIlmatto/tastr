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
                 options: dict | None = None, on_stage=None) -> TrackCard:
    """Analyze one audio file. A file that can't be decoded raises DecodeError.
    `options` reach the engine (e.g. {"beats_per_bar": 3} sets the meter instead of detecting it).
    `on_stage(name, i, n)` hears "decode" first, then each stage as it starts, for progress bars.

    The Rust engine (rust.py) makes the card: it decodes the file itself, or, for formats it can't read (Opus),
    takes the audio decoded here. With the optional drum model (options["drum_model"], adt.py), the model runs
    here and Rust puts its hits into the drum lanes. extras["engine"] names the engine. The Python engine
    (run_pipeline over the stages in stages/) stays as the reference the benchmarks compare against, and makes the
    card only when asked for (TASTR_ENGINE=python or options["engine"]), for a subset of stages, or when the Rust
    library isn't built; extras["engine_fallback"] then says why."""
    from pathlib import Path

    p = Path(path)
    source = source or Source(kind="file", uri=str(p), local_path=str(p), title=p.stem)
    if on_stage:
        on_stage("decode", 0, 1)
    card = TrackCard(source=source)
    if rust.chosen(options) == "rust":
        last = ["decode"]

        def heard(name, *rest):
            last[0] = name
            on_stage(name, *rest)

        made, why = _rust_card(p, stages, options, heard if on_stage else None, source)
        if made is not None:
            return made
        log.info("Python engine for %s: %s", p.name, why)
        card.extras["engine_fallback"] = why
        if on_stage and last[0] != "decode":  # back to decoding, so a file nothing reads fails at "decode", not "core"
            on_stage("decode", 0, 1)
    y = load_audio(p, SR)
    return run_pipeline(y, SR, card, stages=stages, options=options, on_stage=on_stage)


def _rust_card(path, stages, options, on_stage, source):
    """(card, None) from the Rust engine, or (None, why not)."""
    from dataclasses import asdict

    if stages is not None and set(stages) != set(registered_stages()):
        return None, "only some stages were asked for"
    meter = (options or {}).get("beats_per_bar")
    decoded_here = False
    try:
        if on_stage:
            on_stage("core", 0, len(registered_stages()))
        hits, model_error = _drum_model_hits(path, options)
        try:
            d = rust.card_from_file(path, meter, hits)
        except rust.RustDecodeError:
            # A format the Rust decoder doesn't read (Opus): decode here (librosa, else ffmpeg) and hand Rust the
            # audio. Raises DecodeError when nothing can read the file, which progress then shows at "decode".
            if on_stage:
                on_stage("decode", 0, 1)
            y = load_audio(path, SR)
            if on_stage:
                on_stage("core", 0, len(registered_stages()))
            d, decoded_here = rust.card_from_samples(y, meter, hits), True
        version = rust.version()
    except rust.RustEngineError as e:
        return None, str(e)
    d["source"] = asdict(source)
    card = TrackCard.from_dict(d)
    card.extras["engine"] = f"rust {version}"
    if decoded_here:
        card.extras["decoded_by"] = "python"
    if model_error:
        card.extras["drum_model_error"] = model_error
    return card, None


def _drum_model_hits(path, options) -> tuple[dict | None, str | None]:
    """(hits, None) from the optional drum model (adt.py) on the original file at its own rate, when this
    analysis asks for it (adt.wanted); (None, None) when it doesn't, (None, why) when it failed. The Rust engine
    puts the hits into the kick, tom and hi-hat lanes and adds a cymbal lane."""
    from . import adt

    if not adt.wanted(options):
        return None, None
    try:
        import librosa

        y, _ = librosa.load(str(path), sr=adt.SR, mono=True)
        return adt.onsets(y, adt.SR), None
    except Exception as e:  # e.g. no network for the first download: the built-in lanes stay
        log.warning("drum model unavailable, using the built-in lanes: %s", e)
        return None, f"{type(e).__name__}: {e}"
