"""Core features: BPM, key, energy, loudness, beat grid and the shared scalar features.

Everything here is heuristic signal processing on top of librosa. The goal is
a useful starting point for a producer, not ground truth, so estimates carry
a confidence where that makes sense. Silent or featureless audio leaves
bpm / key as None rather than guessing.
"""

from __future__ import annotations

import numpy as np

from .. import music, signals
from ..card import Key
from ..pipeline import AnalysisContext, register_stage

MIN_BPM = 70.0
MAX_BPM = 180.0
MIN_BEATS = 8


class CoreStage:
    name = "core"
    version = "0.1.0"
    requires: tuple[str, ...] = ()

    def run(self, ctx: AnalysisContext) -> None:
        core = ctx.card.core
        y = ctx.y
        sr = ctx.sr
        duration = y.size / sr

        core.loudness_db = round(float(20 * np.log10(np.sqrt(np.mean(y**2)) + 1e-9)), 2)
        if y.size == 0 or not np.any(np.abs(y) > 1e-4):
            return  # silence: nothing to estimate

        rms = ctx.get("rms", signals.rms)
        rms_db = _amp_to_db(rms)
        onset_env = ctx.get("onset_env", signals.onset_envelope)
        y_harm, y_perc = ctx.get("hpss", signals.hpss)
        n_frames = onset_env.size

        # --- Rhythm -----------------------------------------------------------
        low_flux = ctx.get("low_flux", signals.low_band_flux)
        bpm, beat_frames, bpm_conf, stability, pulse_clarity = _tempo_and_beats(ctx, onset_env, low_flux, n_frames)
        beat_times = beat_frames * ctx.hop / sr
        beat_idx = np.clip(np.round(beat_frames).astype(int), 0, max(n_frames - 1, 0))
        if bpm is not None:
            core.bpm = round(bpm, 2)
            core.bpm_confidence = round(bpm_conf, 3)
            core.beat_times = [round(float(t), 3) for t in beat_times if t <= duration]
            phase = _downbeat_phase(beat_idx, low_flux, onset_env)
            core.downbeat_times = [round(float(t), 3) for t in beat_times[phase::4] if t <= duration]
            ctx.card.extras["core.bpm_alternatives"] = [round(bpm / 2, 2), round(bpm * 2, 2)]
            ctx.card.extras["core.downbeat_phase"] = int(phase)

        # --- Spectral / loudness features --------------------------------------
        import librosa

        spec = np.abs(librosa.stft(y, hop_length=ctx.hop))
        centroid = librosa.feature.spectral_centroid(S=spec, sr=sr)[0]
        freqs = librosa.fft_frequencies(sr=sr)
        power = spec**2
        low_end = float(power[freqs < 150].sum() / (power.sum() + 1e-9))
        brightness = float(np.clip(np.mean(centroid) / 3500.0, 0, 1))
        perc_ratio = float(np.sum(y_perc**2) / (np.sum(y_harm**2) + np.sum(y_perc**2) + 1e-9))
        onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, hop_length=ctx.hop)
        onset_rate = float(onsets.size / duration) if duration > 0 else 0.0
        perc_score = float(np.clip(perc_ratio / 0.5, 0, 1))

        loud_score = float(np.clip((core.loudness_db + 30) / 22, 0, 1))
        energy = (
            0.35 * loud_score
            + 0.25 * float(np.clip(onset_rate / 6.0, 0, 1))
            + 0.20 * brightness
            + 0.20 * perc_score
        )
        core.energy = round(float(np.clip(energy, 0, 1)), 3)

        tempo_fit = float(np.exp(-(((bpm - 122) / 30) ** 2))) if bpm else 0.0
        danceability = 0.55 * pulse_clarity + 0.25 * tempo_fit + 0.20 * perc_score
        swing_pct, swing_conf = _swing(onset_env, beat_idx) if bpm else (50.0, 0.0)

        ctx.card.features.update({
            "brightness": round(brightness, 3),
            "percussiveness": round(perc_ratio, 3),
            "low_end": round(low_end, 3),
            "pulse_clarity": round(pulse_clarity, 3),
            "danceability": round(float(np.clip(danceability, 0, 1)), 3),
            "onset_rate": round(onset_rate, 3),
            "dynamic_range_db": round(float(np.percentile(rms_db, 95) - np.percentile(rms_db, 10)), 2),
            "swing_percent": round(swing_pct, 1),
            "swing_confidence": round(swing_conf, 3),
            "tempo_stability": round(stability, 3),
        })

        # --- Key ----------------------------------------------------------------
        chroma = ctx.get("chroma", signals.chroma)
        profile = chroma.mean(axis=1)
        if np.any(profile > 0):
            est = music.estimate_key(profile)
            core.key = Key(
                tonic=est["tonic"],
                mode=est["mode"],
                scale=music.refine_mode(profile, est["tonic"], est["mode"]),
                camelot=music.camelot(est["tonic"], est["mode"]),
                confidence=est["confidence"],
            )
            ctx.card.extras["core.key_alternatives"] = est.get("alternatives", [])

        # --- Energy per bar ------------------------------------------------------
        if len(core.downbeat_times) >= 1:
            ctx.card.series["energy"] = _bar_energy_series(core.downbeat_times, rms_db, ctx, duration)


