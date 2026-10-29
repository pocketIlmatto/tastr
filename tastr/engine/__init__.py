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

import os

__version__ = "0.1.0"

from .audio import AUDIO_EXTENSIONS, DecodeError, load_audio  # noqa: E402,F401
from .card import Source, TrackCard  # noqa: E402,F401
from .pipeline import SR, register_stage, registered_stages, run_pipeline  # noqa: E402,F401


def analyze_file(path: str | os.PathLike, stages: list[str] | None = None, source: Source | None = None) -> TrackCard:
    """Decode `path` and run the pipeline over it. A failing stage is recorded in
    card.errors rather than raised; a file that can't be decoded raises DecodeError."""
    from pathlib import Path

    p = Path(path)
    source = source or Source(kind="file", uri=str(p), local_path=str(p), title=p.stem)
    return run_pipeline(load_audio(p, SR), SR, TrackCard(source=source), stages=stages)
