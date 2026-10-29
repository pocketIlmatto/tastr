"""Job bodies that the Modal functions call. Plain Python, so tests run them locally."""

from __future__ import annotations

from pathlib import Path


def run_bench(dataset: str, path: str | None, root: str | Path, stages: list[str] | None = None,
              limit: int | None = None, jobs: int = 1, progress=None) -> dict:
    """`tastr bench` with `path` relative to `root` (the bucket mount on Modal). Returns the report."""
    from ..engine import bench
    from ..engine.bench import runner

    items = bench.load(dataset, str(Path(root) / path) if path else None)
    if not items:
        raise ValueError(f"no labeled tracks with audio found for {dataset} at {path} in the bucket")
    return runner.run(items, stages=stages, jobs=jobs, limit=limit, progress=progress)


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
