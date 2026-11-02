"""Modal app for a hosted Tastr server (docs/deploy.md): track analysis and stem separation, nothing else.

The web server is small (it fits in 512 MB) because the engine runs here: each upload is sent to `analyze`
(as its bytes, or with the audio bucket on (tastr/blobs.py) as a signed link that the function downloads from),
which returns the card, "Detect drums with the model" calls `analyze_drums` (the same analysis plus the
pretrained drum model, in its own image so everyday analysis doesn't load PyTorch), and "Separate stems"
calls `stems`. Kept apart from modal_app.py (benchmarks), so
it needs no dataset bucket or secrets and can live in its own Modal environment with its own budget:

    modal deploy --env friends tastr/cloud/modal_web.py

The server finds it with TASTR_MODAL_APP=tastr-web and MODAL_ENVIRONMENT=friends. A server without the Modal
client (the Cloudflare Worker, TAS-84) calls the same functions through `api`, an HTTPS endpoint behind a Modal
proxy auth token, at TASTR_MODAL_URL (tastr/cloud/remote.py). The deploy workflow
(.github/workflows/deploy.yml) deploys it together with the web app, so the two run the same engine.
"""

from __future__ import annotations

import modal

from tastr.cloud.rust_image import with_rust_engine

APP_NAME = "tastr-web"

# Same as modal_app.py (tests/test_cloud.py keeps them in step).
ENGINE_DEPS = ["numpy>=1.24", "scipy>=1.10", "librosa>=0.10", "soundfile>=0.12", "mutagen>=1.47"]
STEMS_DEPS = ["demucs>=4.0.1"]
# CPU-only PyTorch for the drum model: a few hundred MB instead of the 2 GB CUDA build.
TORCH_CPU = ("torch>=2.1", "https://download.pytorch.org/whl/cpu")
PINS = ["numpy<2.3", "numba==0.61.2", "llvmlite==0.44.0", "librosa==0.11.0", "scipy<1.16"]
# Numba keeps compiled functions here; the image build fills it (see _warm), so a cold start skips compiling.
NUMBA_CACHE = "/root/numba-cache"


def _warm() -> None:
    """Analyze a short synthetic track at image build time, so numba's compiled code ships in the image."""
    import tempfile
    from pathlib import Path

    import numpy as np
    import soundfile as sf

    from tastr.engine import SR, analyze_file

    t = np.arange(20 * SR) / SR
    y = 0.3 * np.sin(2 * np.pi * 110 * t) + (np.mod(t, 0.5) < 0.01) * 0.8  # a bass tone and a click every beat
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "warm.wav"
        sf.write(path, y.astype(np.float32), SR)
        analyze_file(path)


def _warm_drums() -> None:
    """Download the drum model's weights into the image (checked by SHA-256), then warm up as above."""
    from tastr.engine import adt

    adt.fetch()
    _warm()


# Both analysis images build in the Rust engine's library (tastr/cloud/rust_image.py), which makes every card;
# the warm-up at build time fails the deploy if it can't.
image = (
    with_rust_engine(modal.Image.debian_slim(python_version="3.12").apt_install("ffmpeg", "libsndfile1"))
    .pip_install(*ENGINE_DEPS, *PINS)
    .env({"NUMBA_CACHE_DIR": NUMBA_CACHE})
    .add_local_python_source("tastr", copy=True)  # copied, so the warm-up below can run against it
    .run_function(_warm)
)
# The drum model's weights are CC BY-NC-SA 4.0 (docs/drums.md); they live in this private image only.
drums_image = (
    with_rust_engine(modal.Image.debian_slim(python_version="3.12").apt_install("ffmpeg", "libsndfile1"))
    .pip_install(*ENGINE_DEPS, *PINS)
    .pip_install(TORCH_CPU[0], index_url=TORCH_CPU[1])
    .env({"NUMBA_CACHE_DIR": NUMBA_CACHE, "TASTR_HOME": "/root/tastr-home"})
    .add_local_python_source("tastr", copy=True)
    .run_function(_warm_drums)
)
stems_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "libsndfile1")
    .pip_install(*ENGINE_DEPS, *STEMS_DEPS, *PINS)
    .run_commands("python -c \"from demucs.pretrained import get_model; get_model('htdemucs')\"")
    .env({"NUMBA_CACHE_DIR": "/tmp/numba"})
    .add_local_python_source("tastr")
)
app = modal.App(APP_NAME)