register_stage(CoreStage())


# ---------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------


def _amp_to_db(x: np.ndarray) -> np.ndarray:
    return 20 * np.log10(np.maximum(x, 1e-5))


def _tempo_and_beats(ctx: AnalysisContext, onset_env: np.ndarray, low_flux: np.ndarray, n_frames: int):
    """Beat frames, tempo refined from the beat spacing, folded into MIN_BPM..MAX_BPM.

    Returns (bpm, beat_frames, confidence, stability, pulse_clarity); bpm is None
    when the audio has no usable onsets.
    """
    import librosa

    if n_frames < 2 or onset_env.max() <= 1e-6 or onset_env.std() < 1e-6:
        return None, np.zeros(0, dtype=int), 0.0, 0.0, 0.0

    tempo_raw, frames = librosa.beat.beat_track(onset_envelope=onset_env, sr=ctx.sr, hop_length=ctx.hop)
    tempo = float(np.atleast_1d(tempo_raw)[0])
    frames = np.asarray(frames, dtype=float)
    if frames.size < MIN_BEATS or tempo <= 0:
        # Too few beats found: lay down a grid from the tempo estimate.
        tempo = tempo if tempo > 0 else 120.0
        frames = np.arange(0, n_frames, 60.0 / tempo * ctx.sr / ctx.hop)
    spb = np.median(np.diff(frames)) * ctx.hop / ctx.sr  # seconds per beat
    bpm = 60.0 / spb

    # Fold the tempo into a sensible range, keeping the beat grid consistent with it.
    while bpm < MIN_BPM:
        mids = (frames[:-1] + frames[1:]) / 2
        frames = np.sort(np.concatenate([frames, mids]))
        bpm *= 2
    while bpm >= MAX_BPM and frames.size >= 2 * MIN_BEATS:
        frames = frames[::2]
        bpm /= 2

    ibis = np.diff(frames) * ctx.hop / ctx.sr
    stability = float(np.clip(1.0 - np.std(ibis) / (np.mean(ibis) + 1e-9) * 4, 0, 1)) if ibis.size else 0.0
    frames = _regularize(frames, n_frames)
    frames = _align_to_kick(frames, low_flux)
    bpm = 60.0 * ctx.sr / (ctx.hop * float(np.median(np.diff(frames)))) if frames.size > 1 else bpm

    idx = np.clip(np.round(frames).astype(int), 0, n_frames - 1)
    pulse = float(np.clip((onset_env[idx].mean() / (onset_env.mean() + 1e-9) - 1.0) / 1.5, 0, 1))
    return bpm, frames, 0.5 * stability + 0.5 * pulse, stability, pulse


def _regularize(frames: np.ndarray, n_frames: int) -> np.ndarray:
    """Replace tracked beats with a uniform grid across the whole track when they are steady.

    Fitting a line through the beat positions removes hop quantisation from the
    tempo and extends the grid over stretches with no drums (intros, breakdowns),
    so every bar gets a downbeat. Drifting or wobbly tempos keep the tracker output.
    """
    if frames.size < MIN_BEATS:
        return frames
    diffs = np.diff(frames)
    typical = float(np.mean(diffs[diffs < 1.5 * np.median(diffs)]))  # mean, not median: no drift in k
    k = np.concatenate([[0.0], np.cumsum(np.maximum(np.round(diffs / typical), 1))])
    period, offset = np.polyfit(k, frames, 1)
    if period <= 0 or np.max(np.abs(frames - (offset + period * k))) > 0.2 * period:
        return frames
    first = int(np.ceil(-offset / period - 1e-9))
    last = int(np.floor((n_frames - 1 - offset) / period))
    return offset + period * np.arange(first, last + 1)


