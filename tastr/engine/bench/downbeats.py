"""Downbeat phase benchmark: does bar 1 land on the right beat?

Scores only the choice of which beat starts each bar (core's `_downbeat_phase`),
given the labeled beats, so beat-tracking errors don't blur it. Uses the
"Beat This!" release (https://zenodo.org/records/13922116), which ships
128-band log-mel spectrograms (the same bands as `signals.log_mel`, at 50 fps) and
beat/downbeat labels for public datasets whose audio isn't redistributable.

    python -m tastr.engine.bench.downbeats ~/data/beat_this --dataset gtzan --limit 200

The folder needs `<dataset>.npz` (unzipped from `<dataset>.zip`) and `beat_this_annotations-1.0/`, or
the slim `<dataset>-originals.npz` and `<dataset>-annotations.json` from `write_originals`. Only tracks
labeled in 4/4 are scored.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np

FPS = 50  # Beat This! spectrogram frame rate
# Some releases also ship pitch-shifted and time-stretched copies ("<name>/track_ps-3", "<name>/track_ts12").
AUGMENTED = re.compile(r"_(ps|ts)-?\d+$")
# Releases whose track names carry a group as their second word (gtzan_blues_00000, rwc_classical_CD1_01).
GROUPED = ("gtzan", "rwc")


def original_keys(specs) -> list[str]:
    """Spectrogram keys of the original recordings, skipping Beat This! augmentations."""
    return sorted(k for k in specs.keys() if not AUGMENTED.search(k))


def spec_file(root: Path, dataset: str) -> Path:
    """`<dataset>-originals.npz` when it's there (written by `write_originals`), else `<dataset>.npz`."""
    slim = root / f"{dataset}-originals.npz"
    return slim if slim.exists() else root / f"{dataset}.npz"


def annotations_file(root: Path, dataset: str) -> Path:
    return root / f"{dataset}-annotations.json"


def _release_annotations(root: Path, dataset: str) -> tuple[dict[str, np.ndarray], dict[str, str]]:
    ann = root / "beat_this_annotations-1.0" / dataset
    beats = {f.stem: np.loadtxt(f, ndmin=2) for f in sorted((ann / "annotations" / "beats").glob("*.beats"))}
    split_file = ann / "single.split"
    split = dict(line.split("\t")[:2] for line in split_file.read_text().splitlines() if "\t" in line) \
        if split_file.exists() else {}
    return beats, {n: s.strip() for n, s in split.items()}


def read_annotations(root: Path, dataset: str) -> tuple[dict[str, np.ndarray], dict[str, str]]:
    """({track name: beat rows (time, bar position)}, {track name: split}).

    From `<dataset>-annotations.json` when it's there: one read instead of one per track, which on the
    cloud bucket mount is most of a run's loading time. Else from the release's `.beats` files.
    """
    bundled = annotations_file(root, dataset)
    if not bundled.exists():
        return _release_annotations(root, dataset)
    data = json.loads(bundled.read_text())
    return {n: np.array(b, dtype=float, ndmin=2) for n, b in data["beats"].items()}, data["split"]


def write_originals(root: str | Path, dataset: str) -> tuple[Path, Path]:
    """Save the original recordings' spectrograms as `<dataset>-originals.npz` (hjdb: 153 MB of 3.2 GB)
    and their beats and splits as `<dataset>-annotations.json`.

    The benchmarks skip the augmented copies, so these two files are all they read; they're what goes
    to the cloud bucket.
    """
    root = Path(root).expanduser()
    specs = np.load(root / f"{dataset}.npz")
    keys = original_keys(specs)
    out = root / f"{dataset}-originals.npz"
    np.savez(out, **{k: specs[k] for k in keys})
    beats, split = _release_annotations(root, dataset)
    names = sorted({k.split("/")[0] for k in keys})
    annotations_file(root, dataset).write_text(json.dumps({
        "beats": {n: beats[n].tolist() for n in names if n in beats},
        "split": {n: split[n] for n in names if n in split}}))
    return out, annotations_file(root, dataset)


def mel_freqs() -> np.ndarray:
    import librosa

    return librosa.mel_frequencies(n_mels=130, fmin=30, fmax=11000)[1:-1]


def bar_length(pos: np.ndarray) -> int | None:
    """Beats per bar when at least 90% of the labeled bars have the same length, else None (meter changes)."""
    ones = np.flatnonzero(pos == 1)
    lengths = [int(pos[i - 1]) for i in ones[1:]]
    if len(lengths) < 2:
        return None
    m, count = Counter(lengths).most_common(1)[0]
    return m if count >= 0.9 * len(lengths) else None


def load(root: str | Path, dataset: str = "gtzan", limit: int | None = None, seed: int = 0, meters=None):
    """[(name, log_mel bands x frames, beat times, beat positions 1-4)], shuffled with a fixed seed.

    Only 4/4 tracks by default; `meters` (e.g. (3, 4)) picks tracks whose bars are steadily that long instead.
    """
    root = Path(root).expanduser()
    specs = np.load(spec_file(root, dataset))
    beats, _ = read_annotations(root, dataset)
    picked = []
    for key in original_keys(specs):
        name = key.split("/")[0]
        a = beats.get(name)
        if a is None or a.size == 0 or a.shape[1] < 2:
            continue
        pos = a[:, 1].astype(int)
        if meters is None and (pos.max() != 4 or (pos == 1).sum() < 3):
            continue
        if meters is not None and bar_length(pos) not in meters:
            continue
        picked.append((name, key, a[:, 0], pos))
    np.random.default_rng(seed).shuffle(picked)
    # Spectrograms last, so --limit reads only the tracks it scores.
    return [(name, specs[key].astype(np.float32).T, t, pos) for name, key, t, pos in picked[:limit or None]]


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
        parts = name.split("_")
        group = parts[1] if parts[0] in GROUPED and len(parts) >= 3 else "all"
        by_group[group] += ok
        n_group[group] += 1
    n = max(1, len(items))
    return {"tracks": len(items), "right": round(right / n, 3), "half_bar_off": round(half / n, 3),
            "by_group": {g: round(by_group[g] / n_group[g], 3) for g in sorted(n_group)} if len(n_group) > 1 else {}}


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
