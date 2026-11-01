"""Drum lane activations: how strongly each kind of drum hits at given frames.

The percussive spectrogram is split into three parts (low, mid and high hits)
with semi-adaptive NMF (non-negative matrix factorisation): each part starts
from a fixed spectral template (kick below 120 Hz; snare body plus mid noise;
hats above 6 kHz) that adapts a little to the track. A part's activation rises
only when its kind of sound arrives, so hats no longer leak into the snare lane
and a kick under a clap still counts for both. What one part still picks up
when another drum hits alone is measured per track and subtracted.

Each hit is then sorted into a lane by a closer look:

- low hits: kick, or tom when the pitch sits above about 120 Hz;
- mid hits: clap when the mid band has several quick bursts and little body,
  perc (cowbell, rim, conga) when the spectrum is pitched, otherwise snare;
- high hits: open hat when the hat band keeps ringing, otherwise closed hat.

    acts = lane_activations(ctx, frames)   # {lane: array shaped like frames}
    grid = drum_grid(ctx, step_grid(ctx, n_frames))   # one bar's pattern per lane, for the benchmarks

Activations are non-negative and comparable within a lane across a track.
Design notes, what was tried and dropped, and known weaknesses: docs/drums.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import signals
from .context import AnalysisContext

LANES = ("kick", "snare", "clap", "hat", "open_hat", "tom", "perc")
# With the optional drum model (tastr[drums], adt.py): which lanes it supplies, and the lane it adds.
MODEL_LANES = ("kick", "hat", "tom", "cymbal")
MODEL_OPEN_HATS = False  # True: split the model's hi-hats into closed and open with the ringing test below
log = logging.getLogger(__name__)

# What each lane measures, for UI tooltips and docs.
LANE_HELP = {
    "kick": "Low hits whose energy sits below about 120 Hz.",
    "snare": "Noisy mid hits (400 Hz to 5 kHz), usually with some body around 200 Hz.",
    "clap": "Noisy mid hits with little body, made of several quick bursts.",
    "hat": "Short hits above 6 kHz (closed hi-hat, shaker).",
    "open_hat": "Hits above 6 kHz that keep ringing (open hi-hat, ride, crash).",
    "tom": "Low hits pitched above about 120 Hz, higher than a kick.",
    "perc": "Pitched mid hits without body (cowbell, rim, conga, wood block).",
}
# The same, for lanes the drum model finds (it learned them from annotated real music).
MODEL_LANE_HELP = {
    "kick": "Kick drum hits, found by the ADTOF drum model.",
    "hat": "Hi-hat hits, found by the ADTOF drum model.",
    "tom": "Tom hits (high, mid and floor toms), found by the ADTOF drum model.",
    "cymbal": "Crash and ride cymbal hits, found by the ADTOF drum model.",
}

# Spectral templates as (centre Hz, width in octaves, weight) bumps, in mel magnitude.
TEMPLATES = {
    "kick": [(60, 0.7, 1.0), (3000, 1.0, 0.01)],
    "snare": [(200, 0.4, 0.6), (1200, 1.0, 1.0)],
    "hat": [(6000, 1.0, 1.0)],
}
TOM_PITCH = (100.0, 135.0)  # Hz: below, a low hit is a kick; above, a tom
UNMIX_PERCENTILE = 20
# Part pairs (from, to) that pick up each other's hits: kicks and hats both bleed into the snare
# part, a snare's body into the kick. Other pairs share too little spectrum, and two drums that
# always land together would otherwise look like a leak and erase each other.
LEAKS = {("kick", "snare"), ("hat", "snare"), ("snare", "kick")}


def _ramp(x, lo, hi):
    return np.clip((np.asarray(x, dtype=float) - lo) / (hi - lo), 0.0, 1.0)


def _templates(freqs: np.ndarray) -> np.ndarray:
    f = np.maximum(freqs, 10.0)
    cols = []
    for bumps in TEMPLATES.values():
        w = sum(a * np.exp(-0.5 * (np.log2(f / c) / wd) ** 2) for c, wd, a in bumps) + 1e-4
        cols.append(w / w.sum())
    return np.stack(cols, axis=1)


def _nmf(V: np.ndarray, W0: np.ndarray, n_iter: int = 60):
    """KL-divergence NMF whose templates W0 adapt only late and partly (semi-adaptive)."""
    W = W0.copy()
    H = np.full((W.shape[1], V.shape[1]), V.mean() + 1e-9)
    eps = 1e-12
    for it in range(n_iter):
        WH = W @ H + eps
        H *= (W.T @ (V / WH)) / (W.sum(axis=0)[:, None] + eps)
        WH = W @ H + eps
        W_new = W * ((V / WH) @ H.T) / (H.sum(axis=1)[None, :] + eps)
        W_new /= W_new.sum(axis=0, keepdims=True) + eps
        alpha = (it / n_iter) ** 4 * 0.5
        W = (1 - alpha) * W0 + alpha * W_new
        W /= W.sum(axis=0, keepdims=True)
    return W, H


def decompose(ctx: AnalysisContext) -> dict[str, np.ndarray]:
    """Per-template activation over time ({name: frames}) for the percussive part."""
    mel, freqs = ctx.get("mel_percussive", signals.mel_percussive)
    V = np.sqrt(np.maximum(mel, 0.0))
    W0 = _templates(freqs)
    W, H = _nmf(V, W0)
    parts = {name: H[i] for i, name in enumerate(TEMPLATES)}
    parts["_W"] = W
    return parts


def _local_peaks(x: np.ndarray) -> np.ndarray:
    return np.flatnonzero((x[1:-1] >= x[:-2]) & (x[1:-1] > x[2:])) + 1


def _unmix(nov: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Remove what each part typically picks up when another drum hits alone.

    NMF still hands a little of each hit to neighbouring templates (a hat to the
    snare's noise, a snare's body to the kick). For each pair, the leak ratio is
    a low percentile of dst/src over src's strongest onsets, which are mostly
    solo hits; that much of src's onset is then taken off dst.
    """
    names = list(nov)
    out = {k: v.copy() for k, v in nov.items()}
    # Parts can peak a frame apart for the same hit, so measure the leak over a 3-frame window.
    wide = {k: np.maximum(v, np.maximum(np.roll(v, 1), np.roll(v, -1))) for k, v in nov.items()}
    for src in names:
        x = nov[src]
        pk = _local_peaks(x)
        if pk.size < 8:
            continue
        strong = pk[x[pk] >= np.percentile(x[pk], 70)]
        for dst in names:
            if (src, dst) not in LEAKS or strong.size < 4:
                continue
            ratio = wide[dst][strong] / (x[strong] + 1e-12)
            c = float(np.percentile(ratio, UNMIX_PERCENTILE))
            # A real leak is small; a big ratio means the two drums are simply always played together.
            if 0 < c <= 1.0:
                # The snare part sits between the others and catches their hits a frame early or late too.
                out[dst] -= c * (wide[src] if dst == "snare" else x)
    return {k: np.maximum(v, 0.0) for k, v in out.items()}


