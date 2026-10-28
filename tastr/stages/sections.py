"""Rough section detection (intro / build / drop / breakdown / outro...).

Boundaries come from temporally-constrained clustering of per-bar timbre,
harmony and loudness, snapped to 4-bar phrases. Labels are guessed from each
section's energy relative to the rest of the track, so they are a starting
point for arranging ideas, not a transcription.
"""

from __future__ import annotations

import numpy as np

from .. import signals
from ..card import Section
from ..pipeline import AnalysisContext, register_stage

FALLBACK_BAR_SECONDS = 8.0  # stand-in "bar" length when core gave no downbeats
PHRASE_BARS = 4  # boundaries snap to multiples of this, sections are at least this long
MIN_BARS_FOR_SPLIT = 8  # shorter tracks get a single "full" section
LETTER_DISTANCE = 0.55  # RMS distance per z-scored dim below which two sections share a letter


class SectionsStage:
    name = "sections"
    version = "0.1.0"
    requires = ("core",)

    def run(self, ctx: AnalysisContext) -> None:
        card = ctx.card
        duration = card.duration or ctx.y.size / ctx.sr
        bar_times = _bar_times(card.core.downbeat_times, duration)
        n_bars = len(bar_times)

        bar_energy = _bar_energy(ctx, bar_times, duration)
        if n_bars < MIN_BARS_FOR_SPLIT:
            card.sections = [
                Section(start=0.0, end=round(duration, 3), label="full", letter="A", start_bar=1,
                        end_bar=max(n_bars, 1), energy=round(float(bar_energy.mean()), 3) if bar_energy.size else 0.5,
                        confidence=0.0)
            ]
            _set_features(card.features, card.sections)
            return

        z = _bar_vectors(ctx, bar_times, duration)
        starts = _snap(_boundaries(z, duration), n_bars)

        edges = starts + [n_bars]
        spans = [(edges[i], edges[i + 1]) for i in range(len(starts))]
        sections = []
        for i, (a, b) in enumerate(spans):
            sections.append(
                Section(
                    start=0.0 if i == 0 else round(float(bar_times[a]), 3),
                    end=round(duration, 3) if i == len(spans) - 1 else round(float(bar_times[b]), 3),
                    label="",
                    start_bar=a + 1,
                    end_bar=b,
                    energy=round(float(bar_energy[a:b].mean()), 3),
                )
            )
        vecs = [z[:, a:b].mean(axis=1) for a, b in spans]
        for s, letter in zip(sections, _letters(vecs)):
            s.letter = letter
        electronic = _is_electronic(ctx)
        for i, s in enumerate(sections):
            s.label = _label(i, sections, electronic)
        for s, c in zip(sections, _confidences(vecs)):
            s.confidence = c

        card.sections = sections
        _set_features(card.features, sections)


def _bar_times(downbeats: list[float], duration: float) -> np.ndarray:
    """Bar start times in seconds; falls back to fixed blocks when core has no downbeats."""
    t = np.asarray([d for d in downbeats if 0.0 <= d < duration], dtype=float)
    if t.size < 2:
        t = np.arange(0.0, duration, FALLBACK_BAR_SECONDS)
    return t


def _bar_spans(ctx: AnalysisContext, bar_times: np.ndarray, duration: float, n_frames: int) -> list[tuple[int, int]]:
    """Frame range [a, b) for each bar; the first bar is stretched back to frame 0 and the last to the end."""
    edges = np.append(bar_times, duration)
    frames = np.clip(np.round(edges * ctx.sr / ctx.hop).astype(int), 0, n_frames)
    frames[0] = 0
    frames[-1] = n_frames
    return [(int(frames[i]), int(max(frames[i + 1], frames[i] + 1))) for i in range(len(bar_times))]


def _scale01(x: np.ndarray) -> np.ndarray:
    """Robust min-max to 0..1 so one loud spike doesn't flatten the rest."""
    if x.size == 0:
        return x
    lo, hi = np.percentile(x, 5), np.percentile(x, 95)
    if hi - lo < 1e-9:
        return np.full(x.shape, 0.5)
    return np.clip((x - lo) / (hi - lo), 0.0, 1.0)


def _bar_energy(ctx: AnalysisContext, bar_times: np.ndarray, duration: float) -> np.ndarray:
    """Per-bar energy 0..1 relative to this track. Uses core's energy series when it has one."""
    series = ctx.card.series.get("energy")
    if series is not None and len(series.times) >= 2:
        ends = np.append(bar_times[1:], duration)
        mids = (bar_times + ends) / 2
        return _scale01(np.interp(mids, series.times, series.values))

    import librosa

    rms = ctx.get("rms", signals.rms)
    db = librosa.amplitude_to_db(rms, ref=1.0)
    onset = ctx.get("onset_envelope", signals.onset_envelope)
    spans = _bar_spans(ctx, bar_times, duration, db.size)
    raw = [
        db[a:b].mean() + 3.0 * (onset[a:b].mean() if a < onset.size and b > a else 0.0) for a, b in spans
    ]
    return _scale01(np.asarray(raw))


