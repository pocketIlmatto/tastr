"""Draw the figures in docs/audio-terms.md from a synthetic track (no real music, no downloads).

    pip install matplotlib   # librosa comes with tastr
    python scripts/terms_figures.py

Writes PNGs to docs/images/terms/. The track is tastr.engine.bench.synth.make_track: a minor-key triad
progression, a bass line, and (in the middle section) a kick on every beat and off-beat hats. Each figure
uses the engine's own settings (22,050 Hz, hop 512, n_fft 2048) unless its caption says otherwise.
"""

from __future__ import annotations

from pathlib import Path

import librosa
import librosa.display
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from scipy import signal  # noqa: E402

from tastr.engine.bench.synth import make_track  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "images" / "terms"
SR, HOP, N_FFT = 22050, 512, 2048
BPM = 120.0
plt.rcParams.update({"figure.dpi": 110, "font.size": 9, "axes.titlesize": 10, "axes.titleweight": "bold",
                     "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white"})
INK, ACCENT, ACCENT2, MUTED = "#1f2937", "#d9480f", "#1971c2", "#868e96"


def save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / name, bbox_inches="tight")
    plt.close(fig)
    print("wrote", OUT / name)


def track():
    """16 bars: 4 quiet bars (no drums), 8 full bars, 4 quiet bars."""
    y, truth = make_track(bpm=BPM, bars_per_section=(4, 8, 4), sr=SR)
    return y.astype(np.float32), truth


def excerpt(y, bar0, bars):
    bar = 4 * 60.0 / BPM
    return y[int(bar0 * bar * SR): int((bar0 + bars) * bar * SR)]


