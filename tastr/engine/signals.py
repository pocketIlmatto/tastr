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


def mfcc(ctx: AnalysisContext, n_mfcc: int = 13) -> np.ndarray:
    """n_mfcc x frames timbre features of the full mix (same hop as the other signals)."""
    import librosa

    mel = librosa.feature.melspectrogram(y=ctx.y, sr=ctx.sr, hop_length=ctx.hop)
    return librosa.feature.mfcc(S=librosa.power_to_db(mel + 1e-10), n_mfcc=n_mfcc)


def mel_percussive(ctx: AnalysisContext) -> tuple[np.ndarray, np.ndarray]:
    """(96 x frames mel power of the percussive part, mel bin centre frequencies)."""
    import librosa

    _, y_perc = ctx.get("hpss", hpss)
    n_mels = 96
    mel = librosa.feature.melspectrogram(y=y_perc, sr=ctx.sr, hop_length=ctx.hop, n_mels=n_mels)
    return mel, librosa.mel_frequencies(n_mels=n_mels, fmax=ctx.sr / 2)


def bass_cqt(ctx: AnalysisContext) -> np.ndarray:
    """36 x frames CQT magnitude of the harmonic part, C1 up three octaves."""
    import librosa

    y_harm, _ = ctx.get("hpss", hpss)
    return np.abs(
        librosa.cqt(y_harm, sr=ctx.sr, hop_length=ctx.hop, fmin=librosa.note_to_hz("C1"), n_bins=36, bins_per_octave=12)
    )


def low_band_flux(ctx: AnalysisContext, lo: float = 20.0, hi: float = 150.0) -> np.ndarray:
    """Onset flux of the percussive part restricted to a low band (kick region), one value per hop."""
    import librosa

    mel, freqs = ctx.get("mel_percussive", mel_percussive)
    mask = (freqs >= lo) & (freqs < hi)
    if not mask.any():
        return np.zeros(mel.shape[1])
    log_s = librosa.power_to_db(mel[mask] + 1e-10)
    flux = np.maximum(0.0, np.diff(log_s, axis=1, prepend=log_s[:, :1]))
    return flux.mean(axis=0)