def onsets(ctx: AnalysisContext) -> dict[str, np.ndarray]:
    """Onset strength of each part per frame: rise in its activation over two frames, leakage removed."""
    acts = {k: v for k, v in ctx.get("drum_nmf", decompose).items() if not k.startswith("_")}
    nov = {k: np.maximum(0.0, v - np.concatenate([v[:2], v[:-2]])) for k, v in acts.items()}
    # The air band's own flux finds soft hats the NMF hat part misses (and scores better on IDMT-SMT-Drums).
    nov["hat"] = _air_flux(ctx)
    return _unmix(nov)


def _air_flux(ctx: AnalysisContext) -> np.ndarray:
    """Mean per-bin dB rise above 6 kHz: very sensitive to hats, which have little else up there to compete with."""
    import librosa

    mel, freqs = ctx.get("mel_percussive", signals.mel_percussive)
    hi = mel[freqs >= 6000]
    if not hi.size:
        return np.zeros(mel.shape[1])
    # Floor 50 dB under the band's loudest moments, so a near-silent band can't post big dB jumps.
    db = librosa.power_to_db(hi + 1e-10, ref=float(np.percentile(hi, 99.9)), top_db=50.0)
    return np.maximum(0.0, np.diff(db, axis=1, prepend=db[:, :1])).mean(axis=0)


