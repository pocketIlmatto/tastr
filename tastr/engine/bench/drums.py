"""Drum lane benchmark: how well tastr.engine.drums finds each kind of hit.

Two suites:

- `synth`: generated loops with known 16-step patterns for every lane (kick,
  snare, clap, hat, open hat, tom, perc), scored step by step on the detected
  groove the suggestions stage builds. Exact labels, but a synthetic kit.
- `idmt`: IDMT-SMT-Drums (real, sampled and techno kits; kick, snare and hi-hat
  onsets only), scored as onsets within 50 ms. Fetch it from
  https://zenodo.org/records/7544164 and unzip; pass the folder that holds
  `audio/` and `annotation_xml/`.

    python -m tastr.engine.bench.drums synth
    python -m tastr.engine.bench.drums idmt ~/data/IDMT-SMT-DRUMS-V2 --limit 20

Iterate on a `--limit` subset; run the full set when a change is worth keeping.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from .. import drums
from ..card import Source, TrackCard
from ..pipeline import SR, AnalysisContext
from .synth import DRUM_PATTERNS, make_drum_track

# IDMT instrument -> the lanes that count as finding it.
IDMT_LANES = {"KD": ("kick",), "SD": ("snare", "clap"), "HH": ("hat", "open_hat")}


def _prf(tp: int, fp: int, fn: int) -> dict:
    tp, fp, fn = int(tp), int(fp), int(fn)
    return {"f": round(2 * tp / max(1, 2 * tp + fp + fn), 3), "p": round(tp / max(1, tp + fp), 3),
            "r": round(tp / max(1, tp + fn), 3), "n": tp + fn}


def _synth_ctx(y: np.ndarray, bpm: float) -> AnalysisContext:
    card = TrackCard(source=Source(kind="file", uri="synth"))
    card.duration = len(y) / SR
    beat = 60.0 / bpm
    card.core.bpm = bpm
    card.core.beat_times = [i * beat for i in range(int(card.duration / beat))]
    card.core.downbeat_times = card.core.beat_times[::4]
    return AnalysisContext(y=y, sr=SR, card=card)


def run_synth(bpms=(100, 128, 174)) -> dict:
    """Step-level precision/recall per lane over every pattern and tempo ("x" and "o" both count as a hit)."""
    from ..stages.suggestions import _drum_grid, _step_grid

    counts = {lane: [0, 0, 0] for lane in drums.LANES}
    rows = []
    for name, pats in DRUM_PATTERNS.items():
        for bpm in bpms:
            y, _ = make_drum_track(pats, bpm=bpm)
            ctx = _synth_ctx(y, bpm)
            grid = _drum_grid(ctx, _step_grid(ctx, 1 + len(y) // ctx.hop))
            got_all = {}
            for lane in drums.LANES:
                want = pats.get(lane, "." * 16)
                got = grid[lane]["pattern"] if grid[lane]["detected"] else "." * 16
                got_all[lane] = got
                c = counts[lane]
                for g, w in zip(got, want):
                    c[0] += g != "." and w == "x"
                    c[1] += g != "." and w != "x"
                    c[2] += g == "." and w == "x"
            rows.append({"pattern": name, "bpm": bpm, "got": got_all, "want": pats})
    return {"summary": {lane: _prf(*c) for lane, c in counts.items()}, "rows": rows}


def _peaks(x: np.ndarray, rel: float = 0.4, wait: int = 3) -> np.ndarray:
    """Frames of local maxima above `rel` x the track's 99.5th percentile, at least `wait` frames apart."""
    thr = rel * np.percentile(x, 99.5)
    keep: list[int] = []
    for i in range(1, len(x) - 1):
        if x[i] < thr or x[i] < x[i - 1] or x[i] <= x[i + 1]:
            continue
        if keep and i - keep[-1] < wait:
            if x[i] > x[keep[-1]]:
                keep[-1] = i
        else:
            keep.append(i)
    return np.asarray(keep, dtype=int)


def _match(est, ref, tol: float = 0.05) -> tuple[int, int, int]:
    used: set[int] = set()
    tp = 0
    for e in est:
        j = next((j for j, r in enumerate(ref) if j not in used and abs(r - e) <= tol), None)
        if j is not None:
            used.add(j)
            tp += 1
    return tp, len(est) - tp, len(ref) - tp


def idmt_items(root: str | Path) -> list[tuple[Path, dict[str, list[float]]]]:
    """(mix audio path, {"KD"|"SD"|"HH": onset seconds}) for every annotated mix with audio present."""
    root = Path(root).expanduser()
    out = []
    for fx in sorted((root / "annotation_xml").glob("*#MIX.xml")):
        r = ET.parse(fx).getroot()
        wav = root / "audio" / r.find("globalParameter/audioFileName").text
        if not wav.exists():
            continue
        ref: dict[str, list[float]] = {k: [] for k in IDMT_LANES}
        for ev in r.iter("event"):
            inst = ev.find("instrument").text
            if inst in ref:
                ref[inst].append(float(ev.find("onsetSec").text))
        out.append((wav, ref))
    return out


def run_idmt(root: str | Path, limit: int | None = None) -> dict:
    """Onset precision/recall per instrument, plus how often the extra lanes (tom, perc) fire on kits that have none."""
    import librosa

    counts = {k: np.zeros(3, dtype=int) for k in IDMT_LANES}
    extra = n_ref = 0
    items = idmt_items(root)[:limit]
    for wav, ref in items:
        y, _ = librosa.load(str(wav), sr=SR, mono=True)
        ctx = AnalysisContext(y=y, sr=SR, card=TrackCard(source=Source(kind="file", uri=str(wav))))
        acts = drums.lane_activations(ctx, np.arange(1 + len(y) // ctx.hop), slop=0)
        lanes = {k: sum(acts[lane] for lane in v) for k, v in IDMT_LANES.items()}
        top = max(np.percentile(v, 99.5) for v in lanes.values())
        for lane in ("tom", "perc"):
            # Mirrors the groove's rule: a lane far quieter than the loudest one is not shown.
            if np.percentile(acts[lane], 99.5) > 0.2 * top:
                extra += len(_peaks(acts[lane]))
        n_ref += sum(len(v) for v in ref.values())
        for k, x in lanes.items():
            counts[k] += _match(_peaks(x) * ctx.hop / SR, ref[k])
    return {"tracks": len(items), "summary": {k: _prf(*c) for k, c in counts.items()},
            "extra_onsets_per_ref": round(extra / max(1, n_ref), 3)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tastr.engine.bench.drums", description=__doc__.split("\n\n")[0])
    ap.add_argument("suite", choices=["synth", "idmt"])
    ap.add_argument("path", nargs="?", help="IDMT-SMT-Drums folder (for idmt)")
    ap.add_argument("--limit", type=int, help="first N tracks only")
    ap.add_argument("--out", help="write the full report as JSON")
    args = ap.parse_args(argv)
    if args.suite == "idmt" and not args.path:
        ap.error("idmt needs the dataset folder")
    report = run_synth() if args.suite == "synth" else run_idmt(args.path, args.limit)
    for k, v in report["summary"].items():
        print(f"{k:9s} F {v['f']:.3f}  P {v['p']:.2f}  R {v['r']:.2f}  (n={v['n']})")
    if "extra_onsets_per_ref" in report:
        print(f"tracks {report['tracks']}, tom/perc onsets per labeled onset: {report['extra_onsets_per_ref']}")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