def _bar_vectors(ctx: AnalysisContext, bar_times: np.ndarray, duration: float) -> np.ndarray:
    """Z-scored per-bar features: chroma (12) + MFCC 2-13 + loudness, shaped features x bars."""
    import librosa

    chroma = ctx.get("chroma", signals.chroma)
    mfcc = ctx.get("mfcc", signals.mfcc)
    db = librosa.amplitude_to_db(ctx.get("rms", signals.rms), ref=1.0)
    n = min(chroma.shape[1], mfcc.shape[1], db.size)
    feats = np.vstack([chroma[:, :n], mfcc[1:, :n], db[None, :n]])
    spans = _bar_spans(ctx, bar_times, duration, n)
    bars = np.stack([feats[:, min(a, n - 1) : max(min(b, n), min(a, n - 1) + 1)].mean(axis=1) for a, b in spans], axis=1)
    return (bars - bars.mean(axis=1, keepdims=True)) / (bars.std(axis=1, keepdims=True) + 1e-9)


def _boundaries(z: np.ndarray, duration: float) -> list[int]:
    """Bar indices where a new section starts (always includes 0), before snapping."""
    import librosa

    n_bars = z.shape[1]
    k = int(np.clip(round(duration / 28), 3, 12))
    k = min(k, max(2, n_bars // PHRASE_BARS))
    # Returns the k left-boundary bar indices of the segments (always starting at 0).
    return sorted({0} | {int(b) for b in librosa.segment.agglomerative(z, k) if 0 < b < n_bars})


def _snap(starts: list[int], n_bars: int) -> list[int]:
    """Snap to 4-bar phrases and merge any section shorter than 4 bars into its neighbour."""
    snapped = sorted({0} | {round(s / PHRASE_BARS) * PHRASE_BARS for s in starts[1:]})
    merged = [0]
    for s in snapped[1:]:
        if s < n_bars and s - merged[-1] >= PHRASE_BARS:
            merged.append(s)
    if n_bars - merged[-1] < PHRASE_BARS and len(merged) > 1:
        merged.pop()
    return merged


def _letters(vecs: list[np.ndarray]) -> list[str]:
    """Sections that sound alike share a letter (A, B, A, C...)."""
    letters, protos = [], []
    for v in vecs:
        dists = [np.linalg.norm(v - p) / np.sqrt(v.size) for p in protos]
        if dists and min(dists) < LETTER_DISTANCE:
            letters.append(chr(65 + int(np.argmin(dists))))
        else:
            protos.append(v)
            letters.append(chr(65 + min(len(protos) - 1, 25)))
    return letters


def _confidences(vecs: list[np.ndarray]) -> list[float]:
    """How distinct each section is from the one before it (the first uses the one after)."""
    if len(vecs) < 2:
        return [0.0] * len(vecs)
    gap = [np.linalg.norm(vecs[i] - vecs[i - 1]) / np.sqrt(vecs[i].size) for i in range(1, len(vecs))]
    out = [gap[0]] + gap
    return [round(float(np.clip(g / 1.5, 0.0, 1.0)), 3) for g in out]


def _is_electronic(ctx: AnalysisContext) -> bool:
    """Percussive and fast enough that a loud section is a "drop" rather than a "chorus"."""
    bpm = ctx.card.core.bpm
    if not bpm or bpm <= 105:
        return False
    perc = ctx.card.features.get("percussiveness")
    if perc is None:
        y_harm, y_perc = ctx.get("hpss", signals.hpss)
        perc = float(np.sum(y_perc**2) / (np.sum(y_harm**2) + np.sum(y_perc**2) + 1e-9))
    return perc > 0.25


def _label(i: int, sections: list[Section], electronic: bool) -> str:
    e = sections[i].energy or 0.0
    last = len(sections) - 1
    nxt = (sections[i + 1].energy or 0.0) if i < last else None
    if i == 0 and e < 0.5:
        return "intro"
    if i == last and e < 0.5:
        return "outro"
    if e >= 0.68:
        return "drop" if electronic else "chorus"
    if nxt is not None and nxt - e > 0.2 and nxt >= 0.6:
        return "build"
    if e < 0.38:
        return "breakdown"
    return "verse"


def _set_features(features: dict[str, float], sections: list[Section]) -> None:
    energies = [s.energy for s in sections if s.energy is not None]
    features["section_contrast"] = round(float(np.std(energies)), 3) if energies else 0.0
    features["section_count"] = float(len(sections))


register_stage(SectionsStage())
