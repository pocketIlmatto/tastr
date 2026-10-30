"""Call the deployed Modal functions from the CLI and the app.

TASTR_MODAL_APP picks the deployed app: `tastr` (modal_app.py, benchmarks and stems) by default, or
`tastr-web` (modal_web.py, analysis and stems for a hosted server).
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "tastr"  # modal_app.APP_NAME; that module needs deploy-time settings to import


class RemoteError(RuntimeError):
    pass


def _call(name: str, *args, **kw):
    try:
        import modal
    except ImportError as e:
        raise RemoteError("running on Modal needs it installed: pip install 'tastr[cloud]' (setup in docs/cloud.md)") from e
    try:
        fn = modal.Function.from_name(app_name(), name)
        return fn.remote(*args, **kw)
    except modal.exception.NotFoundError as e:
        raise RemoteError(f"the {app_name()} app isn't deployed on Modal yet: see docs/cloud.md") from e


def app_name() -> str:
    return os.environ.get("TASTR_MODAL_APP", APP_NAME)


def bench(dataset: str, path: str | None, **kw) -> dict:
    return _call("bench", dataset, path, **kw)


def spec_bench(bench: str, dataset: str, path: str, limit: int | None = None) -> dict:
    return _call("spec_bench", bench, dataset, path, limit)


def key_profiles(dataset: str, path: str, limit: int | None = None) -> dict:
    return _call("key_profiles", dataset, path, limit)


def drum_bench(suite: str, path: str | None, limit: int | None = None, drums_only: bool = False,
               kits: str = "all", model: bool = False) -> dict:
    return _call("drum_bench_model" if model else "drum_bench", suite, path, limit, drums_only, kits)


def vibe_bench(dataset: str, path: str, limit: int | None = None) -> dict:
    return _call("vibe_bench", dataset, path, limit)


def fetch(dataset: str, path: str, archives: list[int] | None = None) -> dict:
    return _call("fetch", dataset, path, archives)


def analyze(audio_path: str | Path, stages: list[str] | None = None, options: dict | None = None):
    """Analyze one file on Modal; returns its TrackCard (whose source the caller sets)."""
    from ..engine.card import TrackCard

    audio_path = Path(audio_path)
    return TrackCard.from_dict(_call("analyze", audio_path.read_bytes(), audio_path.suffix, stages, options))


def stems(audio_path: str | Path, out_dir: Path, progress=None) -> None:
    """Upload one file to the Modal `stems` function and write the FLAC stems it returns into `out_dir`."""
    from .. import stems as local

    audio_path = Path(audio_path)
    if progress:
        progress(0.05)  # Modal reports no progress; show that the job has started
    result = _call("stems", audio_path.read_bytes(), audio_path.suffix)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, data in result.items():
        if name in local.STEM_NAMES:
            local.stem_path(out_dir, name).write_bytes(data)
    if progress:
        progress(1.0)
