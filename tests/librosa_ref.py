"""The librosa and scipy recipes the Rust engine's shared signals reproduce (engine-rs/src/spectral.rs,
features.rs), written out with those libraries for tests/test_rust_engine.py to compare against."""

from __future__ import annotations

import numpy as np

SR, HOP = 22050, 512


def hpss(y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(harmonic, percussive): librosa.effects.hpss with its defaults. The medians are taken row by row (or
    column by column), which gives librosa's 2-D median filter's values on scipy's fast 1-D path."""
    import librosa
    from scipy.ndimage import median_filter

    S, phase = librosa.magphase(librosa.stft(y))

    def median(axis):  # 31 frames along time (axis 1) or 31 bins along frequency (axis 0), as librosa
        if S.shape[axis] < 31:  # the two paths reflect differently when the window overhangs both ends
            return median_filter(S, size=(1, 31) if axis else (31, 1), mode="reflect")
        return np.apply_along_axis(median_filter, axis, S, size=31, mode="reflect")

    harm, perc = median(1), median(0)
    parts = (librosa.util.softmask(harm, perc, power=2.0, split_zeros=True),
             librosa.util.softmask(perc, harm, power=2.0, split_zeros=True))
    return tuple(librosa.istft(S * mask * phase, dtype=y.dtype, length=y.shape[-1]) for mask in parts)


def onset_envelope(y_perc: np.ndarray) -> np.ndarray:
    import librosa

    return librosa.onset.onset_strength(y=y_perc, sr=SR, hop_length=HOP)


def _envelopes(y_perc: np.ndarray) -> dict[str, np.ndarray]:
    """4 ms amplitude envelopes in dB of the percussive part's mid (800-3000 Hz) and air (above 6 kHz) bands."""
    from scipy import signal

    nyq, out = SR / 2, {}
    for name, (lo, hi) in {"mid": (800, 3000), "air": (6000, None)}.items():
        if hi is None:
            sos = signal.butter(4, min(lo, nyq * 0.8), btype="highpass", fs=SR, output="sos")
        else:
            sos = signal.butter(4, [lo, min(hi, nyq * 0.9)], btype="bandpass", fs=SR, output="sos")
        x = np.abs(signal.sosfiltfilt(sos, y_perc))
        win = max(1, int(0.004 * SR))
        out[name] = 20 * np.log10(np.convolve(x, np.ones(win) / win, mode="same") + 1e-7)
    return out


def signals(y: np.ndarray) -> dict[str, np.ndarray]:
    """Every shared signal the native binary writes, by its output name."""
    import librosa

    _, perc = hpss(y)
    mel_perc = librosa.feature.melspectrogram(y=perc, sr=SR, hop_length=HOP, n_mels=96)
    freqs = librosa.mel_frequencies(n_mels=96, fmax=SR / 2)
    low = librosa.power_to_db(mel_perc[(freqs >= 20) & (freqs < 150)] + 1e-10)
    hi = mel_perc[freqs >= 6000]
    hi_db = librosa.power_to_db(hi + 1e-10, ref=float(np.percentile(hi, 99.9)), top_db=50.0)
    mel = librosa.feature.melspectrogram(y=y, sr=SR, hop_length=HOP)
    log_mel = librosa.feature.melspectrogram(y=y, sr=SR, hop_length=HOP, n_mels=128, fmin=30, fmax=11000)
    env = _envelopes(perc)
    return {"onset_envelope": onset_envelope(perc), "rms": librosa.feature.rms(y=y, hop_length=HOP)[0],
            "mfcc": librosa.feature.mfcc(S=librosa.power_to_db(mel + 1e-10), n_mfcc=13),
            "centroid": librosa.feature.spectral_centroid(S=np.abs(librosa.stft(y)), sr=SR)[0],
            "mel_percussive": mel_perc,
            "low_band_flux": np.maximum(0.0, np.diff(low, axis=1, prepend=low[:, :1])).mean(axis=0),
            "air_flux": np.maximum(0.0, np.diff(hi_db, axis=1, prepend=hi_db[:, :1])).mean(axis=0),
            "log_mel": np.log1p(1000 * np.sqrt(log_mel)), "envelope_mid": env["mid"], "envelope_air": env["air"]}
