"""Picture data for the UI's Waveform and Spectrogram views, computed once at analysis time.

Writes `card.extras["views"]`:

    waveform     {"sr", "block", "data"}: per block of `block` samples, min then max
                 as int8 (scaled so the loudest peak is +-127), base64.
    spectrogram  {"hop", "fmin", "fmax", "rows", "frames", "png"}: log-frequency
                 magnitude, one column per `hop` seconds and `rows` bands from fmin
                 (bottom) to fmax (top), as a grayscale PNG data URL. 252 is the
                 loudest cell, 0 is 70 dB or more below it.

Kept in extras rather than `card.series` because series are stored one row per
point, and these are tens of thousands of points per track.
"""

from __future__ import annotations

import base64
import struct
import zlib

import numpy as np

from ..pipeline import AnalysisContext, register_stage

WAVE_BLOCK = 256  # samples, ~12 ms at 22.05 kHz
SPEC_HOP = 0.1  # seconds per column
SPEC_ROWS = 120
SPEC_FMIN, SPEC_FMAX = 40.0, 10000.0
SPEC_RANGE_DB = 70.0
SPEC_NFFT = 4096


def waveform(y: np.ndarray, block: int = WAVE_BLOCK) -> np.ndarray:
    """(blocks, 2) int8 min/max per block, normalized to the track's peak."""
    n = int(np.ceil(y.size / block)) if y.size else 0
    if n == 0:
        return np.zeros((0, 2), dtype=np.int8)
    padded = np.zeros(n * block, dtype=np.float32)
    padded[: y.size] = y
    padded[y.size:] = y[-1]  # pad with the last sample so the tail block's min/max stay honest
    blocks = padded.reshape(n, block)
    mm = np.stack([blocks.min(axis=1), blocks.max(axis=1)], axis=1)
    peak = max(float(np.abs(mm).max()), 1e-6)
    return np.clip(np.round(mm / peak * 127), -127, 127).astype(np.int8)


def spectrogram(y: np.ndarray, sr: int) -> np.ndarray:
    """(SPEC_ROWS, frames) uint8, row 0 = SPEC_FMIN. Each row takes the strongest FFT bin in its band."""
    import librosa

    hop = max(1, int(round(SPEC_HOP * sr)))
    mag = np.abs(librosa.stft(y, n_fft=SPEC_NFFT, hop_length=hop, center=True))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=SPEC_NFFT)
    edges = SPEC_FMIN * (SPEC_FMAX / SPEC_FMIN) ** (np.arange(SPEC_ROWS + 1) / SPEC_ROWS)
    lo = np.searchsorted(freqs, edges[:-1], side="left")
    hi = np.maximum(lo + 1, np.searchsorted(freqs, edges[1:], side="left"))
    rows = np.stack([mag[a:b].max(axis=0) for a, b in zip(lo, hi)])
    db = 20 * np.log10(rows + 1e-6)
    scaled = (db - (db.max() - SPEC_RANGE_DB)) / SPEC_RANGE_DB
    # 64 levels (steps of 4) look the same through the color ramp and compress about a third smaller.
    return (np.clip(np.round(scaled * 63), 0, 63) * 4).astype(np.uint8)


def gray_png(img: np.ndarray) -> bytes:
    """Encode a (height, width) uint8 array as an 8-bit grayscale PNG (row 0 at the top)."""
    h, w = img.shape
    raw = np.hstack([np.zeros((h, 1), dtype=np.uint8), img]).tobytes()  # filter byte 0 per row

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


class ViewsStage:
    name = "views"
    version = "0.1.0"
    requires: tuple[str, ...] = ()

    def run(self, ctx: AnalysisContext) -> None:
        wave = waveform(ctx.y)
        spec = spectrogram(ctx.y, ctx.sr)
        ctx.card.extras["views"] = {
            "waveform": {"sr": ctx.sr, "block": WAVE_BLOCK, "data": base64.b64encode(wave.tobytes()).decode()},
            "spectrogram": {
                "hop": round(SPEC_HOP * ctx.sr) / ctx.sr, "fmin": SPEC_FMIN, "fmax": SPEC_FMAX, "rows": SPEC_ROWS, "frames": int(spec.shape[1]),
                "png": "data:image/png;base64," + base64.b64encode(gray_png(spec[::-1])).decode(),
            },
        }


register_stage(ViewsStage())
