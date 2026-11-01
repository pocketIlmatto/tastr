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


def analyze(audio_path: str | Path, options: dict | None = None, on_stage=None):
    """Analyze one file on Modal; returns its TrackCard (whose source the caller sets). With the drum model
    asked for ({"drum_model": True}), it runs in `analyze_drums`, whose image has PyTorch and the weights.

    Otherwise, with `on_stage`, it calls `analyze_stream`, which reports each stage as it starts; an app
    deployed before that function existed falls back to `analyze`, without stages."""
    from ..engine.card import TrackCard

    audio_path = Path(audio_path)
    args = (_audio(audio_path), audio_path.suffix, None, options)  # None: the stages, which the engine no longer takes
    if (options or {}).get("drum_model"):
        return TrackCard.from_dict(_call("analyze_drums", *args))
    if on_stage:
        try:
            for event in _call_gen("analyze_stream", *args):
                if "card" in event:
                    return TrackCard.from_dict(event["card"])
                on_stage(event["stage"], event.get("i", 0), event.get("n", 1))
            raise RemoteError("analyze_stream ended without a card")
        except _NotDeployed:
            # An older deployment without the streaming function: fall back to the plain call, without progress.
            pass
    return TrackCard.from_dict(_call("analyze", *args))


class _NotDeployed(RemoteError):
    pass


def _call_gen(name: str, *args):
    """Like _call, for a generator function: yields what it yields."""
    try:
        import modal
    except ImportError as e:
        raise RemoteError("running on Modal needs it installed: pip install 'tastr[cloud]' (setup in docs/cloud.md)") from e
    try:
        fn = modal.Function.from_name(app_name(), name)
        fn.hydrate()  # finds out now, before any work, whether the function exists
    except modal.exception.NotFoundError as e:
        raise _NotDeployed(name) from e
    yield from fn.remote_gen(*args)


def _audio(audio_path: Path) -> bytes | str:
    """What a Modal function gets for the audio: a signed link when the file is in the audio bucket (Modal
    downloads it from there, tastr/blobs.py), else the file's bytes."""
    from .. import blobs

    if blobs.in_bucket(audio_path):
        return blobs.signed_url(audio_path)
    local = blobs.ensure(audio_path) or audio_path
    return local.read_bytes()


def stems(audio_path: str | Path, put_urls: dict[str, str] | None = None) -> dict:
    """Separate one file with the Modal `stems` function. Returns {stem name: FLAC bytes} (tastr/stems.py
    writes them), or with `put_urls` ({stem name: signed upload link}, the audio bucket) Modal uploads each
    stem itself and returns {stem name: bytes written}."""
    audio_path = Path(audio_path)
    if put_urls:
        return _call("stems", _audio(audio_path), audio_path.suffix, put_urls)
    return _call("stems", _audio(audio_path), audio_path.suffix)
