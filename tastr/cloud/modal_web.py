"""Modal app for a hosted Tastr server (docs/deploy.md): track analysis and stem separation, nothing else.

The web server is small (it fits in 512 MB) because the engine runs here: each upload is sent to `analyze`,
which returns the card, "Detect drums with the model" calls `analyze_drums` (the same analysis plus the
pretrained drum model, in its own image so everyday analysis doesn't load PyTorch), and "Separate stems"
calls `stems`. Kept apart from modal_app.py (benchmarks), so
it needs no dataset bucket or secrets and can live in its own Modal environment with its own budget:

    modal deploy --env friends tastr/cloud/modal_web.py

The server finds it with TASTR_MODAL_APP=tastr-web and MODAL_ENVIRONMENT=friends. The deploy workflow
(.github/workflows/deploy.yml) deploys it together with the web app, so the two run the same engine.
"""

from __future__ import annotations

import modal

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


image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "libsndfile1")
    .pip_install(*ENGINE_DEPS, *PINS)
    .env({"NUMBA_CACHE_DIR": NUMBA_CACHE})
    .add_local_python_source("tastr", copy=True)  # copied, so the warm-up below can run against it
    .run_function(_warm)
)
# The drum model's weights are CC BY-NC-SA 4.0 (docs/drums.md); they live in this private image only.
drums_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "libsndfile1")
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


# About 810 MB at peak for a 3-minute track. A container stays warm 1 minute after a request: enough for the
# uploads of one batch to share a cold start, without paying for minutes of idle time after every upload.
@app.function(image=image, cpu=2, memory=2048, timeout=10 * 60, scaledown_window=60)
def analyze(audio: bytes, suffix: str, stages: list[str] | None = None, options: dict | None = None) -> dict:
    from tastr.cloud.jobs import run_analyze

    return run_analyze(audio, suffix, stages, options)


# The same, reporting each stage as it starts (the app's progress bars): yields {"stage", "i", "n"}, then {"card"}.
@app.function(image=image, cpu=2, memory=2048, timeout=10 * 60, scaledown_window=60)
def analyze_stream(audio: bytes, suffix: str, stages: list[str] | None = None, options: dict | None = None):
    from tastr.cloud.jobs import run_analyze_stream

    yield from run_analyze_stream(audio, suffix, stages, options)


# The analysis above plus the drum model, only when someone asks for it on a track.
@app.function(image=drums_image, cpu=2, memory=4096, timeout=15 * 60)
def analyze_drums(audio: bytes, suffix: str, stages: list[str] | None = None, options: dict | None = None) -> dict:
    from tastr.cloud.jobs import run_analyze

    return run_analyze(audio, suffix, stages, options)


@app.function(image=stems_image, gpu="T4", memory=8192, timeout=15 * 60)
def stems(audio: bytes, suffix: str) -> dict[str, bytes]:
    from tastr.cloud.jobs import run_stems

    return run_stems(audio, suffix)