def _align_to_kick(frames: np.ndarray, low_flux: np.ndarray) -> np.ndarray:
    """Slide the beat grid onto the kick when the tracker locked onto something else.

    Trackers weight broadband onsets, so off-beat hats can win over a kick and
    leave the grid half a beat out. Try shifts within +-half a beat and move only
    when the kick evidence is clearly stronger than at the tracker's own position.
    """
    n = low_flux.size
    if frames.size < MIN_BEATS or n == 0 or low_flux.max() <= 0:
        return frames
    period = float(np.median(np.diff(frames)))
    half = int(period // 2)
    shifts = np.arange(-half, half + 1)
    scores = np.array([low_flux[np.clip(np.round(frames + d).astype(int), 0, n - 1)].mean() for d in shifts])
    base = scores[half]
    best = int(np.argmax(scores))
    if scores[best] <= base * 1.25 + 1e-9:
        return frames
    shifted = frames + shifts[best]
    shifted = shifted[(shifted >= -2) & (shifted <= n - 1)]
    while shifted[0] - period >= -2:  # keep the grid reaching the start of the track
        shifted = np.concatenate([[shifted[0] - period], shifted])
    return np.clip(shifted, 0, None)  # a beat a hair before 0 is beat 0


def _sample(env: np.ndarray, frames: np.ndarray, radius: int = 2) -> np.ndarray:
    """Peak of an envelope in a small window around each frame (tolerates timing slop)."""
    out = np.zeros(frames.shape, dtype=float)
    for i, f in enumerate(frames):
        a, b = max(0, int(f) - radius), min(env.size, int(f) + radius + 1)
        out[i] = env[a:b].max() if b > a else 0.0
    return out


def _downbeat_phase(beat_frames: np.ndarray, kick_env: np.ndarray, onset_env: np.ndarray) -> int:
    """Which of the first 4 beats starts bar 1 (assumes 4/4)."""
    if beat_frames.size < MIN_BEATS:
        return 0
    strength = _sample(kick_env, beat_frames) + 0.5 * _sample(onset_env, beat_frames)
    scores = [float(strength[p::4].mean()) for p in range(4)]
    best = int(np.argmax(scores))
    # Four-on-the-floor makes every phase look alike; only move off 0 for a clear winner.
    return best if scores[best] > scores[0] * 1.1 else 0


def _swing(onset_env: np.ndarray, beat_frames: np.ndarray) -> tuple[float, float]:
    """Where the off-beat 16th lands inside each beat, as (percent, confidence).

    50 = straight, ~66.7 = triplet shuffle. The second 16th sits at 25% of the
    beat when straight and 33% when fully swung, i.e. 50% / 66.7% of the way
    through the first 8th.
    """
    positions, weights = [], []
    for a, b in zip(beat_frames[:-1], beat_frames[1:]):
        length = int(b - a)
        if length < 8:
            continue
        lo, hi = int(a) + int(length * 0.18), int(a) + int(length * 0.42)
        window = onset_env[lo:hi]
        if window.size == 0:
            continue
        k = int(np.argmax(window))
        offset = float(k)
        if 0 < k < window.size - 1:  # parabolic interpolation for sub-frame position
            l, c, r = window[k - 1], window[k], window[k + 1]
            denom = l - 2 * c + r
            if denom < 0:
                offset += float(np.clip(0.5 * (l - r) / denom, -0.5, 0.5))
        positions.append((lo + offset - a) / length)
        weights.append(float(window[k]))
    if not positions or sum(weights) == 0:
        return 50.0, 0.0
    pos = float(np.average(positions, weights=weights))
    spread = float(np.sqrt(np.average((np.array(positions) - pos) ** 2, weights=weights)))
    percent = float(np.clip(pos / 0.5 * 100.0, 50.0, 75.0))
    return percent, float(np.clip(1 - spread / 0.08, 0, 1))


def _bar_energy_series(downbeats: list[float], rms_db: np.ndarray, ctx: AnalysisContext, duration: float):
    from ..card import Series

    edges = list(downbeats) + [duration]
    raw = []
    for a, b in zip(edges[:-1], edges[1:]):
        fa = int(a * ctx.sr / ctx.hop)
        fb = max(int(b * ctx.sr / ctx.hop), fa + 1)
        seg = rms_db[fa:fb] if fa < rms_db.size else rms_db[-1:]
        raw.append(float(np.mean(seg)))
    raw = np.array(raw)
    lo, hi = np.percentile(raw, 5), np.percentile(raw, 95)
    if hi - lo < 1e-9:
        norm = np.full_like(raw, 0.5)
    else:
        norm = np.clip((raw - lo) / (hi - lo), 0, 1)
    return Series(times=[round(float(t), 3) for t in downbeats], values=[round(float(v), 3) for v in norm])