def _envelopes(ctx: AnalysisContext):
    """Fine-grained (4 ms) amplitude envelopes in dB of the percussive part's mid and air bands."""
    from scipy import signal

    _, y = ctx.get("hpss", signals.hpss)
    sr = ctx.sr
    nyq = sr / 2
    out = {}
    for name, (lo, hi) in {"mid": (800, 3000), "air": (6000, None)}.items():
        if hi is None:
            sos = signal.butter(4, min(lo, nyq * 0.8), btype="highpass", fs=sr, output="sos")
        else:
            sos = signal.butter(4, [lo, min(hi, nyq * 0.9)], btype="bandpass", fs=sr, output="sos")
        x = np.abs(signal.sosfiltfilt(sos, y))
        win = max(1, int(0.004 * sr))
        out[name] = 20 * np.log10(np.convolve(x, np.ones(win) / win, mode="same") + 1e-7)
    return out


def _bursts(env_db: np.ndarray, i: int, sr: int) -> int:
    """Number of quick bursts in the mid band within 40 ms of sample i."""
    from scipy.signal import find_peaks

    a, b = max(0, i - int(0.015 * sr)), min(env_db.size, i + int(0.04 * sr))
    seg = env_db[a:b]
    if seg.size < 8:
        return 0
    peaks, _ = find_peaks(seg, prominence=6.0, distance=max(1, int(0.004 * sr)), height=seg.max() - 10)
    return int(len(peaks))


def _ringing(env_db: np.ndarray, i: int, sr: int, gap: float) -> float:
    """How much the air band is still ringing after a hit at sample i (1 = open hat)."""
    a, b = max(0, i - int(0.015 * sr)), min(env_db.size, i + int(0.03 * sr))
    if b <= a:
        return 0.0
    p = a + int(np.argmax(env_db[a:b]))
    # Look 90 ms on, or just before the next 16th, whichever is sooner.
    look = min(0.09, max(0.03, gap - 0.02))
    j = min(env_db.size - 1, p + int(look * sr))
    drop_per_90ms = (env_db[p] - env_db[j]) * 0.09 / look
    # Closed hats fall 20 dB or more in 90 ms; open hats under 10 dB.
    return float(1.0 - _ramp(drop_per_90ms, 9.0, 16.0))


def _mid_kind(body_share: float, peaky: float, n_bursts: int) -> dict[str, float]:
    """Split a mid-family hit between snare, clap and perc from its spectrum and bursts."""
    perc = float(_ramp(peaky, 4.0, 7.0) * (1 - _ramp(body_share, 0.25, 0.45)))
    clap = float(_ramp(n_bursts, 2, 3) * (1 - _ramp(body_share, 0.35, 0.55))) * (1 - perc)
    return {"perc": perc, "clap": clap, "snare": max(0.0, 1 - perc - clap)}


@dataclass
class _LaneFeatures:
    """What lane_activations reads at each hit, computed once per track."""
    nov: dict[str, np.ndarray]  # onset novelty per family (kick, snare, hat)
    env: dict[str, np.ndarray]
    model: dict[str, np.ndarray] | None
    sr: int
    hop: int
    gap: float
    V: np.ndarray  # sqrt of the percussive mel spectrogram
    rise: np.ndarray  # how much each band rose over the last two frames
    kick_template: np.ndarray
    freqs: np.ndarray
    snare_ref: float

    @property
    def n(self) -> int:
        return next(iter(self.nov.values())).size


BODY_HZ, MID_HZ, TONAL_HZ, LOW_HZ = (100, 400), (400, 5000), (400, 3000), (30, 400)


