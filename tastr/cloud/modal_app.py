"""Modal app for Tastr's heavy jobs. Deploy it once, and again after engine changes:

    TASTR_R2_ENDPOINT=https://<account id>.r2.cloudflarestorage.com modal deploy tastr/cloud/modal_app.py

`tastr bench --remote` then calls the deployed `bench` function, and the app's stem separation calls
`stems` when `TASTR_STEMS_BACKEND=modal`. Datasets are read from the R2 bucket,
mounted read-only at /data with the `tastr-r2` Modal secret. Setup steps are in docs/cloud.md.
"""

from __future__ import annotations

import os

import modal

APP_NAME = "tastr"
DATA = "/data"
BUCKET = os.environ.get("TASTR_R2_BUCKET", "tastr-data")
ENDPOINT = os.environ.get("TASTR_R2_ENDPOINT", "")
if modal.is_local() and not ENDPOINT:
    raise SystemExit("set TASTR_R2_ENDPOINT to https://<account id>.r2.cloudflarestorage.com before deploying")

# The engine's runtime dependencies from pyproject.toml, without the web server (tests/test_cloud.py
# checks they stay in step).
ENGINE_DEPS = ["numpy>=1.24", "scipy>=1.10", "librosa>=0.10", "soundfile>=0.12", "mutagen>=1.47"]
STEMS_DEPS = ["demucs>=4.0.1"]  # the `stems` extra
# Versions known to work together (docs/bench.md): librosa 1.0 with numba 0.68 and numpy 2.5 segfaults.
PINS = ["numpy<2.3", "numba==0.61.2", "llvmlite==0.44.0", "librosa==0.11.0", "scipy<1.16"]

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "libsndfile1")
    .pip_install(*ENGINE_DEPS, *PINS)
    .env({"NUMBA_CACHE_DIR": "/tmp/numba"})
    .add_local_python_source("tastr")
)
# The same plus Demucs, with the htdemucs weights baked in so a cold start does not download them.
stems_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "libsndfile1")
    .pip_install(*ENGINE_DEPS, *STEMS_DEPS, *PINS)
    .run_commands("python -c \"from demucs.pretrained import get_model; get_model('htdemucs')\"")
    .env({"NUMBA_CACHE_DIR": "/tmp/numba"})
    .add_local_python_source("tastr")
)
datasets = modal.CloudBucketMount(BUCKET, bucket_endpoint_url=ENDPOINT, read_only=True,
                                  secret=modal.Secret.from_name("tastr-r2"))
app = modal.App(APP_NAME)


@app.function(image=image, cpu=8, memory=16384, timeout=3 * 3600, volumes={DATA: datasets})
def bench(dataset: str, path: str | None = None, stages: list[str] | None = None, limit: int | None = None,
          jobs: int | None = None) -> dict:
    from tastr.cloud.jobs import run_bench

    return run_bench(dataset, path, DATA, stages=stages, limit=limit, jobs=jobs or os.cpu_count() or 1)


@app.function(image=stems_image, gpu="T4", memory=8192, timeout=15 * 60)
def stems(audio: bytes, suffix: str) -> dict[str, bytes]:
    from tastr.cloud.jobs import run_stems

    return run_stems(audio, suffix)
