"""Section boundary benchmark on Harmonix Set mel spectrograms, for tuning how many sections the stage asks for.

Harmonix doesn't distribute audio, only mel spectrograms (`scripts/fetch_harmonix_mels.py` copies
them to the bucket). This rebuilds the sections stage's per-bar features from them (MFCC from the
log-mel, loudness from the summed mel power, chroma folded from the mel bands, which is coarser
than the stage's CQT chroma), takes the bar grid from the labeled downbeats so beat tracking
errors don't blur the result, and runs the stage's own clustering and phrase snapping for a grid
of settings: seconds per section, phrase length in bars, and the cap on the section count.

Tracks are split into two halves at random (seed 0): pick settings on "tune", read the result on "check".

    python -m tastr.engine.bench.sections_spec ~/data/harmonix --limit 100

The folder needs `melspecs.npz`, `melspecs-info.json` and `annotations.json` as written by the fetch
script (or the repo's `dataset/` folder with segments and beats_and_downbeats).
"""

from __future__ import annotations

import argparse
import json
import sys
from itertools import product
from pathlib import Path

import numpy as np

from .metrics import boundary_scores

SECONDS = (16, 18, 20, 22, 24, 28, 32)
PHRASES = (1, 2, 4)
CAPS = (12, 16, 20)
SEED = 0
CHROMA_BAND = (55.0, 2100.0)  # Hz; mel bands outside this are too wide or too high to fold into pitch classes


def _info(folder: Path) -> dict:
    info = json.loads((folder / "melspecs-info.json").read_text()) if (folder / "melspecs-info.json").exists() else {}
    info = {k.lower(): v for k, v in (info or {}).items()}  # Harmonix writes SR, HOP_LENGTH, MEL_FMIN, MEL_FMAX
    sr = float(info.get("sr") or 22050)
    return {"sr": sr, "hop": int(info.get("hop_length") or 1024),  # Harmonix: 1024
            "fmin": float(info.get("mel_fmin") or info.get("fmin") or 0.0),
            "fmax": float(info.get("mel_fmax") or info.get("fmax") or sr / 2)}


def _annotations(ds: Path, sid: str) -> tuple[list[float], list[float], float | None] | None:
    beats_file, seg_file = ds / "beats_and_downbeats" / f"{sid}.txt", ds / "segments" / f"{sid}.txt"
    if not beats_file.exists() or not seg_file.exists():
        return None
    downbeats = [float(c[0]) for c in (line.split() for line in beats_file.read_text().splitlines())
                 if len(c) >= 2 and c[1] in ("1", "1.0")]
    bounds, end = [], None
    for line in seg_file.read_text().splitlines():
        cols = line.split(maxsplit=1)
        if len(cols) == 2:
            if cols[1].strip().lower() == "end":
                end = float(cols[0])
            else:
                bounds.append(float(cols[0]))
    return downbeats, bounds, end


def load(folder: str | Path, dataset: str = "harmonix", limit: int | None = None):
    """[(id, mel, downbeats, boundaries, split)], `limit` tracks drawn at random (seed 0) so a subset spans the set."""
    folder = Path(folder)
    ds = folder / "dataset"
    bundle = json.loads((folder / "annotations.json").read_text()) if (folder / "annotations.json").exists() else None
    mels = np.load(folder / "melspecs.npz")
    ids = sorted(mels.files)
    order = np.random.default_rng(SEED).permutation(len(ids))
    items = []
    for rank, i in enumerate(order):
        if limit and len(items) >= limit:
            break
        if bundle is not None:
            b = bundle.get(ids[i])
            ann = (b["downbeats"], b["boundaries"], b["end"]) if b else None
        else:
            ann = _annotations(ds, ids[i])
        if ann is None or len(ann[0]) < 8:
            continue
        items.append((ids[i], mels[ids[i]], ann[0], ann[1], "tune" if rank % 2 == 0 else "check"))
    return items, _info(folder)


class _Ctx:
    """Just enough of an AnalysisContext for the stage's bar helpers: precomputed signals and the frame grid."""

    def __init__(self, sr: float, hop: int, signals: dict):
        self.sr, self.hop, self._signals = sr, hop, signals

    def get(self, name, fn=None):
        return self._signals[name]