def _band(freqs: np.ndarray, hz: tuple[float, float]) -> np.ndarray:
    return (freqs >= hz[0]) & (freqs < hz[1])


def _lane_features(ctx: AnalysisContext, nov: dict[str, np.ndarray], model, step_seconds: float | None) -> _LaneFeatures:
    mel, freqs = ctx.get("mel_percussive", signals.mel_percussive)
    V = np.sqrt(np.maximum(mel, 0.0))
    W = ctx.get("drum_nmf", decompose)["_W"]
    return _LaneFeatures(
        nov=nov, env=ctx.get("drum_envelopes", _envelopes), model=model, sr=ctx.sr, hop=ctx.hop,
        gap=step_seconds or 0.125, V=V, rise=np.maximum(0.0, V - np.concatenate([V[:, :2], V[:, :-2]], axis=1)),
        kick_template=W[:, list(TEMPLATES).index("kick")], freqs=freqs,
        snare_ref=float(np.percentile(nov["snare"], 99)) + 1e-12)


def _tom_share(F: _LaneFeatures, a: int, b: int, snare: float) -> float:
    """How much of a low hit is a tom: a kick's energy sits under ~90 Hz, a tom's higher up (its pitch)."""
    t = a + int(np.argmax(F.nov["kick"][a:b]))
    low = _band(F.freqs, LOW_HZ)
    lo = F.V[low, min(F.n - 1, t + 1)]  # a moment in, after a kick's pitch drop
    pitch = float(F.freqs[low][int(np.argmax(lo))])
    # ...unless a mid hit lands at the same time: then that pitch is most likely the snare's body.
    return float(_ramp(pitch, TOM_PITCH[0], TOM_PITCH[1]) * (1 - _ramp(snare / F.snare_ref, 0.15, 0.4)))


def _mid_shares(F: _LaneFeatures, a: int, b: int, i: int) -> dict[str, float]:
    """The mid hit's own spectrum (what rose here, less what the kick part explains) split into snare, clap, perc."""
    t = a + int(np.argmax(F.nov["snare"][a:b]))
    spec = np.maximum(F.rise[:, t] - F.kick_template * F.nov["kick"][t], 0.0)
    body, mid = spec[_band(F.freqs, BODY_HZ)].sum(), spec[_band(F.freqs, MID_HZ)].sum()
    body_share = float(body / (body + mid + 1e-12))
    tm = spec[_band(F.freqs, TONAL_HZ)]
    peaky = float(tm.max() / (tm.mean() + 1e-12)) if tm.size and tm.max() > 0 else 0.0
    return _mid_kind(body_share, peaky, _bursts(F.env["mid"], i, F.sr))


def _model_hit(F: _LaneFeatures, out: dict[str, np.ndarray], k: int, a: int, b: int, hat: float) -> None:
    """Put the drum model's hits at frame index k in place of the built-in kick, tom and hat lanes."""
    m = {lane: float(v[a:b].max()) for lane, v in F.model.items()}
    out["kick"][k], out["tom"][k], out["cymbal"][k] = m["kick"], m["tom"], m["cymbal"]
    if MODEL_OPEN_HATS:
        t = a + int(np.argmax(F.model["hat"][a:b]))
        ring = _ringing(F.env["air"], int(t * F.hop), F.sr, F.gap) if m["hat"] > 0 else 0.0
        out["hat"][k], out["open_hat"][k] = m["hat"] * (1 - ring), m["hat"] * ring
    else:
        # Our open-hat lane stays; the model's hi-hats replace the closed ones.
        out["hat"][k] = m["hat"] * (1 - _ramp(out["open_hat"][k] / (hat + 1e-12), 0.5, 0.9)) if hat > 0 else m["hat"]


