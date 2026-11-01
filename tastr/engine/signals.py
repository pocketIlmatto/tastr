"""Shared intermediate signals, computed once per track via ctx.get().

Use these from stages so e.g. HPSS runs once no matter how many stages want it:

    y_harm, y_perc = ctx.get("hpss", signals.hpss)

Add new shared signals here rather than inside a stage when more than one
stage could use them.
"""

from __future__ import annotations

import numpy as np

from .context import AnalysisContext


def hpss(ctx: AnalysisContext) -> tuple[np.ndarray, np.ndarray]:
    """(harmonic, percussive) audio: librosa.effects.hpss with its defaults, sample for sample, about 3x faster.

    librosa median-filters the whole spectrogram with one 2-D scipy call, which takes the slow general
    path and was about 70% of a track's analysis time. Filtering row by row (or column by column) hits
    scipy's fast 1-D path (scipy >= 1.15) and gives the same medians; the rest is librosa's own steps.
    """
    import librosa
    from scipy.ndimage import median_filter

    y = ctx.y
    S, phase = librosa.magphase(librosa.stft(y))

    def median(axis):  # 31 frames along time (axis 1) or 31 bins along frequency (axis 0), as librosa
        if S.shape[axis] < 31:  # the two paths reflect differently when the window overhangs both ends
            return median_filter(S, size=(1, 31) if axis else (31, 1), mode="reflect")
        return np.apply_along_axis(median_filter, axis, S, size=31, mode="reflect")

    harm, perc = median(1), median(0)
    parts = (librosa.util.softmask(harm, perc, power=2.0, split_zeros=True),
             librosa.util.softmask(perc, harm, power=2.0, split_zeros=True))
    return tuple(librosa.istft(S * mask * phase, dtype=y.dtype, length=y.shape[-1]) for mask in parts)


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


def log_mel(ctx: AnalysisContext) -> tuple[np.ndarray, np.ndarray]:
    """(128 x frames log-compressed mel magnitude of the full mix, 30 Hz-11 kHz; band centre frequencies).

    Same bands and compression as the Beat This! spectrograms (128 slaney-scale
    bands, log1p(1000 * magnitude)), so the downbeat cues tuned on their labeled
    sets carry over. It is not identical: this runs on core's hop (512, ~43 fps;
    theirs is 441, 50 fps) with n_fft 2048, librosa's area-normalized filters and
    sqrt of mel power, where theirs applies unnormalized filters to the STFT
    magnitude / sqrt(1024). Band edges and centres match; per-band gain, low-band
    frequency resolution, levels and frame times differ.
    """
    import librosa

    mel = librosa.feature.melspectrogram(y=ctx.y, sr=ctx.sr, hop_length=ctx.hop, n_mels=128, fmin=30,
                                         fmax=min(11000, ctx.sr / 2))
    return np.log1p(1000 * np.sqrt(mel)), librosa.mel_frequencies(n_mels=130, fmin=30, fmax=min(11000, ctx.sr / 2))[1:-1]
