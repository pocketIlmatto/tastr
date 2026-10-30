"""Drum lane benchmark: how well tastr.engine.drums finds each kind of hit.

Four suites:

- `synth`: generated loops with known 16-step patterns for every lane (kick,
  snare, clap, hat, open hat, tom, perc), scored step by step on the detected
  groove the suggestions stage builds. Exact labels, but a synthetic kit.
- `idmt`: IDMT-SMT-Drums (real, sampled and techno kits; kick, snare and hi-hat
  onsets only). Fetch it from https://zenodo.org/records/7544164 and unzip;
  pass the folder that holds `audio/` and `annotation_xml/`.
- `mdb`: MDB Drums (23 real full mixes from MedleyDB, with toms, tambourine and
  side stick). Pass the folder that holds `audio/full_mix/` and
  `annotations/subclass/` (https://github.com/CarlSouthall/MDBDrums).
- `stemgmd-mix`: grooves generated from the StemGMD single hits (10 acoustic
  kits; https://zenodo.org/records/7882857, `StemGMD_single_hits.zip`), with
  toms landing alone and with kick or snare. StemGMD has no percussion, so one
  groove layers the synthetic cowbell over the kit. Exact labels.

`idmt`, `mdb` and `stemgmd-mix` are scored as onsets within 50 ms. A label's
onsets are the peaks of its lanes' summed activation above 0.4x the 99.5th
percentile of that lane family (low: kick and tom; mid: snare, clap and perc;
high: hat and open hat), so a lane a track barely uses can't fire on its own
leakage. Tom and perc recall is also split by whether a kick or snare lands
within 30 ms ("alone" or "with").

    python -m tastr.engine.bench.drums synth
    python -m tastr.engine.bench.drums idmt ~/data/IDMT-SMT-DRUMS-V2 --limit 20
    python -m tastr.engine.bench.drums mdb ~/data/mdb-drums --jobs 4
    python -m tastr.engine.bench.drums stemgmd-mix ~/data/stemgmd-single-hits

`--write-bundle` saves what a remote run reads in one go next to the data:
the annotations as one JSON file (idmt, mdb) or the trimmed mono hits as one
npz (stemgmd-mix). Iterate on a `--limit` subset; run the full set when a
change is worth keeping.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from .. import drums
from ..card import Source, TrackCard
from ..pipeline import SR, AnalysisContext
from .synth import DRUM_PATTERNS, _fade, drum_sounds, make_drum_track

SUITES = ("synth", "idmt", "mdb", "stemgmd-mix")
TOL = 0.05  # seconds: an onset within this of a labeled one counts
WITH_TOL = 0.03  # a tom or perc hit with a kick or snare this close "lands with" it
FAMILY = {"kick": "low", "tom": "low", "snare": "mid", "clap": "mid", "perc": "mid", "hat": "high", "open_hat": "high",
          "cymbal": "high"}

# label -> (reference classes, lanes whose onsets count as finding it)
IDMT_LABELS = {"kick": (("KD",), ("kick",)), "snare": (("SD",), ("snare", "clap")), "hat": (("HH",), ("hat", "open_hat"))}
MDB_CLASSES = {  # MDB subclass -> class used here
    "KD": "kick", "SD": "snare", "SDB": "snare", "SDD": "snare", "SDF": "snare", "SDG": "snare", "SDNS": "snare",
    "CHH": "hat", "PHH": "hat", "OHH": "open_hat", "HIT": "tom", "MHT": "tom", "HFT": "tom", "LFT": "tom",
    "TMB": "perc", "SST": "perc", "RDC": "cymbal", "RDB": "cymbal", "CRC": "cymbal", "CHC": "cymbal", "SPC": "cymbal",
}
LANE_LABELS = {  # mdb and stemgmd-mix
    "kick": (("kick",), ("kick",)),
    "snare": (("snare",), ("snare", "clap")),
    "hat": (("hat",), ("hat",)),
    "open_hat": (("open_hat",), ("open_hat",)),
    "hats": (("hat", "open_hat"), ("hat", "open_hat")),  # either hat lane, as IDMT scores it
    "tom": (("tom",), ("tom",)),
    "perc": (("perc",), ("perc",)),
    "cymbal": (("cymbal",), ("cymbal",)),  # scored only when the drum model supplies a cymbal lane
}
# Hits on these classes don't count against these labels: a ride or crash has no lane of its own and
# reads as a hat.
EXCUSED = {"hat": ("cymbal",), "open_hat": ("cymbal",), "hats": ("cymbal",)}
STEMGMD_CLASSES = {"kick": "kick", "snare": "snare", "hihat_closed": "hat", "hihat_open": "open_hat",
                   "hi_tom": "tom", "mid_tom": "tom", "low_tom": "tom", "ride": "cymbal", "crash_left": "cymbal"}


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
    return {"suite": "synth", "summary": {lane: _prf(*c) for lane, c in counts.items()}, "rows": rows}


def _peaks(x: np.ndarray, thr: float, wait: int = 3) -> np.ndarray:
    """Frames of local maxima of at least `thr`, at least `wait` frames apart."""
    keep: list[int] = []
    for i in range(1, len(x) - 1):
        if x[i] < thr or x[i] <= 0 or x[i] < x[i - 1] or x[i] <= x[i + 1]:
            continue
        if keep and i - keep[-1] < wait:
            if x[i] > x[keep[-1]]:
                keep[-1] = i
        else:
            keep.append(i)
    return np.asarray(keep, dtype=int)


def lane_onsets(acts: dict[str, np.ndarray], lanes, rel: float = 0.4) -> np.ndarray:
    """Onset frames of `lanes` together: peaks above `rel` x the 99.5th percentile of their families' total."""
    fam = {FAMILY[lane] for lane in lanes}
    total = sum(v for lane, v in acts.items() if FAMILY[lane] in fam)
    return _peaks(sum(acts[lane] for lane in lanes), rel * float(np.percentile(total, 99.5)))