def fig_frames(y):
    """Samples, frames, hops, windows and RMS on the first beat of the drum section."""
    x = excerpt(y, 4, 1)[: int(0.6 * SR)]
    t = np.arange(x.size) / SR
    fig, (a, b) = plt.subplots(2, 1, figsize=(8, 4.2), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
    a.plot(t, x, color=INK, lw=0.5)
    for k in range(5):
        s = k * HOP * 2  # every 2nd hop, so the windows stay readable
        w = np.hanning(N_FFT) * np.abs(x).max()
        a.plot((s + np.arange(N_FFT)) / SR, w, color=[ACCENT, ACCENT2][k % 2], lw=1.2)
        a.annotate(f"frame {2 * k}", ((s + N_FFT / 2) / SR, w.max() * 1.02), ha="center", fontsize=8,
                   color=[ACCENT, ACCENT2][k % 2])
    a.set_ylim(-1.05 * np.abs(x).max(), 1.25 * np.abs(x).max())
    a.set_title("Audio samples, cut into overlapping Hann-windowed frames (n_fft 2048 = 93 ms; hop 512 = 23 ms)")
    a.set_ylabel("amplitude")
    rms = librosa.feature.rms(y=x, frame_length=N_FFT, hop_length=HOP)[0]
    tr = librosa.frames_to_time(np.arange(rms.size), sr=SR, hop_length=HOP)
    b.step(tr, 20 * np.log10(rms + 1e-9), where="mid", color=ACCENT2)
    b.plot(tr, 20 * np.log10(rms + 1e-9), "o", ms=2.5, color=ACCENT2)
    b.set_title("RMS per frame, in dB: one number per hop (about 43 per second)")
    b.set_ylabel("dB")
    b.set_xlabel("time (s)")
    b.set_xlim(0, t[-1])
    save(fig, "frames-rms.png")


def fig_spectrograms(y):
    """The same two bars as an STFT spectrogram, a mel spectrogram and a CQT."""
    x = excerpt(y, 4, 2)
    fig, ax = plt.subplots(1, 3, figsize=(11, 3.6), sharex=True)
    S = librosa.amplitude_to_db(np.abs(librosa.stft(x, n_fft=N_FFT, hop_length=HOP)), ref=np.max)
    librosa.display.specshow(S, sr=SR, hop_length=HOP, x_axis="time", y_axis="linear", ax=ax[0], cmap="magma")
    ax[0].set_title("STFT spectrogram (linear Hz)")
    M = librosa.power_to_db(librosa.feature.melspectrogram(y=x, sr=SR, hop_length=HOP, n_mels=128), ref=np.max)
    librosa.display.specshow(M, sr=SR, hop_length=HOP, x_axis="time", y_axis="mel", ax=ax[1], cmap="magma")
    ax[1].set_title("Mel spectrogram (128 bands)")
    C = librosa.amplitude_to_db(np.abs(librosa.cqt(x, sr=SR, hop_length=HOP, fmin=librosa.note_to_hz("C1"),
                                                   n_bins=84)), ref=np.max)
    librosa.display.specshow(C, sr=SR, hop_length=HOP, x_axis="time", y_axis="cqt_note", ax=ax[2], cmap="magma",
                             fmin=librosa.note_to_hz("C1"))
    ax[2].set_title("CQT (12 bins per octave, C1-B7)")
    for a in ax:
        a.set_xlabel("time (s)")
    fig.suptitle("Two bars of the synthetic track, three ways (brighter = louder)", y=1.02)
    fig.tight_layout()
    save(fig, "stft-mel-cqt.png")


def fig_mel_filters():
    """A few mel filters on a linear Hz axis: narrow at the bottom, wide at the top."""
    fb = librosa.filters.mel(sr=SR, n_fft=N_FFT, n_mels=24, fmax=SR / 2)
    f = librosa.fft_frequencies(sr=SR, n_fft=N_FFT)
    fig, (a, b) = plt.subplots(1, 2, figsize=(10, 2.8), gridspec_kw={"width_ratios": [3, 2]})
    for i, row in enumerate(fb):
        a.plot(f, row / row.max(), color=[ACCENT, ACCENT2][i % 2], lw=1)
    a.set_title("24 mel filters over the STFT's bins (each band sums the bins under its triangle)")
    a.set_xlabel("frequency (Hz)")
    a.set_yticks([])
    hz = np.linspace(0, 11025, 400)
    b.plot(hz, librosa.hz_to_mel(hz), color=INK)
    b.set_title("Hz to mel: about linear below 1 kHz, log above")
    b.set_xlabel("Hz")
    b.set_ylabel("mel")
    fig.tight_layout()
    save(fig, "mel-filters.png")


def fig_hpss(y):
    """HPSS: sustained notes are horizontal lines, hits are vertical lines."""
    x = excerpt(y, 4, 2)
    D = librosa.stft(x, n_fft=N_FFT, hop_length=HOP)
    H, P = librosa.decompose.hpss(D)
    ref = np.abs(D).max()
    fig, ax = plt.subplots(1, 3, figsize=(11, 3.4), sharey=True)
    for a, part, title in zip(ax, (D, H, P), ("Full mix", "Harmonic part (horizontal)", "Percussive part (vertical)")):
        librosa.display.specshow(librosa.amplitude_to_db(np.abs(part), ref=ref), sr=SR, hop_length=HOP,
                                 x_axis="time", y_axis="log", ax=a, cmap="magma", vmin=-80, vmax=0)
        a.set_title(title)
        a.set_xlabel("time (s)")
    fig.suptitle("HPSS: a median filter along time keeps steady notes, one along frequency keeps hits", y=1.03)
    fig.tight_layout()
    save(fig, "hpss.png")


def fig_chroma(y):
    """Chroma of the harmonic part, with the synth's chord per bar."""
    x = excerpt(y, 4, 4)
    h, _ = librosa.effects.hpss(x)
    c = librosa.feature.chroma_cqt(y=h, sr=SR, hop_length=HOP)
    fig, a = plt.subplots(figsize=(8, 3))
    librosa.display.specshow(c, sr=SR, hop_length=HOP, x_axis="time", y_axis="chroma", ax=a, cmap="Greys")
    bar = 4 * 60.0 / BPM
    for i, name in enumerate(["Am", "F", "C", "G"]):  # make_track's minor progression from A: i - VI - III - VII
        a.axvline(i * bar, color=ACCENT, lw=0.8)
        a.text(i * bar + 0.05, 11.3, name, color=ACCENT, fontsize=10, fontweight="bold", va="center")
    a.set_title("Chroma (harmonic part): 12 pitch classes per frame; octaves folded together")
    a.set_xlabel("time (s)")
    save(fig, "chroma.png")


def fig_onsets_tempogram(y):
    """Onset envelope, peaks, beats, and the tempogram of the whole drum section."""
    x = excerpt(y, 4, 8)
    _, p = librosa.effects.hpss(x)
    env = librosa.onset.onset_strength(y=p, sr=SR, hop_length=HOP)
    t = librosa.frames_to_time(np.arange(env.size), sr=SR, hop_length=HOP)
    onsets = librosa.onset.onset_detect(onset_envelope=env, sr=SR, hop_length=HOP)
    _, beats = librosa.beat.beat_track(onset_envelope=env, sr=SR, hop_length=HOP)
    tg = librosa.feature.tempogram(onset_envelope=env, sr=SR, hop_length=HOP, win_length=384)
    bpms = librosa.tempo_frequencies(tg.shape[0], sr=SR, hop_length=HOP)
    fig, (a, b) = plt.subplots(1, 2, figsize=(11, 3.2), gridspec_kw={"width_ratios": [3, 2]})
    show = t < 4.0
    a.plot(t[show], env[show], color=INK, lw=1)
    o = onsets[t[onsets] < 4.0]
    a.plot(t[o], env[o], "o", color=ACCENT, ms=4, label="onsets (peaks)")
    for k, bt in enumerate(beats[t[beats] < 4.0]):
        a.axvline(t[bt], color=ACCENT2, lw=0.8, ls="--", label="beats" if k == 0 else None)
    a.set_title("Onset envelope of the percussive part (first 2 bars)")
    a.set_xlabel("time (s)")
    a.legend(loc="upper right", fontsize=8)
    mean = tg.mean(axis=1)
    keep = (bpms > 30) & (bpms < 400)
    b.semilogx(bpms[keep], mean[keep], color=INK)
    b.axvline(BPM, color=ACCENT, lw=0.8, ls="--")
    b.text(BPM * 1.04, mean[keep].max() * 1.02, "the true tempo", fontsize=8, color=ACCENT, va="top")
    b.set_xticks([30, 40, 60, 80, 120, 240])
    b.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
    b.get_xaxis().set_minor_formatter(matplotlib.ticker.NullFormatter())
    b.set_title("Tempogram, averaged over time")
    b.set_xlabel("tempo (BPM, log scale)")
    b.set_yticks([])
    fig.tight_layout()
    save(fig, "onsets-tempogram.png")


def fig_sections(y, truth):
    """Per-bar features (chroma + MFCC + loudness), z-scored, and Ward clustering into 3 sections."""
    bar = 4 * 60.0 / BPM
    n_bars = 16
    chroma = librosa.feature.chroma_cqt(y=librosa.effects.hpss(y)[0], sr=SR, hop_length=HOP)
    mel = librosa.feature.melspectrogram(y=y, sr=SR, hop_length=HOP)
    mfcc = librosa.feature.mfcc(S=librosa.power_to_db(mel + 1e-10), n_mfcc=13)
    db = 20 * np.log10(librosa.feature.rms(y=y, hop_length=HOP)[0] + 1e-9)
    n = min(chroma.shape[1], mfcc.shape[1], db.size)
    feats = np.vstack([chroma[:, :n], mfcc[1:, :n], db[None, :n]])
    edges = librosa.time_to_frames(np.arange(n_bars + 1) * bar, sr=SR, hop_length=HOP)
    per_bar = np.stack([feats[:, a:b].mean(axis=1) for a, b in zip(edges[:-1], edges[1:])], axis=1)
    z = (per_bar - per_bar.mean(axis=1, keepdims=True)) / (per_bar.std(axis=1, keepdims=True) + 1e-9)
    bounds = librosa.segment.agglomerative(z, 3)
    fig, a = plt.subplots(figsize=(8, 3.6))
    a.imshow(z, aspect="auto", cmap="RdBu_r", vmin=-2.5, vmax=2.5, interpolation="nearest",
             extent=(0.5, n_bars + 0.5, z.shape[0] - 0.5, -0.5))
    a.set_yticks([5.5, 17.5, 24])
    a.set_yticklabels(["chroma (12)", "MFCC 2-13", "loudness"])
    for b in bounds[1:]:
        a.axvline(b + 0.5, color=INK, lw=2.5)
    a.set_xticks(range(1, n_bars + 1))
    a.set_xlabel("bar")
    a.set_title("One column per bar, each row z-scored (red = above the track's average). Black lines: Ward clustering, k = 3")
    save(fig, "sections-ward.png")


def fig_drum_envelopes():
    """A snare and a clap through the 800 Hz-3 kHz band-pass, as 4 ms dB envelopes."""
    rng = np.random.default_rng(1)
    n = int(0.25 * SR)
    t = np.arange(n) / SR
    snare = rng.standard_normal(n) * np.exp(-t / 0.05) + 0.8 * np.sin(2 * np.pi * 185 * t) * np.exp(-t / 0.04)
    clap = np.zeros(n)
    for k, g in enumerate((0.8, 0.9, 1.0)):  # three quick bursts, then the tail
        i = int(k * 0.011 * SR)
        clap[i:] += g * rng.standard_normal(n - i) * np.exp(-(t[: n - i]) / (0.004 if k < 2 else 0.06))
    sos = signal.butter(4, [800, 3000], btype="bandpass", fs=SR, output="sos")
    smooth = int(0.004 * SR)
    fig, ax = plt.subplots(1, 2, figsize=(10, 2.8), sharey=True)
    for a, x, name in zip(ax, (snare, clap), ("Snare: one decay", "Clap: three bursts, then a decay")):
        env = np.convolve(np.abs(signal.sosfiltfilt(sos, x)), np.ones(smooth) / smooth, mode="same")
        db = 20 * np.log10(env / env.max() + 1e-6)
        a.plot(t * 1000, db, color=ACCENT if "Clap" in name else ACCENT2)
        a.set_title(name)
        a.set_xlabel("time (ms)")
        a.set_ylim(-45, 3)
        a.set_xlim(0, 120)
    ax[0].set_ylabel("dB (800 Hz-3 kHz envelope)")
    save(fig, "drum-envelopes.png")


def main():
    y, truth = track()
    fig_frames(y)
    fig_spectrograms(y)
    fig_mel_filters()
    fig_hpss(y)
    fig_chroma(y)
    fig_onsets_tempogram(y)
    fig_sections(y, truth)
    fig_drum_envelopes()


if __name__ == "__main__":
    main()
