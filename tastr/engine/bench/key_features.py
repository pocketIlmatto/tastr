"""Per-track key features cached to .npz, so key estimation can be tuned and retrained offline in seconds.

Analyzing a track takes seconds; scoring a key-estimation variant on cached chroma takes milliseconds.
`cache` decodes each labeled track of a key dataset exactly like the bench does and writes
<out>/<track id>.npz with:

    chroma      12 x frames float32  signals.chroma (chroma_cqt of the harmonic part)
    bass        36 x frames float32  signals.bass_cqt
    tonic, mode reference key
    label_conf  annotator confidence (0-2) where the dataset has one (Beatport EDM Key), else -1
    silent      True when the core stage would skip key estimation

Files already written are skipped, so an interrupted run resumes.

    python -m tastr.engine.bench.key_features beatport-key ~/data/beatport-key ~/data/features/beatport-key --jobs 8

Training the key model needs only each track's 12-bin key profile and label, so `profiles` computes
those straight from the audio, without the cache, and `write_profiles` saves them as one small JSON file
(about 200 bytes a track) that `train_key --profiles` reads. `tastr key-profiles` runs it locally or on
Modal against the R2 bucket; `profiles_from_cache` turns an existing cache into the same file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from xml.etree import ElementTree

import numpy as np

from .. import music
from .datasets import Item, load as load_dataset


def track_id(item: Item) -> str:
    """"bp-key-1234" -> "1234": the dataset's own id, used as the cache file name."""
    return item.id.split("-key-", 1)[-1]


def beatport_label_confidence(root: str | Path) -> dict[str, int]:
    """{track id: confidence} from Beatport-EDM-Key-Dataset.xlsx (columns A `id` and H `confidence`).

    Read straight from the sheet XML: both columns are plain numbers, so no spreadsheet library is needed.
    """
    path = Path(root) / "Beatport-EDM-Key-Dataset.xlsx"
    if not path.exists():
        return {}
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as z:
        sheet = ElementTree.fromstring(z.read("xl/worksheets/sheet1.xml"))
    out = {}
    for row in sheet.iterfind(".//m:sheetData/m:row", ns):
        cells = {re.sub(r"\d", "", c.get("r", "")): c.findtext("m:v", None, ns) for c in row.iterfind("m:c", ns)}
        if cells.get("A") and cells.get("H") and cells["A"].replace(".", "").isdigit() and float(cells["A"]) > 0:
            out[str(int(float(cells["A"])))] = int(float(cells["H"]))  # id 0 is a totals row below the table
    return out


def _extract(args) -> tuple[str, str | None]:
    item, out_dir, conf = args
    from .. import signals
    from ..audio import load_audio
    from ..pipeline import HOP, SR, AnalysisContext

    sid = track_id(item)
    dest = Path(out_dir) / f"{sid}.npz"
    if dest.exists():
        return sid, None
    try:
        y = load_audio(item.path, SR)
        ctx = AnalysisContext(y=y, sr=SR, card=None, hop=HOP)
        silent = y.size == 0 or not np.any(np.abs(y) > 1e-4)
        tmp = dest.with_name(dest.stem + ".tmp.npz")
        np.savez(tmp, chroma=ctx.get("chroma", signals.chroma).astype(np.float32),
                 bass=ctx.get("bass_cqt", signals.bass_cqt).astype(np.float32),
                 tonic=item.truth.tonic, mode=item.truth.mode, label_conf=conf.get(sid, -1), silent=silent)
        os.replace(tmp, dest)  # only complete files count as done
        return sid, None
    except Exception as e:
        return sid, f"{type(e).__name__}: {e}"


def cache(dataset: str, root: str | Path, out_dir: str | Path, jobs: int = 1, progress=None) -> list[tuple[str, str]]:
    """Write the feature cache for every labeled track; returns (track id, error) for the failures."""
    items = load_dataset(dataset, str(root))
    conf = beatport_label_confidence(root) if dataset == "beatport-key" else {}
    os.makedirs(out_dir, exist_ok=True)
    work = [(it, str(out_dir), conf) for it in items]
    if not work:
        return []
    # One track here first so numba's kernels are compiled and cached before workers share the cache.
    results = [_extract(work[0])]
    if jobs > 1:
        with ProcessPoolExecutor(jobs) as pool:
            for r in pool.map(_extract, work[1:]):
                results.append(r)
                if progress:
                    progress(len(results), len(work))
    else:
        for w in work[1:]:
            results.append(_extract(w))
            if progress:
                progress(len(results), len(work))
    return [(sid, err) for sid, err in results if err]


