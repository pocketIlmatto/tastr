"""Synthetic test tracks with known ground truth (BPM, key, structure).

Used by the tests (tests/synth.py re-exports this module) and by the `synth`
benchmark dataset. Feature tests should use these so expectations are exact:

    y, truth = make_track(bpm=124, tonic=9, mode="minor")
    truth["bpm"], truth["key"], truth["sections"]  # what an estimator should find
"""

from __future__ import annotations

import numpy as np

SR = 22050


def _tone(freq, dur, sr=SR, amp=0.2):
    t = np.arange(int(dur * sr)) / sr
    env = np.minimum(1, t * 50) * np.exp(-t * 1.5)
    return amp * env * (np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(4 * np.pi * freq * t))


def _kick(sr=SR):
    t = np.arange(int(0.25 * sr)) / sr
    f = 50 + 100 * np.exp(-t * 30)
    return 0.9 * np.exp(-t * 12) * np.sin(2 * np.pi * np.cumsum(f) / sr)


def _hat(sr=SR, rng=np.random.default_rng(0)):
    n = int(0.05 * sr)
    noise = rng.standard_normal(n)
    noise = np.diff(noise, prepend=0)  # crude high-pass
    return 0.15 * noise * np.exp(-np.arange(n) / sr * 80)


def make_track(bpm: float = 120.0, tonic: int = 9, mode: str = "minor", bars_per_section=(8, 16, 8), sr: int = SR):
    """Kick on every beat, off-beat hats, a triad progression in the key.

    Sections: an intro with no drums, a full section, and a quiet outro, each
    `bars_per_section[i]` bars long. Returns (audio, ground_truth).
    """
    beat = 60.0 / bpm
    bar = 4 * beat
    total_bars = sum(bars_per_section)
    y = np.zeros(int((total_bars * bar + 1.0) * sr))
    third = 3 if mode == "minor" else 4
    # i - VI - III - VII in minor, I - V - vi - IV in major (semitones from tonic)
    prog = [(0, third), (8, 4), (3, 4), (10, 4)] if mode == "minor" else [(0, 4), (7, 4), (9, 3), (5, 4)]

    def add(sig, t0, gain=1.0):
        i = int(t0 * sr)
        j = min(y.size, i + sig.size)
        y[i:j] += gain * sig[: j - i]

    bar_idx = 0
    sections = []
    for s, n in enumerate(bars_per_section):
        sections.append({"start": bar_idx * bar, "end": (bar_idx + n) * bar, "drums": s == 1})
        for b in range(n):
            t_bar = (bar_idx + b) * bar
            root, q = prog[(bar_idx + b) % 4]
            for iv in (0, q, 7):
                add(_tone(220 * 2 ** (((tonic + root + iv) % 12 - 9) / 12), bar), t_bar, 0.6)
            add(_tone(55 * 2 ** (((tonic + root) % 12 - 9) / 12), bar, amp=0.35), t_bar)
            if s == 1:
                for k in range(4):
                    add(_kick(), t_bar + k * beat)
                    add(_hat(), t_bar + k * beat + beat / 2)
        bar_idx += n
    y = (y / (np.abs(y).max() + 1e-9) * 0.9).astype(np.float32)
    truth = {"bpm": bpm, "tonic": tonic, "mode": mode, "bar_seconds": bar, "sections": sections}
    return y, truth


def write_wav(path, y, sr: int = SR):
    import soundfile as sf

    sf.write(str(path), y, sr)
    return path


class TruthCoreStage:
    """Drop-in replacement for the `core` stage that fills core from ground truth.

    Lets downstream stages (sections, tags, suggestions) be built and tested
    before the real core estimators exist:

        register_stage(TruthCoreStage(truth), replace=True)
    """

    name = "core"
    version = "truth"
    requires: tuple = ()

    def __init__(self, truth: dict, energy: float = 0.6):
        self.truth = truth
        self.energy = energy

    def run(self, ctx) -> None:
        from tastr.card import Key
        from tastr.music import camelot

        t = self.truth
        beat = 60.0 / t["bpm"]
        n_beats = int(ctx.card.duration / beat)
        core = ctx.card.core
        core.bpm = float(t["bpm"])
        core.bpm_confidence = 1.0
        core.key = Key(tonic=t["tonic"], mode=t["mode"], scale="Minor" if t["mode"] == "minor" else "Major",
                       camelot=camelot(t["tonic"], t["mode"]), confidence=1.0)
        core.energy = self.energy
        core.beat_times = [round(i * beat, 4) for i in range(n_beats)]
        core.downbeat_times = core.beat_times[::4]
