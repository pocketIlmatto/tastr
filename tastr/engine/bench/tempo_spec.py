"""Tempo benchmark on Beat This! spectrograms, for labeled sets whose audio isn't public.

Runs core's tempo logic (`_tempo_and_beats`) on onset envelopes rebuilt from the
128-band log-mel spectrograms in the "Beat This!" release
(https://zenodo.org/records/13922116). The reference tempo is 60 / the median
labeled inter-beat interval. Only the original recordings are scored, not the
pitch-shifted / time-stretched copies.

    python -m tastr.engine.bench.tempo_spec ~/data/beat_this --dataset hjdb

The folder needs `<dataset>.npz` (or the slim `<dataset>-originals.npz`) and
`beat_this_annotations-1.0/` as for `bench.downbeats`. On GiantSteps audio this adapter gives the same BPM (within 4%)
as the normal audio path on 39 of 43 tracks; see docs/bench.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

from .downbeats import FPS, mel_freqs, original_keys, spec_file
from .metrics import TEMPO_TOLERANCE, tempo_scores

SR = 22050
HOP = 512  # core's frame grid; the envelopes are resampled onto it (the tempo logic depends on frame rate)
# est / ref ratios named in the report, beyond the double / half that Acc2 allows.
RATIOS = {"x2": 2.0, "/2": 0.5, "x3/2": 1.5, "x2/3": 2 / 3, "x4/3": 4 / 3, "x3/4": 0.75, "x5/4": 1.25,
          "x4/5": 0.8, "x3": 3.0, "/3": 1 / 3}


def envelopes(log_mel: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(onset_env, low_flux) on core's grid from a 128 x frames Beat This! log-mel spectrogram.

    Mirrors signals.onset_envelope / low_band_flux: HPSS on mel power (undoing
    log1p(1000 x)), percussive part to dB, positive flux averaged over bands
    (all bands for onsets, below 150 Hz for the kick).
    """
    import librosa

    power = (np.expm1(log_mel.astype(np.float64)) / 1000) ** 2
    _, perc = librosa.decompose.hpss(power, kernel_size=36)  # core's 31 frames at 43 fps
    db = librosa.power_to_db(perc + 1e-10)
    onset = librosa.onset.onset_strength(S=db, sr=SR, hop_length=SR // FPS)
    low = db[mel_freqs() < 150]
    low_flux = np.maximum(0.0, np.diff(low, axis=1, prepend=low[:, :1])).mean(axis=0)
    t_src = np.arange(onset.size) / FPS
    t_dst = np.arange(int(t_src[-1] * SR / HOP) + 1) * HOP / SR
    return np.interp(t_dst, t_src, onset), np.interp(t_dst, t_src, low_flux)


def tempo(log_mel: np.ndarray) -> float | None:
    from ..pipeline import AnalysisContext
    from ..stages.core import _tempo_and_beats

    onset, low_flux = envelopes(log_mel)
    ctx = AnalysisContext(y=np.zeros(0, dtype=np.float32), sr=SR, card=None, hop=HOP)
    bpm, *_ = _tempo_and_beats(ctx, onset, low_flux, onset.size)
    return bpm


def ratio_name(est: float | None, ref: float) -> str:
    if not est:
        return "none"
    if tempo_scores(est, ref)["acc1"]:
        return "ok"
    for name, f in RATIOS.items():
        if abs(est - f * ref) <= TEMPO_TOLERANCE * f * ref:
            return name
    return "other"


def load(root: str | Path, dataset: str = "hjdb", limit: int | None = None):
    """[(name, log_mel bands x frames, reference bpm, split)] for the original recordings."""
    root = Path(root).expanduser()
    specs = np.load(spec_file(root, dataset))
    ann = root / "beat_this_annotations-1.0" / dataset
    split_file = ann / "single.split"
    split = dict(line.split("\t")[:2] for line in split_file.read_text().splitlines() if "\t" in line) \
        if split_file.exists() else {}
    items = []
    for key in original_keys(specs):
        name = key.split("/")[0]
        f = ann / "annotations" / "beats" / f"{name}.beats"
        if not f.exists():
            continue
        beats = np.loadtxt(f, ndmin=2)[:, 0]
        if beats.size < 2:
            continue
        ref = 60.0 / float(np.median(np.diff(beats)))
        items.append((name, specs[key].astype(np.float32).T, ref, split.get(name, "all").strip()))
    return items[:limit] if limit else items


def run(items) -> dict:
    rows = []
    for name, log_mel, ref, split in items:
        est = tempo(log_mel)
        rows.append({"name": name, "split": split, "ref": round(ref, 2), "est": est and round(est, 2),
                     "error": ratio_name(est, ref), **tempo_scores(est, ref)})
    summary = {}
    for split in sorted({r["split"] for r in rows}) + ["all"]:
        rs = [r for r in rows if split in ("all", r["split"])]
        n = max(1, len(rs))
        summary[split] = {"tracks": len(rs), "acc1": round(sum(r["acc1"] for r in rs) / n, 3),
                          "acc2": round(sum(r["acc2"] for r in rs) / n, 3),
                          "errors": dict(Counter(r["error"] for r in rs if r["error"] != "ok").most_common())}
    return {"summary": summary, "tracks": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tastr.engine.bench.tempo_spec", description=__doc__.split("\n\n")[0])
    ap.add_argument("path", help="folder with <dataset>.npz and beat_this_annotations-1.0/")
    ap.add_argument("--dataset", default="hjdb")
    ap.add_argument("--limit", type=int, help="first N tracks")
    ap.add_argument("--out", help="write per-track results as JSON here")
    args = ap.parse_args(argv)
    r = run(load(args.path, args.dataset, args.limit))
    print(format_report(r))
    if args.out:
        Path(args.out).write_text(json.dumps(r, indent=1))
    return 0


def format_report(r: dict) -> str:
    return "\n".join(f"{split:6s} {s['tracks']:4d} tracks  Acc1 {s['acc1']:.3f}  Acc2 {s['acc2']:.3f}  errors {s['errors']}"
                     for split, s in r["summary"].items())


if __name__ == "__main__":
    sys.exit(main())