def signals_from_mel(mel: np.ndarray, info: dict) -> dict:
    """chroma (12), mfcc (13) and rms per frame from a bands x frames mel spectrogram (power, magnitude or dB)."""
    import librosa

    m = mel.astype(np.float64)
    if m.shape[0] > m.shape[1]:  # stored frames x bands
        m = m.T
    if m.min() < 0:  # already in dB
        db = m
        power = librosa.db_to_power(m)
    else:
        power = m
        db = librosa.power_to_db(m + 1e-10)
    freqs = librosa.mel_frequencies(n_mels=m.shape[0] + 2, fmin=info["fmin"], fmax=info["fmax"])[1:-1]
    keep = (freqs >= CHROMA_BAND[0]) & (freqs <= CHROMA_BAND[1])
    pc = np.round(12 * np.log2(freqs[keep] / 440.0) + 9).astype(int) % 12  # pitch class, C = 0
    chroma = np.zeros((12, m.shape[1]))
    np.add.at(chroma, pc, np.sqrt(power[keep]))
    chroma /= chroma.max(axis=0, keepdims=True) + 1e-9
    return {"chroma": chroma, "mfcc": librosa.feature.mfcc(S=db, n_mfcc=13),
            "rms": np.sqrt(power.mean(axis=0))}


def run(items_info) -> dict:
    from ..stages import sections

    items, info = items_info
    configs = list(product(SECONDS, PHRASES, CAPS))
    scores = {c: {"tune": [], "check": []} for c in configs}
    rows = []
    for sid, mel, downbeats, bounds, split in items:
        sig = signals_from_mel(mel, info)
        n_frames = sig["mfcc"].shape[1]
        duration = n_frames * info["hop"] / info["sr"]
        ctx = _Ctx(info["sr"], info["hop"], sig)
        bar_times = sections._bar_times(downbeats, duration)
        n_bars = len(bar_times)
        if n_bars < sections.MIN_BARS_FOR_SPLIT:
            continue
        z = sections._bar_vectors(ctx, bar_times, duration)
        row = {"id": sid, "split": split, "duration": round(duration, 1), "n_ref": len(bounds)}
        for c in configs:
            seconds, phrase, cap = c
            starts = sections._segment(z, duration, n_bars, seconds=seconds, phrase=phrase, cap=cap)
            est = [0.0] + [float(bar_times[s]) for s in starts[1:]]
            s = boundary_scores(est, bounds, duration)
            scores[c][split].append((s["boundary_f_0.5"], s["boundary_f_3"], s["n_sections_est"] - s["n_sections_ref"]))
            if c == (sections.SECONDS_PER_SECTION, sections.PHRASE_BARS, sections.MAX_SECTIONS):
                row |= {"f3": round(s["boundary_f_3"], 3), "n_est": s["n_sections_est"]}
        rows.append(row)

    def summary(vals):
        if not vals:
            return None
        a = np.asarray(vals)
        return {"f_0.5": round(float(a[:, 0].mean()), 3), "f_3": round(float(a[:, 1].mean()), 3),
                "count_bias": round(float(a[:, 2].mean()), 2), "count_mae": round(float(np.abs(a[:, 2]).mean()), 2)}

    grid = [{"seconds": c[0], "phrase": c[1], "cap": c[2], "tune": summary(v["tune"]), "check": summary(v["check"])}
            for c, v in scores.items()]
    best = max((g for g in grid if g["tune"]), key=lambda g: g["tune"]["f_3"], default=None)
    current = next((g for g in grid if (g["seconds"], g["phrase"], g["cap"]) ==
                    (sections.SECONDS_PER_SECTION, sections.PHRASE_BARS, sections.MAX_SECTIONS)), None)
    return {"tracks": len(rows), "tune_tracks": sum(r["split"] == "tune" for r in rows), "info": info,
            "current": current, "best_on_tune": best, "grid": grid, "rows": rows}


def format_report(r: dict) -> str:
    def line(g):
        t, c = g["tune"] or {}, g["check"] or {}
        return (f"{g['seconds']:>5g} s  {g['phrase']} bars  cap {g['cap']:>2}   tune F3 {t.get('f_3', 0):.3f} F0.5 "
                f"{t.get('f_0.5', 0):.3f}   check F3 {c.get('f_3', 0):.3f} F0.5 {c.get('f_0.5', 0):.3f}  "
                f"count bias {c.get('count_bias', 0):+.1f}")

    out = [f"Harmonix sections on mel spectrograms: {r['tracks']} tracks ({r['tune_tracks']} tune), labeled downbeats",
           "current:  " + line(r["current"]) if r.get("current") else "current: n/a",
           "best on tune:  " + line(r["best_on_tune"]) if r.get("best_on_tune") else "best: n/a", ""]
    out += [line(g) for g in sorted(r["grid"], key=lambda g: -(g["tune"] or {}).get("f_3", 0))[:15]]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", help="folder with melspecs.npz, melspecs-info.json and annotations.json")
    ap.add_argument("--limit", type=int, help="N tracks drawn at random")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    report = run(load(args.path, limit=args.limit))
    print(format_report(report))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