def _match(est, ref, tol: float = TOL) -> tuple[np.ndarray, np.ndarray]:
    """Greedy one-to-one matching: (estimates that found a reference, references that were found)."""
    ref = np.asarray(ref, dtype=float)
    hit_e, hit_r = np.zeros(len(est), bool), np.zeros(len(ref), bool)
    for k, e in enumerate(est):
        free = np.flatnonzero(~hit_r & (np.abs(ref - e) <= tol))
        if free.size:
            hit_e[k] = hit_r[free[0]] = True
    return hit_e, hit_r


def _near(t: float, times, tol: float) -> bool:
    return bool(len(times)) and float(np.min(np.abs(np.asarray(times) - t))) <= tol


def _nearest(t: float, ref: dict[str, list[float]], skip=()) -> str:
    """The class (outside `skip`) with the labeled onset closest to `t`, if one is within TOL, else "none"."""
    best, name = TOL, "none"
    for c, times in ref.items():
        if c not in skip and len(times):
            d = float(np.min(np.abs(np.asarray(times) - t)))
            if d <= best:
                best, name = d, c
    return name


def _merged(onsets: dict[str, list[float]], lanes, gap: float = 0.03) -> np.ndarray:
    """The onsets of `lanes` together, one per hit: onsets of different lanes closer than `gap` count once."""
    times = sorted(t for lane in lanes for t in onsets.get(lane, []))
    keep: list[float] = []
    for t in times:
        if not keep or t - keep[-1] > gap:
            keep.append(t)
    return np.asarray(keep, dtype=float)


