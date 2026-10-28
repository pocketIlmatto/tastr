"""Shared intermediate signals, computed once per track via ctx.get().

Use these from stages so e.g. HPSS runs once no matter how many stages want it:

    y_harm, y_perc = ctx.get("hpss", signals.hpss)

Add new shared signals here rather than inside a stage when more than one
stage could use them.
"""

from __future__ import annotations

import numpy as np

from .pipeline import AnalysisContext


def hpss(ctx: AnalysisContext) -> tuple[np.ndarray, np.ndarray]:
    import librosa

    return librosa.effects.hpss(ctx.y)


def onset_envelope(ctx: AnalysisContext) -> np.ndarray:
    """Onset strength of the percussive part, one value per hop."""
    import librosa

    _, y_perc = ctx.get("hpss", hpss)
    return librosa.onset.onset_strength(y=y_perc, sr=ctx.sr, hop_length=ctx.hop)


def rms(ctx: AnalysisContext) -> np.ndarray:
    import librosa

    return librosa.feature.rms(y=ctx.y, hop_length=ctx.hop)[0]


def chroma(ctx: AnalysisContext) -> np.ndarray:
    """12 x frames chroma of the harmonic part."""
    import librosa

    y_harm, _ = ctx.get("hpss", hpss)
    return librosa.feature.chroma_cqt(y=y_harm, sr=ctx.sr, hop_length=ctx.hop)


def frame_times(ctx: AnalysisContext, n_frames: int) -> np.ndarray:
    return np.arange(n_frames) * ctx.hop / ctx.sr
