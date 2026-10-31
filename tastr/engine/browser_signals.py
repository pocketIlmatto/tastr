"""Signals the browser measured before uploading (TASTR_BROWSER_SIGNALS), so the server can skip that work.

With the flag on, the page decodes the file itself, runs HPSS in a worker (tastr/web/static/signals.js) and
uploads a "signals" file next to the audio: the decoded mono audio at the engine's rate and HPSS's harmonic
part. The engine then analyzes that audio instead of decoding the file, and takes HPSS from it: the
harmonic part as sent, the percussive part as audio minus harmonic. HPSS's two soft masks add up to one and
its STFT round trip reconstructs the input, so that difference is HPSS's percussive output to about 1e-7.

Format (version 1), little-endian:

    b"TSG1" | uint32 header length | header (UTF-8 JSON, space-padded to an even length) | int16 samples

The header is {"format": "tastr-signals", "version": 1, "sr": 22050, "n": samples, "arrays": ["y",
"harmonic"], "scales": [..]}; each array is n int16 samples, multiplied by its scale to get float audio.
"""

from __future__ import annotations

import json
import logging
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

MAGIC = b"TSG1"
ARRAYS = ("y", "harmonic")
MAX_SECONDS = 20 * 60  # the page sends nothing for longer tracks (signals.js MAX_SECONDS)
# The audio the page decoded must last as long as the file says, give or take decoder padding.
DURATION_SLACK = (1.0, 0.01)  # seconds, share of the duration: the larger applies


class SignalsError(ValueError):
    pass


@dataclass
class BrowserSignals:
    y: np.ndarray  # mono float32 at sr
    harmonic: np.ndarray
    sr: int

    def hpss(self) -> tuple[np.ndarray, np.ndarray]:
        return self.harmonic, self.y - self.harmonic


def parse(data: bytes, sr: int) -> BrowserSignals:
    """Read a signals file, checking it is whole and made at the engine's rate `sr`."""
    if len(data) < 8 or data[:4] != MAGIC:
        raise SignalsError("not a signals file")
    (size,) = struct.unpack_from("<I", data, 4)
    try:
        head = json.loads(data[8:8 + size].decode())
    except (UnicodeDecodeError, ValueError) as e:
        raise SignalsError(f"bad header: {e}") from e
    if not isinstance(head, dict) or head.get("format") != "tastr-signals" or head.get("version") != 1:
        raise SignalsError("unknown signals format or version")
    if head.get("sr") != sr:
        raise SignalsError(f"signals at {head.get('sr')} Hz, the engine needs {sr}")
    if tuple(head.get("arrays", ())) != ARRAYS:
        raise SignalsError(f"expected arrays {ARRAYS}")
    n, scales = head.get("n"), head.get("scales")
    if not isinstance(n, int) or not 0 < n <= MAX_SECONDS * sr:
        raise SignalsError("bad sample count")
    if (not isinstance(scales, list) or len(scales) != len(ARRAYS)
            or not all(isinstance(s, (int, float)) and np.isfinite(s) and s > 0 for s in scales)):
        raise SignalsError("bad scales")
    start = 8 + size
    if len(data) != start + 2 * n * len(ARRAYS):
        raise SignalsError("signals file is cut short or too long")
    raw = np.frombuffer(data, dtype="<i2", offset=start).reshape(len(ARRAYS), n)
    y, harmonic = (raw[i].astype(np.float32) * np.float32(scales[i]) for i in range(len(ARRAYS)))
    return BrowserSignals(y=y, harmonic=harmonic, sr=sr)


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


def load(data: bytes, path: str | Path, sr: int) -> BrowserSignals:
    """parse(), then check the signals belong to the audio file at `path` (same duration, near enough)."""
    sig = parse(data, sr)
    expected = file_duration(path)
    got = sig.y.size / sr
    if expected is not None and abs(got - expected) > max(DURATION_SLACK[0], DURATION_SLACK[1] * expected):
        raise SignalsError(f"signals last {got:.1f} s but the file {expected:.1f} s")
    return sig