def score_track(acts: dict[str, np.ndarray] | None, ref: dict[str, list[float]], labels: dict, excused: dict | None = None,
                spare=("tom", "perc", "clap"), hop: int = 512, sr: int = SR, onsets: dict | None = None) -> dict:
    """Counts for one track: per label tp/fp/fn, where its false positives landed, tom/perc recall alone vs
    with a kick or snare, and onsets of the `spare` lanes the dataset has no labels for (it has none of them).
    `onsets` ({lane: seconds}, e.g. from another drum transcriber) replaces the lane activations."""
    excused = excused or {}
    out = {"counts": {}, "fp_at": {}, "split": {}, "excused": 0, "unlabeled": {}}
    for label, (classes, lanes) in labels.items():
        if onsets is None and not all(lane in acts for lane in lanes):
            continue  # e.g. cymbal without the drum model
        want = sorted(t for c in classes for t in ref.get(c, []))
        est = _merged(onsets, lanes) if onsets is not None else lane_onsets(acts, lanes) * hop / sr
        hit_e, hit_r = _match(est, want)
        ok = [t for c in excused.get(label, ()) for t in ref.get(c, [])]
        fps = [e for e, h in zip(est, hit_e) if not h and not _near(e, ok, TOL)]
        if label == "hats":  # it covers both hat lanes; counting the others too would count onsets twice
            out["excused"] = int((~hit_e).sum()) - len(fps)
        out["counts"][label] = [int(hit_r.sum()), len(fps), int((~hit_r).sum())]
        out["fp_at"][label] = dict(Counter(_nearest(e, ref, classes) for e in fps))
        if label in ("tom", "perc") and onsets is None:
            # Lenient: against the lane's own loudest moment, not its family's. Tells a lane that ranks its
            # hits right but too quietly (it would rarely show in the groove) from one that misses them.
            x = sum(acts[lane] for lane in lanes)
            le, lr = _match(_peaks(x, 0.4 * float(x.max())) * hop / sr if x.max() > 0 else [], want)
            out["lenient"] = out.get("lenient", {}) | {label: [int(lr.sum()), int((~le).sum()), int((~lr).sum())]}
        elif label in ("tom", "perc"):
            out["lenient"] = out.get("lenient", {}) | {label: out["counts"][label]}  # nothing laxer to try
        if label in ("tom", "perc"):
            others = [t for c in ("kick", "snare", "KD", "SD") for t in ref.get(c, [])]
            split = {"alone": [0, 0], "with": [0, 0]}  # [found, labeled]
            for t, h in zip(want, hit_r):
                s = split["with" if _near(t, others, WITH_TOL) else "alone"]
                s[0] += int(h)
                s[1] += 1
            out["split"][label] = split
    labeled = {c for classes, _ in labels.values() for c in classes}
    for lane in spare:
        if lane not in labeled:  # e.g. clap on MDB, which labels none; it still counts towards snare above
            out["unlabeled"][lane] = len(onsets.get(lane, [])) if onsets is not None else len(lane_onsets(acts, (lane,)))
    return out


