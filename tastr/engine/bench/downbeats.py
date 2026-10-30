"""Downbeat phase benchmark: does bar 1 land on the right beat?

Scores only the choice of which beat starts each bar (core's `_downbeat_phase`),
given the labeled beats, so beat-tracking errors don't blur it. Uses the
"Beat This!" release (https://zenodo.org/records/13922116), which ships
128-band log-mel spectrograms (the same bands as `signals.log_mel`, at 50 fps) and
beat/downbeat labels for public datasets whose audio isn't redistributable.

    python -m tastr.engine.bench.downbeats ~/data/beat_this --dataset gtzan --limit 200

The folder needs `<dataset>.npz` (unzipped from `<dataset>.zip`), or the slim
`<dataset>-originals.npz` from `write_originals`, and `beat_this_annotations-1.0/`. Only tracks labeled in 4/4 are scored.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np

FPS = 50  # Beat This! spectrogram frame rate
# Some releases also ship pitch-shifted and time-stretched copies ("<name>/track_ps-3", "<name>/track_ts12").
AUGMENTED = re.compile(r"_(ps|ts)-?\d+$")


def original_keys(specs) -> list[str]:
    """Spectrogram keys of the original recordings, skipping Beat This! augmentations."""
    return sorted(k for k in specs.keys() if not AUGMENTED.search(k))


def spec_file(root: Path, dataset: str) -> Path:
    """`<dataset>-originals.npz` when it's there (written by `write_originals`), else `<dataset>.npz`."""
    slim = root / f"{dataset}-originals.npz"
    return slim if slim.exists() else root / f"{dataset}.npz"


def write_originals(root: str | Path, dataset: str) -> Path:
    """Save only the original recordings' spectrograms as `<dataset>-originals.npz` (hjdb: 153 MB of 3.2 GB).

    The benchmarks skip the augmented copies, so this is all they read; it's what goes to the cloud bucket.
    """
    root = Path(root).expanduser()
    specs = np.load(root / f"{dataset}.npz")
    out = root / f"{dataset}-originals.npz"
    np.savez(out, **{k: specs[k] for k in original_keys(specs)})
    return out


def mel_freqs() -> np.ndarray:
    import librosa

    return librosa.mel_frequencies(n_mels=130, fmin=30, fmax=11000)[1:-1]


def load(root: str | Path, dataset: str = "gtzan", limit: int | None = None, seed: int = 0):
    """[(name, log_mel bands x frames, beat times, beat positions 1-4)], shuffled with a fixed seed."""
    root = Path(root).expanduser()
    specs = np.load(spec_file(root, dataset))
    ann = root / "beat_this_annotations-1.0" / dataset / "annotations" / "beats"
    items = []
    for key in original_keys(specs):
        name = key.split("/")[0]
        f = ann / f"{name}.beats"
        if not f.exists():
            continue
        a = np.loadtxt(f, ndmin=2)
        if a.shape[1] < 2:
            continue
        pos = a[:, 1].astype(int)
        if pos.max() != 4 or (pos == 1).sum() < 3:
            continue
        items.append((name, specs[key].astype(np.float32).T, a[:, 0], pos))
    np.random.default_rng(seed).shuffle(items)
    return items[:limit] if limit else items


def run(items) -> dict:
    from ..stages.core import _downbeat_phase

    freqs = mel_freqs()
    right = half = 0
    by_group: Counter = Counter()
    n_group: Counter = Counter()
    for name, log_mel, beats, pos in items:
        p = _downbeat_phase(np.round(beats * FPS).astype(int), log_mel, freqs)
        ok = (pos[p::4] == 1).mean() > 0.5
        right += ok
        half += (pos[(p + 2) % 4::4] == 1).mean() > 0.5
        group = name.split("_")[1] if name.count("_") >= 2 else "all"
        by_group[group] += ok
        n_group[group] += 1
    n = max(1, len(items))
    return {"tracks": len(items), "right": round(right / n, 3), "half_bar_off": round(half / n, 3),
            "by_group": {g: round(by_group[g] / n_group[g], 3) for g in sorted(n_group)}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tastr.engine.bench.downbeats", description=__doc__.split("\n\n")[0])
    ap.add_argument("path", help="folder with <dataset>.npz and beat_this_annotations-1.0/")
    ap.add_argument("--dataset", default="gtzan")
    ap.add_argument("--limit", type=int, help="first N tracks (after a fixed shuffle)")
    args = ap.parse_args(argv)
    print(format_report(run(load(args.path, args.dataset, args.limit))))
    return 0


def format_report(r: dict) -> str:
    lines = [f"tracks {r['tracks']}: bar 1 right {r['right']:.3f}, half a bar off {r['half_bar_off']:.3f}"]
    lines += [f"  {g:10s} {v:.2f}" for g, v in r["by_group"].items()]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
