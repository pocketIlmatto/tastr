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

Activations are non-negative and comparable within a lane across a track.
Design notes, what was tried and dropped, and known weaknesses: docs/drums.md.
"""

from __future__ import annotations

import numpy as np

from . import signals
from .pipeline import AnalysisContext

LANES = ("kick", "snare", "clap", "hat", "open_hat", "tom", "perc")

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


def lane_activations(ctx: AnalysisContext, frames: np.ndarray, step_seconds: float | None = None,
                     slop: int = 2) -> dict[str, np.ndarray]:
    """Strength of each drum lane at each frame index in `frames` (any shape), allowing `slop` frames of timing error."""
    frames = np.asarray(frames, dtype=int)
    flat = frames.reshape(-1)
    out = {lane: np.zeros(flat.size) for lane in LANES}
    nov = ctx.get("drum_onsets", onsets)
    n = next(iter(nov.values())).size
    if n < 3 or flat.size == 0:
        return {k: v.reshape(frames.shape) for k, v in out.items()}
    env = ctx.get("drum_envelopes", _envelopes)
    sr, hop = ctx.sr, ctx.hop
    gap = step_seconds or 0.125

    mel, freqs = ctx.get("mel_percussive", signals.mel_percussive)
    V = np.sqrt(np.maximum(mel, 0.0))
    rise = np.maximum(0.0, V - np.concatenate([V[:, :2], V[:, :-2]], axis=1))
    W = ctx.get("drum_nmf", decompose)["_W"]
    names = list(TEMPLATES)
    body, mid = (freqs >= 100) & (freqs < 400), (freqs >= 400) & (freqs < 5000)
    tonal = (freqs >= 400) & (freqs < 3000)
    low = (freqs >= 30) & (freqs < 400)
    snare_ref = float(np.percentile(nov["snare"], 99)) + 1e-12

    for k, f in enumerate(flat):
        a, b = max(0, f - slop), min(n, f + slop + 1)
        s = {name: float(v[a:b].max()) for name, v in nov.items()}
        i = int(f * hop)
        if s["kick"] > 0:
            # Low hits: a kick's energy sits under ~90 Hz, a tom's higher up (its pitch).
            t = a + int(np.argmax(nov["kick"][a:b]))
            lo = V[low, min(n - 1, t + 1)]  # a moment in, after a kick's pitch drop
            pitch = float(freqs[low][int(np.argmax(lo))])
            # ...unless a mid hit lands at the same time: then that pitch is most likely the snare's body.
            tom = float(_ramp(pitch, TOM_PITCH[0], TOM_PITCH[1]) * (1 - _ramp(s["snare"] / snare_ref, 0.15, 0.4)))
            out["kick"][k] = s["kick"] * (1 - tom)
            out["tom"][k] += s["kick"] * tom
        if s["snare"] > 0:
            # The mid hit's own spectrum: what rose here, less what the kick part explains.
            t = a + int(np.argmax(nov["snare"][a:b]))
            spec = rise[:, t] - W[:, names.index("kick")] * nov["kick"][t]
            spec = np.maximum(spec, 0.0)
            body_share = float(spec[body].sum() / (spec[body].sum() + spec[mid].sum() + 1e-12))
            tm = spec[tonal]
            peaky = float(tm.max() / (tm.mean() + 1e-12)) if tm.size and tm.max() > 0 else 0.0
            n_bursts = _bursts(env["mid"], i, sr)
            w = _mid_kind(body_share, peaky, n_bursts)
            for lane, share in w.items():
                out[lane][k] += s["snare"] * share
        if s["hat"] > 0:
            ring = _ringing(env["air"], i, sr, gap)
            out["hat"][k] = s["hat"] * (1 - ring)
            out["open_hat"][k] = s["hat"] * ring
    return {k: v.reshape(frames.shape) for k, v in out.items()}
