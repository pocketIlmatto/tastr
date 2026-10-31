"""Job bodies that the Modal functions call. Plain Python, so tests run them locally."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

# A dataset's item list, written next to the data so a remote run reads one file instead of walking
# the bucket (minutes over the R2 mount on a cold container). One per dataset, since some share a
# folder (beatport-key and beatport-tempo). Rewrite it when the folder changes.
def index_name(dataset: str) -> str:
    return f"tastr-index-{dataset}.json"


def _load(dataset: str, folder: Path) -> list:
    """The dataset's loader: a `tastr bench` dataset or a vibe one (tastr.engine.bench.vibes)."""
    from ..engine import bench
    from ..engine.bench import vibes

    return vibes.load(dataset, folder) if dataset in vibes.DATASETS else bench.load(dataset, str(folder))


def write_index(dataset: str, folder: str | Path) -> Path:
    """Run the dataset's loader on a local folder and save its items, with paths relative to it."""
    folder = Path(folder).expanduser().resolve()
    items = _load(dataset, folder)
    if not items:
        raise ValueError(f"no labeled tracks with audio found for {dataset} at {folder}")
    rows = [{"id": it.id, "path": str(Path(it.path).relative_to(folder)), "truth": asdict(it.truth),
             "meta": it.meta} for it in items]
    out = folder / index_name(dataset)
    out.write_text(json.dumps({"dataset": dataset, "items": rows}))
    return out


def load_items(dataset: str, folder: Path) -> list:
    """The folder's index when it has one for this dataset, else the dataset's loader."""
    from ..engine.bench.datasets import Item, Truth

    for index in (folder / index_name(dataset), folder / "tastr-index.json"):  # the second: #61's name
        if not index.is_file():
            continue
        data = json.loads(index.read_text())
        if data.get("dataset") == dataset:
            return [Item(id=r["id"], path=str(folder / r["path"]), truth=Truth(**r["truth"]), meta=r.get("meta", {}))
                    for r in data["items"]]
    return _load(dataset, folder)


def run_bench(dataset: str, path: str | None, root: str | Path, stages: list[str] | None = None,
              limit: int | None = None, jobs: int = 1, progress=None) -> dict:
    """`tastr bench` with `path` relative to `root` (the bucket mount on Modal). Returns the report."""
    from ..engine import bench
    from ..engine.bench import runner

    t0 = time.time()
    items = load_items(dataset, Path(root) / path) if path else bench.load(dataset)
    if not items:
        raise ValueError(f"no labeled tracks with audio found for {dataset} at {path} in the bucket")
    load_seconds = round(time.time() - t0, 1)
    report = runner.run(items, stages=stages, jobs=jobs, limit=limit, progress=progress)
    report["timing"] = {"load_seconds": load_seconds, "jobs": jobs}
    return report


def run_key_profiles(dataset: str, path: str, root: str | Path, limit: int | None = None, jobs: int = 1,
                     progress=None) -> dict:
    """Key profiles and labels for `train_key` (`tastr key-profiles`), with `path` relative to `root`."""
    from ..engine.bench import key_features

    t0 = time.time()
    folder = Path(root) / path
    items = load_items(dataset, folder)
    if not items:
        raise ValueError(f"no labeled tracks with audio found for {dataset} at {path}")
    load_seconds = round(time.time() - t0, 1)
    rows, failed = key_features.profiles(dataset, items, folder, jobs=jobs, limit=limit, progress=progress)
    return {"tracks": [dict(r, profile=[float(v) for v in r["profile"]]) for r in rows], "failed": failed,
            "timing": {"load_seconds": load_seconds, "seconds": round(time.time() - t0 - load_seconds, 1), "jobs": jobs}}


