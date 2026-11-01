"""Core features: BPM, key, energy, loudness, beat grid and the shared scalar features.

Everything here is heuristic signal processing on top of librosa. The goal is
a useful starting point for a producer, not ground truth, so estimates carry
a confidence where that makes sense. Silent or featureless audio leaves
bpm / key as None rather than guessing.
"""

from __future__ import annotations

import os

import numpy as np

from .. import calibration, music, signals
from ..card import Key
from ..context import AnalysisContext, register_stage

MIN_BPM = 70.0
MAX_BPM = 180.0
MIN_BEATS = 8
# Switch to 1.5x the tracked tempo when the onset periodicity there is at least this strong relative
# to the tracked tempo. Drum & bass and jungle (150-175 BPM) get tracked at 2/3 tempo (100-117) because
# the tracker prefers tempos near 120; a straight 4/4 track has little periodicity at 1.5x its beat.
# Picked on the 200 hjdb train tracks (0.75-0.85 score alike), checked on hjdb val, Beatport (774)
# and GiantSteps (43); see docs/bench.md "Tempo notes".
THREE_TWO_RATIO = 0.8
# ...unless the faster beats group in 3s (meter score above this): then the tracked tempo was the beat of a
# 4/4 track and 1.5x lands on dotted eighths. On GTZAN this blocks 89 of 101 wrong switches and 2 of 17
# right ones; see docs/bench.md "Tempo notes".
THREE_TWO_MAX_METER = 0.0
# Pulse clarity: how much stronger the onset envelope is on the tracker's beats than on average, read on the
# tracker's own grid (before the kick alignment, which moves the grid onto the low-band kick, a frame or two
# before the broadband onset peak). (ratio - 1) / PULSE_SCALE, clipped to 0-1: noise scores about 0.1,
# EDM 0.45 to 1. Before 0.3.0 it was read after the alignment and /1.5, so it was 0 on most tracks.
PULSE_SCALE = 4.0


