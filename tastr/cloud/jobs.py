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


def write_index(dataset: str, folder: str | Path) -> Path:
    """Run the dataset's loader on a local folder and save its items, with paths relative to it."""
    from ..engine import bench

    folder = Path(folder).expanduser().resolve()
    items = bench.load(dataset, str(folder))
    if not items:
        raise ValueError(f"no labeled tracks with audio found for {dataset} at {folder}")
    rows = [{"id": it.id, "path": str(Path(it.path).relative_to(folder)), "truth": asdict(it.truth),
             "meta": it.meta} for it in items]
    out = folder / index_name(dataset)
    out.write_text(json.dumps({"dataset": dataset, "items": rows}))
    return out


def load_items(dataset: str, folder: Path) -> list:
    """The folder's index when it has one for this dataset, else the dataset's loader."""
    from ..engine import bench
    from ..engine.bench.datasets import Item, Truth

    for index in (folder / index_name(dataset), folder / "tastr-index.json"):  # the second: #61's name
        if not index.is_file():
            continue
        data = json.loads(index.read_text())
        if data.get("dataset") == dataset:
            return [Item(id=r["id"], path=str(folder / r["path"]), truth=Truth(**r["truth"]), meta=r.get("meta", {}))
                    for r in data["items"]]
    return bench.load(dataset, str(folder))


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
