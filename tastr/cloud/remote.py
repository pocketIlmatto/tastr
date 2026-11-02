"""Call the deployed Modal functions from the CLI and the app.

TASTR_MODAL_APP picks the deployed app: `tastr` (modal_app.py, benchmarks and stems) by default, or
`tastr-web` (modal_web.py, analysis and stems for a hosted server).

With TASTR_MODAL_URL set (the `api` web endpoint of modal_web.py), analysis and stems go over HTTPS instead of
through the Modal client, which doesn't run on the Cloudflare Worker (worker/, TAS-84). The endpoint needs a
Modal proxy auth token: TASTR_MODAL_KEY and TASTR_MODAL_SECRET. Each call answers with lines of JSON: the events
of a streaming call, then {"result": ...} or {"error": "..."}. `use_transport` swaps how the request is sent
(the Worker sends it with its own fetch).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Iterator

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


def http_enabled() -> bool:
    return bool(os.environ.get("TASTR_MODAL_URL"))


def available() -> bool:
    """Whether this server can call Modal at all: over HTTPS, or with the Modal client installed."""
    import importlib.util

    return http_enabled() or importlib.util.find_spec("modal") is not None


def _urllib_post(url: str, data: bytes, headers: dict[str, str]) -> Iterable[bytes]:
    """POST and yield the answer's body as it arrives; raises RemoteError for an HTTP error."""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=20 * 60)  # noqa: S310 - our own Modal endpoint
    except urllib.error.HTTPError as e:
        raise RemoteError(f"Modal answered {e.code}: {e.read()[:200].decode(errors='replace')}") from e
    except OSError as e:
        raise RemoteError(f"couldn't reach Modal: {e}") from e
    with resp:
        while chunk := resp.read1(65536):
            yield chunk


_transport: Callable[[str, bytes, dict[str, str]], Iterable[bytes]] = _urllib_post


def use_transport(post: Callable[[str, bytes, dict[str, str]], Iterable[bytes]] | None) -> None:
    """`post(url, body, headers)` yields the answer's body in chunks and raises RemoteError for an HTTP error.
    None: back to urllib."""
    global _transport
    _transport = post or _urllib_post


def _http(name: str, payload: dict) -> Iterator[dict]:
    """Call `name` on the web endpoint; yields its events, the last one {"result": ...}."""
    url = f"{os.environ['TASTR_MODAL_URL'].rstrip('/')}/{name}"
    headers = {"Content-Type": "application/json", "Modal-Key": os.environ.get("TASTR_MODAL_KEY", ""),
               "Modal-Secret": os.environ.get("TASTR_MODAL_SECRET", "")}
    buf = b""
    for chunk in _transport(url, json.dumps(payload).encode(), headers):
        buf += chunk
        *lines, buf = buf.split(b"\n")
        for line in lines:
            if line.strip():  # blank lines keep a long call's connection open
                event = json.loads(line)
                if "error" in event:
                    raise RemoteError(event["error"])
                yield event
                if "result" in event:
                    return
    raise RemoteError(f"Modal's {name} ended without an answer")


def _http_result(name: str, payload: dict):
    for event in _http(name, payload):
        if "result" in event:
            return event["result"]


def _payload(audio: bytes | str, suffix: str, **kw) -> dict:
    """The audio as a link (the audio bucket's signed link) or, base64-encoded, its bytes."""
    where = {"audio_url": audio} if isinstance(audio, str) else {"audio_b64": base64.b64encode(audio).decode()}
    return where | {"suffix": suffix} | kw


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
    if http_enabled():
        name = "analyze_drums" if (options or {}).get("drum_model") else "analyze_stream"
        for event in _http(name, _payload(_audio(audio_path), audio_path.suffix, options=options)):
            if "result" in event:
                return TrackCard.from_dict(event["result"])
            if on_stage and "stage" in event:
                on_stage(event["stage"], event.get("i", 0), event.get("n", 1))
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
    if http_enabled():
        result = _http_result("stems", _payload(_audio(audio_path), audio_path.suffix, put_urls=put_urls))
        return {k: base64.b64decode(v) if isinstance(v, str) else v for k, v in result.items()}
    if put_urls:
        return _call("stems", _audio(audio_path), audio_path.suffix, put_urls)
    return _call("stems", _audio(audio_path), audio_path.suffix)
