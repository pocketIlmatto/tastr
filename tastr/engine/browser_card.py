"""Whole cards made in the browser (TASTR_BROWSER_ENGINE): the page runs the Rust engine's WebAssembly build
on an upload (tastr/web/static/engine.js) and sends the finished card with the audio, so the server needs no
analysis at all. Because the browser and the server run the same Rust code, the card is the one the server's
Rust engine (TASTR_ENGINE=rust) would make from the same file.

The server takes a card only when it checks out, and otherwise analyzes the file itself as usual:

- the format and version are this module's;
- it was made by the WebAssembly build the server serves now (`rust.wasm_id()`), so a page left open across a
  deploy doesn't store a card from an older engine;
- it ran every stage at the versions the server's engine has, at the engine's sample rate;
- it lasts as long as the uploaded file.

A person can only ever put a card in their own library this way, the same as editing their tracks.

Format (version 1): UTF-8 JSON {"format": "tastr-card", "version": 1, "engine": "<wasm id>",
"engine_version": "<crate version>", "card": {...}}, the card in TrackCard.to_dict() shape without its source.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from pathlib import Path

from . import rust
from .card import Source, TrackCard
from .pipeline import SR, registered_stages

FORMAT, VERSION = "tastr-card", 1
MAX_BYTES = 8 << 20  # a 20-minute card is about 1.5 MB, mostly the waveform and spectrogram pictures
MAX_SECONDS = 20 * 60  # the page makes no card for longer tracks (app.js ENGINE_MAX_MINUTES)
# The card must last as long as the file says, give or take decoder padding.
DURATION_SLACK = (1.0, 0.01)  # seconds, share of the duration: the larger applies


class BrowserCardError(ValueError):
    pass


def enabled() -> bool:
    return os.environ.get("TASTR_BROWSER_ENGINE", "").strip().lower() in ("1", "true", "yes", "on")


def is_card(data: bytes | None) -> bool:
    return bool(data) and data[:1] == b"{" and b'"tastr-card"' in data[:200]


def load(data: bytes, source: Source) -> TrackCard:
    """The card the page sent for the file `source.local_path`, checked; raises BrowserCardError."""
    if not enabled():
        raise BrowserCardError("browser cards are turned off on this server")
    if len(data) > MAX_BYTES:
        raise BrowserCardError("card too big")
    try:
        msg = json.loads(data.decode())
    except (UnicodeDecodeError, ValueError) as e:
        raise BrowserCardError(f"not JSON: {e}") from e
    if not isinstance(msg, dict) or msg.get("format") != FORMAT or msg.get("version") != VERSION:
        raise BrowserCardError("unknown card format or version")
    expected = rust.wasm_id()
    if expected is None or msg.get("engine") != expected:
        raise BrowserCardError("made by a different engine build than this server's")
    d = msg.get("card")
    if not isinstance(d, dict):
        raise BrowserCardError("no card")
    want = {name: stage.version for name, stage in registered_stages().items()}
    if d.get("stages_run") != want:
        raise BrowserCardError(f"stages {d.get('stages_run')} don't match the engine's {want}")
    if d.get("sample_rate") != SR:
        raise BrowserCardError(f"card at {d.get('sample_rate')} Hz, the engine works at {SR}")
    got = d.get("duration")
    if not isinstance(got, (int, float)) or not 0 < got <= MAX_SECONDS:
        raise BrowserCardError("bad duration")
    expected_s = file_duration(source.local_path) if source.local_path else None
    if expected_s is not None and abs(got - expected_s) > max(DURATION_SLACK[0], DURATION_SLACK[1] * expected_s):
        raise BrowserCardError(f"card lasts {got:.1f} s but the file {expected_s:.1f} s")
    try:
        card = TrackCard.from_dict({**d, "source": asdict(source)})
    except (TypeError, ValueError, KeyError, AttributeError) as e:
        raise BrowserCardError(f"not a card: {e}") from e
    v = msg.get("engine_version")
    card.extras["engine"] = f"rust {v}" if isinstance(v, str) and re.fullmatch(r"[\w.\-]{1,20}", v) else "rust"
    card.extras["browser_card"] = True
    return card


def file_duration(path: str | Path) -> float | None:
    """The file's duration from its headers, without decoding it; None if it can't be read that way."""
    try:
        import soundfile

        return float(soundfile.info(str(path)).duration)
    except Exception:
        pass
    try:
        import mutagen

        f = mutagen.File(str(path))
        return float(f.info.length) if f is not None and f.info.length else None
    except Exception:
        return None