def _hit(F: _LaneFeatures, out: dict[str, np.ndarray], k: int, f: int, slop: int) -> None:
    """Fill every lane at frame index k (analysis frame f)."""
    a, b = max(0, f - slop), min(F.n, f + slop + 1)
    s = {name: float(v[a:b].max()) for name, v in F.nov.items()}
    i = int(f * F.hop)
    if s["kick"] > 0:
        tom = _tom_share(F, a, b, s["snare"])
        out["kick"][k] = s["kick"] * (1 - tom)
        out["tom"][k] += s["kick"] * tom
    if s["snare"] > 0:
        for lane, share in _mid_shares(F, a, b, i).items():
            out[lane][k] += s["snare"] * share
    if s["hat"] > 0:
        ring = _ringing(F.env["air"], i, F.sr, F.gap)
        out["hat"][k] = s["hat"] * (1 - ring)
        out["open_hat"][k] = s["hat"] * ring
    if F.model:
        _model_hit(F, out, k, a, b, s["hat"])


def lane_activations(ctx: AnalysisContext, frames: np.ndarray, step_seconds: float | None = None,
                     slop: int = 2) -> dict[str, np.ndarray]:
    """Strength of each drum lane at each frame index in `frames` (any shape), allowing `slop` frames of timing error."""
    frames = np.asarray(frames, dtype=int)
    flat = frames.reshape(-1)
    model = ctx.get("drum_model", model_lanes)
    out = {lane: np.zeros(flat.size) for lane in LANES + (("cymbal",) if model else ())}
    nov = ctx.get("drum_onsets", onsets)
    if next(iter(nov.values())).size >= 3 and flat.size:
        F = _lane_features(ctx, nov, model, step_seconds)
        for k, f in enumerate(flat):
            _hit(F, out, k, f, slop)
    return {k: v.reshape(frames.shape) for k, v in out.items()}


def _full_rate_audio(ctx: AnalysisContext) -> tuple[np.ndarray, int]:
    """The original file at the drum model's rate when it is on disk (the analysis signal stops at 11 kHz)."""
    from . import adt

    path = ctx.card.source.local_path
    if path and Path(path).is_file():
        import librosa

        y, _ = librosa.load(path, sr=adt.SR, mono=True)
        return y, adt.SR
    return ctx.y, ctx.sr


def model_lanes(ctx: AnalysisContext) -> dict[str, np.ndarray] | None:
    """The drum model's hits per lane on the analysis frames, or None when this analysis didn't ask for it
    (adt.wanted), without tastr[drums], or offline on first use. Each hit is its confidence (0.5 to 1) times the level of that drum family's own part, so the
    model's lanes sit on the same scale as the built-in ones."""
    from . import adt

    if not adt.wanted(ctx.options):
        return None
    try:
        hits = adt.onsets(*_full_rate_audio(ctx))
    except Exception as e:  # e.g. no network for the first download
        log.warning("drum model unavailable, using the built-in lanes: %s", e)
        ctx.card.extras["drum_model_error"] = f"{type(e).__name__}: {e}"
        return None
    ctx.card.extras["drum_model"] = "adtof"  # re-analysis keeps using it (service.reanalyze_track)
    nov = ctx.get("drum_onsets", onsets)
    n = next(iter(nov.values())).size
    level = {fam: float(np.percentile(nov[part], 99.5)) or 1.0 for fam, part in (("low", "kick"), ("high", "hat"))}
    out = {}
    for lane, fam in (("kick", "low"), ("tom", "low"), ("hat", "high"), ("cymbal", "high")):
        x = np.zeros(n)
        t, conf = hits[lane]
        f = np.clip(np.round(t * ctx.sr / ctx.hop).astype(int), 0, n - 1)
        np.maximum.at(x, f, conf * level[fam])
        out[lane] = x
    return out


# --- The step grid: one bar's drum pattern from the lanes (the card's suggestions.drum_grid) ------------------
# The Rust engine makes the cards (engine-rs/src/stages/suggestions.rs); this copy is what the drum benchmarks
# (bench/drums.py) score, and tests/test_rust_engine.py checks the two agree.

STEPS_PER_BEAT = 4  # 16th notes
CORE_LANES = ("kick", "snare", "hat")  # always kept; a much quieter other lane is likely a misread drum


def _pattern(profile: np.ndarray) -> str:
    return "".join("x" if v >= 0.72 else "o" if v >= 0.5 else "." for v in profile)