def run_vibe_bench(dataset: str, path: str, root: str | Path, limit: int | None = None, jobs: int = 1,
                   progress=None) -> dict:
    """`tastr vibe-bench` with `path` relative to `root`: per-track rows plus the agreement report."""
    from ..engine.bench import vibes

    t0 = time.time()
    items = load_items(dataset, Path(root) / path)
    if not items:
        raise ValueError(f"no labeled tracks with audio found for {dataset} at {path}")
    load_seconds = round(time.time() - t0, 1)
    rows = vibes.analyze(items, jobs=jobs, limit=limit, progress=progress)
    return {"dataset": dataset, "report": vibes.score(rows), "rows": rows,
            "timing": {"load_seconds": load_seconds, "seconds": round(time.time() - t0 - load_seconds, 1), "jobs": jobs}}


SPEC_BENCHES = {"tempo": "tempo_spec", "downbeats": "downbeats", "meter": "meter",  # Beat This! spectrogram benches
                "sections": "sections_spec"}  # Harmonix mel spectrograms


def spec_module(bench: str):
    import importlib

    if bench not in SPEC_BENCHES:
        raise ValueError(f"unknown spectrogram bench {bench!r}; choose from {', '.join(SPEC_BENCHES)}")
    return importlib.import_module(f"tastr.engine.bench.{SPEC_BENCHES[bench]}")


def run_spec_bench(bench: str, dataset: str, folder: str | Path, limit: int | None = None) -> dict:
    """A Beat This! spectrogram bench (`python -m tastr.engine.bench.<module>`) on `folder`; returns its report."""
    mod = spec_module(bench)
    t0 = time.time()
    items = mod.load(folder, dataset, limit)
    load_seconds = round(time.time() - t0, 1)
    report = mod.run(items)
    report["timing"] = {"load_seconds": load_seconds, "seconds": round(time.time() - t0 - load_seconds, 1)}
    return report


def run_drum_bench(suite: str, folder: str | Path | None, limit: int | None = None, jobs: int = 1,
                   drums_only: bool = False, kits: str = "all") -> dict:
    """A drum lane suite (`python -m tastr.engine.bench.drums`) on `folder`; returns its report."""
    from ..engine.bench import drums

    if suite not in drums.SUITES:
        raise ValueError(f"unknown drum suite {suite!r}; choose from {', '.join(drums.SUITES)}")
    t0 = time.time()
    report = drums.run(suite, folder, limit, jobs, drums_only, kits)
    report["timing"] = {"seconds": round(time.time() - t0, 1), "jobs": jobs}
    return report


def run_analyze(audio: bytes, suffix: str, stages: list[str] | None = None, options: dict | None = None,
                signals: bytes | None = None) -> dict:
    """Analyze one file's bytes; returns the card as a dict (its source names a temporary file).
    `signals`: what the browser measured from it (tastr/engine/browser_signals.py), if anything."""
    import tempfile

    from ..engine import analyze_file

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"track{suffix}"
        src.write_bytes(audio)
        return analyze_file(src, stages, options=options, signals=signals).to_dict()


def run_analyze_stream(audio: bytes, suffix: str, stages: list[str] | None = None, options: dict | None = None,
                       signals: bytes | None = None):
    """run_analyze, yielding {"stage", "i", "n"} as each stage starts and then {"card": ...}, for progress bars."""
    import queue
    import tempfile
    import threading

    from ..engine import analyze_file

    events: queue.Queue = queue.Queue()
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"track{suffix}"
        src.write_bytes(audio)

        def work():
            try:
                card = analyze_file(src, stages, options=options, signals=signals,
                                    on_stage=lambda s, i, n: events.put({"stage": s, "i": i, "n": n}))
                events.put({"card": card.to_dict()})
            except BaseException as e:  # noqa: BLE001 - raised again below, in the caller's thread
                events.put({"error": e})

        threading.Thread(target=work, daemon=True).start()
        while True:
            event = events.get()
            if "error" in event:
                raise event["error"]
            yield event
            if "card" in event:
                return


def run_stems(audio: bytes, suffix: str) -> dict[str, bytes]:
    """Separate one file's bytes with the local Demucs; returns {stem name: FLAC bytes}."""
    import tempfile

    from .. import stems

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"track{suffix}"
        src.write_bytes(audio)
        out = Path(tmp) / "stems"
        stems.separate(src, out)
        return {n: stems.stem_path(out, n).read_bytes() for n in stems.STEM_NAMES}