class CoreStage:
    name = "core"
    version = "0.3.0"
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
        mel = ctx.get("log_mel", signals.log_mel)  # on core's frame grid

        def meter_of(frames):
            return _meter_score(np.clip(np.round(frames).astype(int), 0, n_frames - 1), *mel)

        bpm, beat_frames, bpm_conf, stability, pulse_clarity = _tempo_and_beats(ctx, onset_env, low_flux, n_frames,
                                                                                 meter_of)
        beat_times = beat_frames * ctx.hop / sr
        beat_idx = np.clip(np.round(beat_frames).astype(int), 0, max(n_frames - 1, 0))
        if bpm is not None:
            core.bpm = round(bpm, 2)
            core.bpm_confidence = round(bpm_conf, 3)
            core.bpm_likely_correct = calibration.likely_correct("bpm", bpm_conf)
            core.beat_times = [round(float(t), 3) for t in beat_times if t <= duration]
            log_mel, mel_f = mel
            cues = _beat_cues(beat_idx, log_mel, mel_f) if beat_idx.size >= MIN_BEATS and log_mel.shape[1] >= 2 else None
            meter = _meter_score(beat_idx, log_mel, mel_f, cues=cues)
            guess = 3 if meter > METER_THRESHOLD else 4
            ctx.card.extras["core.meter_guess"] = {"beats_per_bar": guess, "score": round(meter, 2)}
            core.beats_per_bar, core.meter_set = _beats_per_bar(ctx.options.get("beats_per_bar"), guess)
            phase = _downbeat_phase(beat_idx, log_mel, mel_f, core.beats_per_bar, cues=cues)
            core.downbeat_times = [round(float(t), 3) for t in beat_times[phase::core.beats_per_bar] if t <= duration]
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

        # Weights fitted against MTG-Jamendo listener labels (docs/vibes.md): pulse clarity didn't help.
        tempo_fit = float(np.exp(-(((bpm - 135) / 45) ** 2))) if bpm else 0.0
        danceability = 0.25 * tempo_fit + 0.75 * perc_score
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
        profile = music.key_profile(chroma)
        if np.any(profile > 0):
            est = music.estimate_key(profile)
            core.key = Key(
                tonic=est["tonic"],
                mode=est["mode"],
                scale=music.refine_mode(profile, est["tonic"], est["mode"]),
                camelot=music.camelot(est["tonic"], est["mode"]),
                confidence=est["confidence"],
                likely_correct=calibration.likely_correct("key", est["confidence"]),
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


def _tempo_and_beats(ctx: AnalysisContext, onset_env: np.ndarray, low_flux: np.ndarray, n_frames: int,
                     meter_of=None):
    """Beat frames, tempo refined from the beat spacing, folded into MIN_BPM..MAX_BPM.

    `meter_of(beat_frames)` gives `_meter_score` for a beat grid; without it the 1.5x switch is ungated.

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
    if bpm * 1.5 < MAX_BPM and _periodicity(ctx, onset_env, bpm * 1.5) >= THREE_TWO_RATIO * _periodicity(ctx, onset_env, bpm):
        _, faster = librosa.beat.beat_track(onset_envelope=onset_env, sr=ctx.sr, hop_length=ctx.hop, bpm=bpm * 1.5)
        if faster.size >= MIN_BEATS and (meter_of is None or meter_of(np.asarray(faster, dtype=float)) <= THREE_TWO_MAX_METER):
            frames = np.asarray(faster, dtype=float)
            bpm = 60.0 / (np.median(np.diff(frames)) * ctx.hop / ctx.sr)

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
    idx = np.clip(np.round(frames).astype(int), 0, n_frames - 1)
    pulse = float(np.clip((onset_env[idx].mean() / (onset_env.mean() + 1e-9) - 1.0) / PULSE_SCALE, 0, 1))
    frames = _align_to_kick(frames, low_flux)
    bpm = 60.0 * ctx.sr / (ctx.hop * float(np.median(np.diff(frames)))) if frames.size > 1 else bpm
    return bpm, frames, 0.5 * stability + 0.5 * pulse, stability, pulse


def _periodicity(ctx: AnalysisContext, onset_env: np.ndarray, bpm: float) -> float:
    """Mean tempogram strength at `bpm` (best of the nearest three lags), cached per track."""
    import librosa

    def tempogram(c):
        ac = librosa.feature.tempogram(onset_envelope=onset_env, sr=c.sr, hop_length=c.hop, win_length=384).mean(axis=1)
        return ac, librosa.tempo_frequencies(ac.size, sr=c.sr, hop_length=c.hop)

    ac, bpms = ctx.get("core.tempogram", tempogram)
    i = int(np.argmin(np.abs(bpms[1:] - bpm))) + 1
    return float(ac[max(i - 1, 1) : i + 2].max())


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


def _beat_cues(beat_frames: np.ndarray, log_mel: np.ndarray, freqs: np.ndarray) -> dict:
    """Per-beat bar cues read from the log-mel spectrogram (docs/rhythm.md).

    kick and snare are onset strengths on each beat; timbre and bass are each
    beat's mean spectrum; change and bass_change how much they moved from the
    previous beat, zeroed near silence (going in or out of it is a big change
    wherever it falls). `quiet_near` marks those beats for the bar-level cue.
    """
    n_frames = log_mel.shape[1]
    frames = np.clip(beat_frames, 0, n_frames - 1)

    def flux(lo, hi):
        x = log_mel[(freqs >= lo) & (freqs < hi)]
        return np.maximum(0.0, np.diff(x, axis=1, prepend=x[:, :1])).mean(axis=0)

    def beat_spectra(lo, hi):
        x = log_mel[(freqs >= lo) & (freqs < hi)]
        edges = list(frames) + [min(n_frames, frames[-1] + max(1, frames[-1] - frames[-2]))]
        return np.stack([x[:, a:max(b, a + 1)].mean(axis=1) for a, b in zip(edges[:-1], edges[1:])])

    timbre = beat_spectra(60, 5000)
    bass = beat_spectra(30, 250)
    change = np.r_[0.0, np.linalg.norm(np.diff(timbre, axis=0), axis=1)]
    bass_change = np.r_[0.0, np.linalg.norm(np.diff(bass, axis=0), axis=1)]
    level = timbre.mean(axis=1)
    quiet = level < 0.5 * np.median(level)
    near_quiet = quiet | np.r_[False, quiet[:-1]] | np.r_[quiet[1:], False]
    for i in np.flatnonzero(quiet):
        near_quiet[max(0, i - 4):i + 5] = True
    change[near_quiet] = bass_change[near_quiet] = 0.0
    return {"kick": _sample(flux(30, 150), frames), "snare": _sample(flux(180, 2500), frames),
            "timbre": timbre, "bass": bass, "change": change, "bass_change": bass_change, "near_quiet": near_quiet}


def _downbeat_phase(beat_frames: np.ndarray, log_mel: np.ndarray, freqs: np.ndarray, beats_per_bar: int = 4,
                    cues: dict | None = None) -> int:
    """Which of the first `beats_per_bar` beats starts bar 1 (4 or 3).

    Five cues each score the possible phases; they are z-scored and summed:
    a heavier kick on the downbeat, a snare backbeat (beats 2 and 4 in 4/4,
    2 and 3 in 3/4), and the timbre, chords (beat-to-beat and bar-to-bar)
    and bass changing as a new bar starts. Kick and snare alone can't tell
    bar 1 from beat 3 (the old half-bar offset); the change cues can. Tuned
    on GTZAN (docs/rhythm.md).
    """
    if beat_frames.size < MIN_BEATS or log_mel.shape[1] < 2:
        return 0
    m = beats_per_bar
    c = cues or _beat_cues(beat_frames, log_mel, freqs)
    timbre = c["timbre"]
    n = len(timbre)
    bar_change = np.zeros(n)
    for i in range(m, n - m + 1):
        bar_change[i] = np.linalg.norm(timbre[i:i + m].mean(axis=0) - timbre[i - m:i].mean(axis=0))
    bar_change[c["near_quiet"]] = 0.0

    def per_phase(v):
        return np.array([v[p::m].mean() for p in range(m)])

    sn = per_phase(c["snare"])
    if m == 4:
        backbeat = np.array([sn[(p + 1) % 4] + sn[(p + 3) % 4] - sn[p] - sn[(p + 2) % 4] for p in range(4)])
    else:  # every other beat against the downbeat
        backbeat = (sn.sum() - sn) / (m - 1) - sn
    total = np.zeros(m)
    for sc in (per_phase(c["kick"]), backbeat, per_phase(c["change"]), per_phase(c["bass_change"]), per_phase(bar_change)):
        total += (sc - sc.mean()) / (sc.std() + 1e-9)
    best = int(np.argmax(total))
    # Without a clear winner (a loop that repeats every bar exactly), trust the first beat: most tracks start on bar 1.
    return best if total[best] - total[0] > 1.0 else 0


def _beats_per_bar(chosen, guess: int) -> tuple[int, bool]:
    """(beats per bar, set by the user): the caller's choice (3 or 4) wins; else the detected guess,
    unless TASTR_METER=4/4 turns 3/4 detection off."""
    if chosen in (3, 4):
        return int(chosen), True
    if os.environ.get("TASTR_METER", "auto").strip().lower() in ("4", "4/4", "off"):
        return 4, False
    return guess, False


# 3 beats per bar vs 4: each cue is >0 when the music repeats every 3 beats more than every 4. Scales are
# the cues' spread over GTZAN (labeled beats), so each counts about equally; see docs/rhythm.md.
METER_CUE_SCALES = {"timbre_lag": 0.1, "bass_lag": 0.27, "kick": 0.3, "snare": 0.25, "change": 0.17, "bass_change": 0.22}
METER_THRESHOLD = 1.0  # summed scaled cues above this read as 3/4; below stays 4/4


def _meter_score(beat_frames: np.ndarray, log_mel: np.ndarray, freqs: np.ndarray, cues: dict | None = None) -> float:
    """How much more the beats group in 3s than in 4s (>METER_THRESHOLD reads as 3/4).

    Two kinds of cue: beat spectra a 3 or 6 beats apart look more alike than 4 or 8 apart (timbre and
    bass), and kick, snare and change strengths vary more across the 3 phases of a 3-beat cycle
    than across the 4 of a 4-beat one.
    """
    if beat_frames.size < 2 * MIN_BEATS or log_mel.shape[1] < 2:
        return 0.0
    c = cues or _beat_cues(beat_frames, log_mel, freqs)

    def lag_ratio(x):
        d = {k: np.linalg.norm(x[k:] - x[:-k], axis=1).mean() for k in (3, 4, 6, 8)}
        return float(np.log((d[4] + d[8] + 1e-9) / (d[3] + d[6] + 1e-9)))

    def phase_spread(v, m):
        return float(np.std([v[p::m].mean() for p in range(m)]) / (v.std() + 1e-9))

    raw = {"timbre_lag": lag_ratio(c["timbre"]), "bass_lag": lag_ratio(c["bass"])}
    for k in ("kick", "snare", "change", "bass_change"):
        raw[k] = phase_spread(c[k], 3) - phase_spread(c[k], 4)
    return float(sum(raw[k] / s for k, s in METER_CUE_SCALES.items()))


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