def _profile(args) -> dict:
    item, dataset, conf = args
    from .. import signals
    from ..audio import load_audio
    from ..pipeline import HOP, SR, AnalysisContext

    sid = track_id(item)
    try:
        y = load_audio(item.path, SR)
        chroma = AnalysisContext(y=y, sr=SR, card=None, hop=HOP).get("chroma", signals.chroma)
        silent = y.size == 0 or not np.any(np.abs(y) > 1e-4)
        return {"id": sid, "dataset": dataset, "profile": music.key_profile(chroma), "silent": bool(silent),
                "tonic": item.truth.tonic, "mode": item.truth.mode, "label_conf": conf.get(sid, -1)}
    except Exception as e:
        return {"id": sid, "error": f"{type(e).__name__}: {e}"}


def profiles(dataset: str, items: list[Item], root: str | Path, jobs: int = 1, limit: int | None = None,
             progress=None) -> tuple[list[dict], list[tuple[str, str]]]:
    """Key profile and label of every item (as `load` returns them, computed from the audio); also returns
    (track id, error) for the failures. `root` is the dataset folder, for Beatport's label confidence."""
    conf = beatport_label_confidence(root) if dataset == "beatport-key" else {}
    work = [(it, dataset, conf) for it in items[:limit]]
    if not work:
        return [], []
    results = [_profile(work[0])]  # compiles numba's kernels before workers share the cache
    if jobs > 1:
        with ProcessPoolExecutor(jobs) as pool:
            for r in pool.map(_profile, work[1:]):
                results.append(r)
                if progress:
                    progress(len(results), len(work))
    else:
        for w in work[1:]:
            results.append(_profile(w))
            if progress:
                progress(len(results), len(work))
    return [r for r in results if "error" not in r], [(r["id"], r["error"]) for r in results if "error" in r]


def profiles_from_cache(dataset: str, root: str | Path, feat_dir: str | Path) -> list[dict]:
    """The same rows from a feature cache written by `cache`, without decoding any audio."""
    return load(dataset, root, feat_dir)


def write_profiles(rows: list[dict], path: str | Path) -> Path:
    path = Path(path)
    path.write_text(json.dumps({"tracks": [dict(r, profile=[round(float(v), 6) for v in r["profile"]]) for r in rows]}))
    return path


def read_profiles(path: str | Path) -> dict[str, list[dict]]:
    """{dataset: rows} from a `write_profiles` file, with profiles as arrays, ready for `train_key`."""
    out: dict[str, list[dict]] = {}
    for r in json.loads(Path(path).read_text())["tracks"]:
        out.setdefault(r["dataset"], []).append(dict(r, profile=np.array(r["profile"])))
    return out


def load(dataset: str, root: str | Path, feat_dir: str | Path) -> list[dict]:
    """Cached tracks in dataset loader order, with the 12-bin key profile the engine computes from them."""
    tracks = []
    for it in load_dataset(dataset, str(root)):
        f = Path(feat_dir) / f"{track_id(it)}.npz"
        if not f.exists():
            continue
        d = np.load(f)
        tracks.append({"id": track_id(it), "dataset": dataset, "profile": music.key_profile(d["chroma"]),
                       "silent": bool(d["silent"]), "tonic": int(d["tonic"]), "mode": str(d["mode"]),
                       "label_conf": int(d["label_conf"])})
    return tracks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset", help="a key dataset: giantsteps-key, beatport-key or manifest")
    ap.add_argument("root", help="dataset folder")
    ap.add_argument("out", help="folder for the .npz files")
    ap.add_argument("--jobs", type=int, default=1)
    a = ap.parse_args(argv)
    t0 = time.time()

    def progress(i, n):
        print(f"\r{i}/{n} {time.time() - t0:.0f}s", end="", file=sys.stderr, flush=True)

    failed = cache(a.dataset, a.root, a.out, a.jobs, progress)
    print(f"\ndone in {time.time() - t0:.0f}s, {len(failed)} failed", file=sys.stderr)
    for sid, err in failed:
        print(f"  {sid}: {err}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