def _activations(y: np.ndarray, uri: str, path: str | None = None) -> tuple[dict[str, np.ndarray], int]:
    ctx = AnalysisContext(y=y, sr=SR, card=TrackCard(source=Source(kind="file", uri=uri, local_path=path)))
    return drums.lane_activations(ctx, np.arange(1 + len(y) // ctx.hop), slop=0), ctx.hop


def _work(job) -> dict:
    """One item: (id, audio path or samples at SR, ref, labels, excused[, onsets]) -> its score_track counts.
    With `onsets` (another transcriber's {lane: seconds}) the audio isn't analyzed."""
    item_id, audio, ref, labels, excused, *given = job
    if given:
        return {"id": item_id, "seconds": given[0].get("_duration", 0.0)} | score_track(
            None, ref, labels, excused, onsets={k: v for k, v in given[0].items() if not k.startswith("_")})
    if isinstance(audio, (str, Path)):
        import librosa

        path = str(audio)
        audio, _ = librosa.load(path, sr=SR, mono=True)
    else:
        path = None
    acts, hop = _activations(np.asarray(audio, dtype=np.float32), item_id, path)
    return {"id": item_id, "seconds": round(len(audio) / SR, 1)} | score_track(acts, ref, labels, excused, hop=hop)


def run_onsets(suite: str, items: list, labels: dict, excused: dict | None = None, jobs: int = 1) -> dict:
    """Score `items` [(id, audio path or samples, {class: onset seconds}[, {lane: onsets}])] and pool the
    counts per label."""
    work = [(it[0], it[1], it[2], labels, excused, *it[3:]) for it in items]
    t0 = time.time()
    if jobs > 1 and len(work) > 1:
        # One track here first so librosa's numba kernels are compiled and cached before the workers start
        # (see runner.run).
        rows = [_work(work[0])]
        # Spawned, not forked: that first track may have started PyTorch's threads (the drum model), and
        # forked workers then hang in them (Linux forks by default; it hung every drum-model run on Modal).
        import multiprocessing

        with ProcessPoolExecutor(jobs, mp_context=multiprocessing.get_context("spawn")) as pool:
            rows += list(pool.map(_work, work[1:]))
    else:
        rows = [_work(w) for w in work]
    seen = {label for r in rows for label in r["counts"]}  # labels whose lanes this run produced
    counts = {label: np.zeros(3, dtype=int) for label in labels if label in seen}
    split = {label: {"alone": [0, 0], "with": [0, 0]} for label in ("tom", "perc") if label in labels}
    fp_at: dict[str, Counter] = {label: Counter() for label in counts}
    unlabeled: Counter = Counter()
    lenient = {label: np.zeros(3, dtype=int) for label in split}
    for r in rows:
        for label, c in r.get("lenient", {}).items():
            lenient[label] += c
        for label, c in r["counts"].items():
            counts[label] += c
            fp_at[label].update(r["fp_at"][label])
        for label, s in r["split"].items():
            for cond in s:
                split[label][cond] = [a + b for a, b in zip(split[label][cond], s[cond])]
        unlabeled.update(r["unlabeled"])
    summary = {label: _prf(*c) for label, c in counts.items()}
    for label, s in split.items():
        summary[label] |= {"lenient": _prf(*lenient[label])}
        summary[label] |= {f"r_{cond}": round(f / max(1, n), 3) for cond, (f, n) in s.items()}
        summary[label] |= {f"n_{cond}": n for cond, (_, n) in s.items()}
    n_ref = sum(int(c[0] + c[2]) for label, c in counts.items() if label != "hats")
    return {"suite": suite, "tracks": len(rows), "summary": summary,
            "fp_at": {label: dict(c.most_common()) for label, c in fp_at.items() if c},
            "excused_cymbal_onsets": sum(r["excused"] for r in rows),
            "unlabeled_onsets_per_ref": {lane: round(n / max(1, n_ref), 3) for lane, n in unlabeled.items()},
            "seconds": round(time.time() - t0, 1), "audio_seconds": round(sum(r["seconds"] for r in rows), 1),
            "rows": rows}


# --- IDMT-SMT-Drums -------------------------------------------------------------------------------

IDMT_BUNDLE = "idmt-annotations.json"


def _idmt_annotations(root: Path) -> dict[str, dict[str, list[float]]]:
    """{mix audio file name: {"KD"|"SD"|"HH": onset seconds}} from the XML files."""
    out = {}
    for fx in sorted((root / "annotation_xml").glob("*#MIX.xml")):
        r = ET.parse(fx).getroot()
        ref: dict[str, list[float]] = {k: [] for k in ("KD", "SD", "HH")}
        for ev in r.iter("event"):
            inst = ev.find("instrument").text
            if inst in ref:
                ref[inst].append(float(ev.find("onsetSec").text))
        out[r.find("globalParameter/audioFileName").text] = ref
    return out


def idmt_items(root: str | Path, limit: int | None = None) -> list[tuple[str, Path, dict[str, list[float]]]]:
    """(id, mix audio path, {"KD"|"SD"|"HH": onset seconds}) for every annotated mix, from the bundle if present."""
    root = Path(root).expanduser()
    bundle = root / IDMT_BUNDLE
    ann = json.loads(bundle.read_text()) if bundle.is_file() else _idmt_annotations(root)
    # With the bundle, audio isn't checked file by file (slow on a bucket mount); it was there when written.
    items = [(name, root / "audio" / name, ref) for name, ref in sorted(ann.items())
             if bundle.is_file() or (root / "audio" / name).exists()]
    return items[:limit or None]


def run_idmt(root: str | Path, limit: int | None = None, jobs: int = 1) -> dict:
    """Onset precision/recall for kick, snare and hat, plus how often tom, perc and clap fire (IDMT has none)."""
    return run_onsets("idmt", [(i, str(p), ref) for i, p, ref in idmt_items(root, limit)], IDMT_LABELS, jobs=jobs)


# --- MDB Drums ------------------------------------------------------------------------------------

MDB_BUNDLE = "mdb-annotations.json"


def _mdb_annotations(root: Path) -> dict[str, dict[str, list[float]]]:
    """{track name: {MDB subclass: onset seconds}} from annotations/subclass/."""
    out = {}
    for f in sorted((root / "annotations" / "subclass").glob("*_subclass.txt")):
        ref: dict[str, list[float]] = {}
        for line in f.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                ref.setdefault(parts[1], []).append(float(parts[0]))
        out[f.name.removesuffix("_subclass.txt")] = ref
    return out


def mdb_items(root: str | Path, limit: int | None = None, drums_only: bool = False):
    """(id, audio path, {class: onset seconds}) per track, classes as in MDB_CLASSES. `drums_only` scores the
    drum stem instead of the full mix."""
    root = Path(root).expanduser()
    bundle = root / MDB_BUNDLE
    ann = json.loads(bundle.read_text()) if bundle.is_file() else _mdb_annotations(root)
    items = []
    for name, sub in sorted(ann.items()):
        wav = root / "audio" / ("drum_only" if drums_only else "full_mix") / f"{name}_{'Drum' if drums_only else 'MIX'}.wav"
        if not bundle.is_file() and not wav.exists():
            continue
        ref: dict[str, list[float]] = {}
        for code, times in sub.items():
            if code in MDB_CLASSES:
                ref.setdefault(MDB_CLASSES[code], []).extend(times)
        items.append((name, wav, {k: sorted(v) for k, v in ref.items()}))
    return items[:limit or None]


def run_mdb(root: str | Path, limit: int | None = None, jobs: int = 1, drums_only: bool = False) -> dict:
    items = [(i, str(p), ref) for i, p, ref in mdb_items(root, limit, drums_only)]
    report = run_onsets("mdb", items, LANE_LABELS, EXCUSED, jobs)
    report["audio"] = "drum_only" if drums_only else "full_mix"
    return report


# --- StemGMD single hits -> generated grooves -----------------------------------------------------

STEMGMD_BUNDLE = "stemgmd-hits.npz"
HIT_SECONDS = 1.5  # hits are trimmed to this (with a fade); cymbals ring longer, but nothing scored needs it

# 16-step bars per StemGMD class (plus "perc", the synthetic cowbell). Each groove is three bars of its
# main pattern and one fill bar, played twice.
GROOVES = {
    "rock-fill": {
        "main": {"kick": "x.....x.x.......", "snare": "....x.......x...", "hihat_closed": "x.x.x.x.x.x.x.x."},
        "fill": {"kick": "x...............", "snare": "....x...", "hihat_closed": "x.x.x...",
                 "hi_tom": "........xx......", "mid_tom": "..........xx....", "low_tom": "............xxxx"},
    },
    "floor-tom": {  # toms on the kick and snare, and some on their own
        "main": {"kick": "x.......x.x.....", "snare": "....x.......x...", "ride": "x.x.x.x.x.x.x.x.",
                 "low_tom": "x...x...x...x...", "mid_tom": "......x........x"},
        "fill": {"kick": "x.......x.......", "snare": "....x.......x...", "low_tom": "x.x.x.x.x.x.x.x.",
                 "hi_tom": ".x.x.x.x.x.x.x.x"},
    },
    "tribal": {
        "main": {"kick": "x..x..x...x..x..", "low_tom": "x..x..x...x..x..", "hi_tom": "..x...x.x...x.x.",
                 "hihat_closed": "x.x.x.x.x.x.x.x."},
        "fill": {"kick": "x...x...x...x...", "mid_tom": "x.x.x.x.x.x.x.x.", "hi_tom": ".x.x.x.x.x.x.x.x"},
    },
    "disco-open-hat": {
        "main": {"kick": "x...x...x...x...", "snare": "....x.......x...", "hihat_closed": "x...x...x...x...",
                 "hihat_open": "..x...x...x...x."},
        "fill": {"kick": "x...x...x...x...", "snare": "....x.......xxxx", "crash_left": "x...............",
                 "hihat_open": "..x...x...x....."},
    },
    "perc-layer": {  # the synthetic cowbell on the kick, the snare and on its own
        "main": {"kick": "x...x...x...x...", "snare": "....x.......x...", "hihat_closed": "..x...x...x...x.",
                 "perc": "x..x..x...x.x..."},
        "fill": {"kick": "x.......x.......", "snare": "....x.......x...", "perc": "x.x.x.xx.x.x.xxx"},
    },
}
GROOVE_BPMS = (96, 124)


def _read_hits(root: Path) -> dict[str, np.ndarray]:
    """{"kit/class-i": mono hit at SR}, trimmed from where it starts sounding, from StemGMD_single_hits/."""
    import librosa
    import soundfile as sf

    hits = {}
    for wav in sorted((root / "StemGMD_single_hits").glob("*/*.wav")):
        y, sr = sf.read(str(wav), always_2d=True)
        y = librosa.resample(y.mean(axis=1), orig_sr=sr, target_sr=SR)
        a = np.abs(y)
        start = int(np.argmax(a > 1e-3 * a.max()))  # the files start with up to ~20 ms of silence
        hits[f"{wav.parent.name}/{wav.stem}"] = _fade(y[start:start + int(HIT_SECONDS * SR)], SR).astype(np.float16)
    if not hits:
        raise FileNotFoundError(f"no StemGMD_single_hits/<kit>/*.wav under {root}")
    return hits


def write_stemgmd_bundle(root: str | Path) -> Path:
    root = Path(root).expanduser()
    out = root / STEMGMD_BUNDLE
    np.savez(out, **_read_hits(root))
    return out


def _stemgmd_hits(root: Path) -> dict[str, dict[str, list[np.ndarray]]]:
    """{kit: {class: [variants]}} from the bundle, or from the WAV files (slower) without one."""
    bundle = root / STEMGMD_BUNDLE
    data = np.load(bundle) if bundle.is_file() else _read_hits(root)
    keys = data.files if bundle.is_file() else list(data)
    kits: dict[str, dict[str, list[np.ndarray]]] = {}
    for key in sorted(keys, key=lambda k: (k.rsplit("-", 1)[0], int(k.rsplit("-", 1)[1]))):
        kit, name = key.split("/")
        kits.setdefault(kit, {}).setdefault(name.rsplit("-", 1)[0], []).append(data[key].astype(np.float32))
    return kits


def make_groove(kit: dict[str, list[np.ndarray]], groove: dict, bpm: float, seed: int, perc: np.ndarray):
    """(audio, {class: onset seconds}) for one groove on one kit: 3 main bars + 1 fill bar, twice.

    Every hit picks one of the kit's recorded variants (they differ in velocity) with a fixed seed, and a
    closed hat chokes a ringing open hat."""
    rng = np.random.default_rng(seed)
    step = 60.0 / bpm / 4
    bars = [groove["main"]] * 3 + [groove["fill"]]
    bars = bars * 2
    y = np.zeros(int((len(bars) * 16 * step + HIT_SECONDS + 0.5) * SR), dtype=np.float32)
    ref: dict[str, list[float]] = {}
    for b, bar in enumerate(bars):
        closed = [s for s, c in enumerate(bar.get("hihat_closed", "")) if c == "x"]
        for cls, pat in bar.items():
            for s, c in enumerate(pat):
                if c != "x":
                    continue
                t = (b * 16 + s) * step
                if cls == "perc":
                    sig = perc * rng.uniform(0.6, 1.0)
                else:
                    variants = kit[cls]
                    sig = variants[int(rng.integers(len(variants)))]
                if cls == "hihat_open" and closed:
                    nxt = min(((h - s) % 16 or 16) for h in closed)
                    sig = _fade(sig[: int(nxt * step * SR)], SR)
                i = int(round(t * SR))
                y[i:i + sig.size] += sig[: y.size - i]
                ref.setdefault(STEMGMD_CLASSES.get(cls, cls), []).append(round(t, 4))
    y = y / (np.abs(y).max() + 1e-9) * 0.9
    return y.astype(np.float32), {k: sorted(v) for k, v in ref.items()}


KIT_SPLITS = ("all", "train", "test")


def kit_split(kits, split: str = "all") -> list[str]:
    """Kits in `split`: alternate kits in name order, so tuning on "train" leaves "test" unseen."""
    if split not in KIT_SPLITS:
        raise ValueError(f"unknown kit split {split!r}; choose from {', '.join(KIT_SPLITS)}")
    names = sorted(kits)
    return names if split == "all" else names[(split == "test")::2]


def stemgmd_items(root: str | Path, limit: int | None = None, kits: str = "all"):
    """(id, audio, {class: onset seconds}) for every kit x groove x tempo, in a fixed order that spreads
    a --limit over grooves and kits. A groove sounds the same whichever split it is drawn in."""
    root = Path(root).expanduser()
    hits = _stemgmd_hits(root)
    keep = set(kit_split(hits, kits))
    perc = drum_sounds(SR)["perc"].astype(np.float32)
    combos = [(n, bpm, kit, g) for n, (bpm, kit, g) in enumerate(
        (bpm, kit, g) for bpm in GROOVE_BPMS for k, kit in enumerate(sorted(hits))
        for g in list(GROOVES)[k % len(GROOVES):] + list(GROOVES)[:k % len(GROOVES)]) if kit in keep]
    items = []
    for n, bpm, kit, g in combos[:limit or None]:
        y, ref = make_groove(hits[kit], GROOVES[g], bpm, seed=n, perc=perc)
        items.append((f"{kit}/{g}/{bpm}", y, ref))
    return items


def run_stemgmd(root: str | Path, limit: int | None = None, jobs: int = 1, kits: str = "all") -> dict:
    report = run_onsets("stemgmd-mix", stemgmd_items(root, limit, kits), LANE_LABELS, EXCUSED, jobs)
    report["kits"] = kits
    report["note"] = "perc is the synthetic cowbell from bench/synth.py layered over the real kits"
    return report


# --- other drum transcribers ----------------------------------------------------------------------
# A model that runs elsewhere (its own virtualenv) reads `export`'s manifest, writes {id: {lane: onset seconds,
# "_seconds": analysis time}} and `score_external` scores that like the lanes, with the same matcher.

EXTERNAL_LABELS = LANE_LABELS


def synth_onset_items(bpms=(100, 128, 174)):
    """The synth suite's loops with their hits as onset times, so it can be scored like the onset suites."""
    items = []
    for name, pats in DRUM_PATTERNS.items():
        for bpm in bpms:
            y, truth = make_drum_track(pats, bpm=bpm)
            step = truth["bar_seconds"] / 16
            bars = int(round((len(y) / SR - 1.0) / truth["bar_seconds"]))
            ref = {lane: [round((b * 16 + i) * step, 4) for b in range(bars) for i, c in enumerate(p) if c == "x"]
                   for lane, p in pats.items()}
            items.append((f"{name}/{bpm}", y, ref))
    return items


SYNTH_LABELS = LANE_LABELS | {"snare": (("snare", "clap"), ("snare", "clap")), "clap": (("clap",), ("clap",))}


def suite_items(suite: str, root=None, limit=None, drums_only=False, kits="all"):
    """(items, labels, excused) of an onset suite; "synth-onsets" is the synth loops scored as onsets."""
    if suite == "synth-onsets":
        return synth_onset_items()[:limit or None], SYNTH_LABELS, {}
    if suite == "idmt":
        return [(i, str(p), r) for i, p, r in idmt_items(root, limit)], IDMT_LABELS, {}
    if suite == "mdb":
        return [(i, str(p), r) for i, p, r in mdb_items(root, limit, drums_only)], LANE_LABELS, EXCUSED
    if suite == "stemgmd-mix":
        return stemgmd_items(root, limit, kits), LANE_LABELS, EXCUSED
    raise ValueError(f"{suite} isn't an onset suite")


def export(suite: str, out: str | Path, root=None, limit=None, drums_only=False, kits="all") -> Path:
    """Write `out`/manifest.json [{id, audio, ref}] for another transcriber; generated audio goes to `out`/audio."""
    import soundfile as sf

    out = Path(out).expanduser()
    (out / "audio").mkdir(parents=True, exist_ok=True)
    items, _, _ = suite_items(suite, root, limit, drums_only, kits)
    rows = []
    for item_id, audio, ref in items:
        if not isinstance(audio, str):
            path = out / "audio" / (item_id.replace("/", "__") + ".wav")
            sf.write(str(path), np.asarray(audio, dtype=np.float32), SR)
            audio = str(path)
        rows.append({"id": item_id, "audio": audio, "ref": ref})
    path = out / "manifest.json"
    path.write_text(json.dumps({"suite": suite, "kits": kits, "drums_only": drums_only, "items": rows}))
    return path


def score_external(manifest: str | Path, onsets: str | Path, name: str = "external") -> dict:
    """Score another transcriber's onsets ({id: {lane: seconds}}) on an exported manifest, like the lanes."""
    m = json.loads(Path(manifest).read_text())
    got = json.loads(Path(onsets).read_text())
    _, labels, excused = suite_items(m["suite"], limit=0) if m["suite"] == "synth-onsets" else (None, *{
        "idmt": (IDMT_LABELS, {}), "mdb": (LANE_LABELS, EXCUSED), "stemgmd-mix": (LANE_LABELS, EXCUSED)}[m["suite"]])
    if m["suite"] != "idmt":
        labels = labels | {"cymbal": EXTERNAL_LABELS["cymbal"]}
    items = [(r["id"], r["audio"], r["ref"], got[r["id"]]) for r in m["items"] if r["id"] in got]
    report = run_onsets(m["suite"], items, labels, excused)
    report["transcriber"] = name
    report["seconds"] = round(sum(got[r[0]].get("_seconds", 0.0) for r in items), 1)
    return report


def write_bundle(suite: str, root: str | Path) -> Path:
    """The one file a remote run of `suite` reads instead of walking the folder."""
    root = Path(root).expanduser()
    if suite == "stemgmd-mix":
        return write_stemgmd_bundle(root)
    if suite == "idmt":
        out, ann = root / IDMT_BUNDLE, _idmt_annotations(root)
        ann = {k: v for k, v in ann.items() if (root / "audio" / k).exists()}
    elif suite == "mdb":
        out, ann = root / MDB_BUNDLE, _mdb_annotations(root)
    else:
        raise ValueError(f"{suite} has nothing to bundle")
    if not ann:
        raise FileNotFoundError(f"no {suite} annotations under {root}")
    out.write_text(json.dumps(ann))
    return out


def run(suite: str, root: str | Path | None = None, limit: int | None = None, jobs: int = 1,
        drums_only: bool = False, kits: str = "all") -> dict:
    if suite == "synth":
        return run_synth()
    if root is None:
        raise ValueError(f"{suite} needs the dataset folder")
    if suite == "idmt":
        return run_idmt(root, limit, jobs)
    if suite == "mdb":
        return run_mdb(root, limit, jobs, drums_only)
    if suite == "stemgmd-mix":
        return run_stemgmd(root, limit, jobs, kits)
    raise ValueError(f"unknown drum suite {suite!r}; choose from {', '.join(SUITES)}")


def format_report(report: dict) -> str:
    lines = []
    for k, v in report["summary"].items():
        line = f"{k:9s} F {v['f']:.3f}  P {v['p']:.2f}  R {v['r']:.2f}  (n={v['n']})"
        if "r_alone" in v:
            line += f"  R alone {v['r_alone']:.2f} (n={v['n_alone']}), with kick/snare {v['r_with']:.2f} (n={v['n_with']})"
            line += f"  lenient F {v['lenient']['f']:.2f} P {v['lenient']['p']:.2f} R {v['lenient']['r']:.2f}"
        lines.append(line)
    if "tracks" in report:
        extra = ", ".join(f"{lane} {n}" for lane, n in report["unlabeled_onsets_per_ref"].items())
        lines.append(f"tracks {report['tracks']} ({report['audio_seconds']:.0f} s of audio) in {report['seconds']:.0f} s"
                     + (f"; unlabeled lanes fire per labeled onset: {extra}" if extra else ""))
        if report.get("excused_cymbal_onsets"):
            lines.append(f"hat-lane onsets on ride/crash hits (not counted): {report['excused_cymbal_onsets']}")
        for label in ("tom", "perc"):
            if report["fp_at"].get(label):
                lines.append(f"{label} false positives at: " + ", ".join(f"{c} {n}" for c, n in report["fp_at"][label].items()))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tastr.engine.bench.drums", description=__doc__.split("\n\n")[0])
    ap.add_argument("suite", choices=SUITES)
    ap.add_argument("path", nargs="?", help="dataset folder (idmt, mdb, stemgmd-mix)")
    ap.add_argument("--limit", type=int, help="first N tracks only")
    ap.add_argument("--jobs", type=int, default=1, help="tracks analyzed in parallel")
    ap.add_argument("--drums-only", action="store_true", help="mdb: score the drum stems instead of the full mixes")
    ap.add_argument("--kits", choices=KIT_SPLITS, default="all", help="stemgmd-mix: all kits, or the train / test half")
    ap.add_argument("--write-bundle", action="store_true", help="save the file a remote run reads, next to the data")
    ap.add_argument("--out", help="write the full report as JSON")
    args = ap.parse_args(argv)
    if args.suite != "synth" and not args.path:
        ap.error(f"{args.suite} needs the dataset folder")
    if args.write_bundle:
        print(f"wrote {write_bundle(args.suite, args.path)}")
        return 0
    report = run(args.suite, args.path, args.limit, args.jobs, args.drums_only, args.kits)
    print(format_report(report))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