def step_grid(ctx: AnalysisContext, n_frames: int) -> np.ndarray:
    """Frame index of each 16th note for every complete bar (consecutive downbeats)."""
    downbeats = np.asarray(ctx.card.core.downbeat_times, dtype=float)
    m = ctx.card.core.beats_per_bar or 4
    if downbeats.size < 2:
        return np.zeros((0, m * STEPS_PER_BEAT), dtype=int)
    beats = np.asarray(ctx.card.core.beat_times, dtype=float)
    rows = []
    for a, b in zip(downbeats[:-1], downbeats[1:]):
        in_bar = beats[(beats >= a - 1e-6) & (beats < b - 1e-6)]
        edges = np.append(in_bar, b) if in_bar.size == m else np.linspace(a, b, m + 1)
        times = [edges[i] + (edges[i + 1] - edges[i]) * s / STEPS_PER_BEAT for i in range(m) for s in range(STEPS_PER_BEAT)]
        rows.append(times)
    frames = np.round(np.asarray(rows) * ctx.sr / ctx.hop).astype(int)
    return np.clip(frames, 0, max(n_frames - 1, 0))


def _bar_energy(ctx: AnalysisContext, step_frames: np.ndarray) -> np.ndarray:
    n = step_frames.shape[0]
    series = ctx.card.series.get("energy")
    if series is not None and len(series.values) >= n:
        return np.asarray(series.values[:n], dtype=float)
    rms = ctx.get("rms", signals.rms)
    ends = np.append(step_frames[1:, 0], rms.size)
    return np.array([rms[a:max(b, a + 1)].mean() for a, b in zip(step_frames[:, 0], ends)])


def drum_grid(ctx: AnalysisContext, step_frames: np.ndarray) -> dict:
    energies = _bar_energy(ctx, step_frames)
    busy = energies >= np.median(energies)
    if busy.sum() < 2:
        busy = np.ones(len(energies), dtype=bool)
    bpm = ctx.card.core.bpm
    step_s = 15.0 / bpm if bpm else None
    acts = lane_activations(ctx, step_frames, step_seconds=step_s)
    # Noise floor: the same measure halfway between 16ths, where a played pattern has no hits.
    between = (step_frames + np.roll(step_frames, -1, axis=1)) // 2
    between[:, -1] = step_frames[:, -1] + (step_frames[:, -1] - step_frames[:, -2]) // 2
    floor_acts = lane_activations(ctx, between, step_seconds=step_s, slop=0)
    grid = {}
    for lane in acts:  # drums.LANES, plus cymbal with the drum model
        # sqrt compresses activation toward perceived loudness so a slightly weaker hit still reads as a hit
        steps = np.sqrt(acts[lane])
        profile = steps[busy].mean(axis=0)
        floor = float(np.sqrt(floor_acts[lane])[busy].mean())
        peak = float(profile.max())
        norm = profile / peak if peak > 0 else profile
        grid[lane] = {
            "pattern": _pattern(norm),
            "velocity": [round(float(v), 3) for v in norm],
            "detected": bool(peak > 0 and peak >= 3.0 * floor),
            "_steps": steps,
            "_peak": peak,
        }
    # A lane far quieter than the loudest one is most likely another drum misread.
    top = max(g["_peak"] for g in grid.values())
    for lane, g in grid.items():
        if lane not in CORE_LANES and g["_peak"] < 0.2 * top:
            g["detected"] = False
    # A mid lane that is a faint copy of the kick (same steps, much quieter) is the kick leaking through.
    kick = grid["kick"]
    for lane in ("snare", "clap", "perc"):
        g = grid[lane]
        if g["detected"] and kick["_peak"] > 0 and g["_peak"] < 0.35 * kick["_peak"]:
            a, b = np.asarray(g["velocity"]), np.asarray(kick["velocity"])
            if a.std() > 0 and b.std() > 0 and float(np.corrcoef(a, b)[0, 1]) > 0.9:
                g["detected"] = False
    return grid