# About 240 MB at peak for a 3-minute track with the Rust engine (66 MB a minute; Python took 810 MB). A container stays warm 1 minute after a request: enough for the
# uploads of one batch to share a cold start, without paying for minutes of idle time after every upload.
@app.function(image=image, cpu=2, memory=2048, timeout=10 * 60, scaledown_window=60)
def analyze(audio: bytes | str, suffix: str, stages: list[str] | None = None, options: dict | None = None) -> dict:
    from tastr.cloud.jobs import run_analyze

    return run_analyze(audio, suffix, stages, options)


# The same, reporting each stage as it starts (the app's progress bars): yields {"stage", "i", "n"}, then {"card"}.
@app.function(image=image, cpu=2, memory=2048, timeout=10 * 60, scaledown_window=60)
def analyze_stream(audio: bytes | str, suffix: str, stages: list[str] | None = None, options: dict | None = None):
    from tastr.cloud.jobs import run_analyze_stream

    yield from run_analyze_stream(audio, suffix, stages, options)


# The analysis above plus the drum model, only when someone asks for it on a track.
@app.function(image=drums_image, cpu=2, memory=4096, timeout=15 * 60)
def analyze_drums(audio: bytes | str, suffix: str, stages: list[str] | None = None, options: dict | None = None) -> dict:
    from tastr.cloud.jobs import run_analyze

    return run_analyze(audio, suffix, stages, options)


@app.function(image=stems_image, gpu="T4", memory=8192, timeout=15 * 60)
def stems(audio: bytes | str, suffix: str, put_urls: dict[str, str] | None = None) -> dict:
    from tastr.cloud.jobs import run_stems

    return run_stems(audio, suffix, put_urls)


# A small HTTPS front for the functions above, for servers that can't run the Modal client (the Cloudflare
# Worker). Only callers with a proxy auth token of this workspace get in (headers Modal-Key and Modal-Secret).
# Every answer is lines of JSON, streamed: a streaming call's events, then {"result": ...} or {"error": ...};
# a blank line every 20 seconds keeps a long call (a drum model run, stems) from timing out on the way.
api_image = modal.Image.debian_slim(python_version="3.12").pip_install("fastapi[standard]").add_local_python_source("tastr")
KEEPALIVE = 20


@app.function(image=api_image, cpu=0.25, memory=256, timeout=20 * 60, scaledown_window=60)
@modal.concurrent(max_inputs=20)
@modal.asgi_app(requires_proxy_auth=True)
def api():
    import asyncio
    import base64
    import json

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse, StreamingResponse

    web = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def line(event: dict) -> str:
        return json.dumps(event) + "\n"

    async def answer(call):
        """Run `call()` (a coroutine), with keep-alive lines until it ends."""
        task = asyncio.ensure_future(call())
        while True:
            done, _ = await asyncio.wait({task}, timeout=KEEPALIVE)
            if done:
                break
            yield "\n"
        try:
            result = task.result()
        except Exception as e:  # noqa: BLE001 - the caller gets the message
            yield line({"error": f"{type(e).__name__}: {e}"})
            return
        if isinstance(result, dict):  # stems without upload links: the FLAC bytes, base64-encoded
            result = {k: base64.b64encode(v).decode() if isinstance(v, bytes) else v for k, v in result.items()}
        yield line({"result": result})

    async def stream(args):
        try:
            async for event in analyze_stream.remote_gen.aio(*args):
                yield line({"result": event["card"]} if "card" in event else event)
        except Exception as e:  # noqa: BLE001
            yield line({"error": f"{type(e).__name__}: {e}"})

    async def call(request):  # a plain route: this module's postponed annotations would hide Request from FastAPI
        name = request.path_params["name"]
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"detail": "a JSON object"}, status_code=422)
        audio = body.get("audio_url") or base64.b64decode(body.get("audio_b64") or "")
        if not audio or not isinstance(body.get("suffix"), str):
            return JSONResponse({"detail": "audio_url or audio_b64, and suffix"}, status_code=422)
        args = (audio, body["suffix"], None, body.get("options"))
        if name == "analyze_stream":
            out = stream(args)
        elif name == "analyze":
            out = answer(lambda: analyze.remote.aio(*args))
        elif name == "analyze_drums":
            out = answer(lambda: analyze_drums.remote.aio(*args))
        elif name == "stems":
            out = answer(lambda: stems.remote.aio(audio, body["suffix"], body.get("put_urls")))
        else:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return StreamingResponse(out, media_type="application/x-ndjson")

    web.add_route("/{name}", call, methods=["POST"])
    return web
