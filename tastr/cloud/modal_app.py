"""Modal app for Tastr's heavy jobs. Deploy it once, and again after engine changes:

    TASTR_R2_ENDPOINT=https://<account id>.r2.cloudflarestorage.com modal deploy tastr/cloud/modal_app.py

`tastr bench --remote` then calls the deployed `bench` function, and the app's stem separation calls
`stems` when `TASTR_STEMS_BACKEND=modal`. Datasets are read from the R2 bucket,
mounted read-only at /data with the `tastr-r2` Modal secret. Setup steps are in docs/cloud.md.
"""

from __future__ import annotations

import os
import time

import modal

_IMPORTED = time.time()  # in the container: when it finished starting and imported this module

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
_torch_base = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "libsndfile1")
    .pip_install(*ENGINE_DEPS, *STEMS_DEPS, *PINS)
    .run_commands("python -c \"from demucs.pretrained import get_model; get_model('htdemucs')\"")
    .env({"NUMBA_CACHE_DIR": "/tmp/numba"})
)
stems_image = _torch_base.add_local_python_source("tastr")
# For the pretrained drum model (PyTorch comes with Demucs). Its weights download into /tmp on first use
# (about 5 MB, docs/drums.md); one torch thread per bench worker, since the 4 workers share the CPUs.
# Local files go last: Modal refuses build steps after them.
drums_image = _torch_base.env({"TASTR_HOME": "/tmp/tastr", "TASTR_DRUM_MODEL": "adtof", "OMP_NUM_THREADS": "1"}).add_local_python_source("tastr")
datasets = modal.CloudBucketMount(BUCKET, bucket_endpoint_url=ENDPOINT, read_only=True,
                                  secret=modal.Secret.from_name("tastr-r2"))
# Writable, for `fetch` only: it downloads datasets straight into the bucket.
datasets_rw = modal.CloudBucketMount(BUCKET, bucket_endpoint_url=ENDPOINT, read_only=False,
                                     secret=modal.Secret.from_name("tastr-r2"))
app = modal.App(APP_NAME)
# CPUs the bench container asks for. os.cpu_count() there reports the host's cores, not this
# reservation, so worker counts come from here.
BENCH_CPUS = 8


@app.function(image=image, cpu=BENCH_CPUS, memory=16384, timeout=3 * 3600, volumes={DATA: datasets})
def bench(dataset: str, path: str | None = None, stages: list[str] | None = None, limit: int | None = None,
          jobs: int | None = None) -> dict:
    called = time.time()
    from tastr.cloud.jobs import run_bench

    report = run_bench(dataset, path, DATA, stages=stages, limit=limit, jobs=jobs or BENCH_CPUS)
    report["timing"] |= {"container_started": _IMPORTED, "called": called, "import_seconds": round(time.time() - called - report["timing"]["load_seconds"] - report["seconds"], 1)}
    return report


@app.function(image=image, cpu=2, memory=4096, timeout=3600, volumes={DATA: datasets})
def spec_bench(bench: str, dataset: str, path: str, limit: int | None = None) -> dict:
    from tastr.cloud.jobs import run_spec_bench

    return run_spec_bench(bench, dataset, f"{DATA}/{path}", limit)


@app.function(image=image, cpu=BENCH_CPUS, memory=16384, timeout=3600, volumes={DATA: datasets})
def key_profiles(dataset: str, path: str, limit: int | None = None) -> dict:
    from tastr.cloud.jobs import run_key_profiles

    return run_key_profiles(dataset, path, DATA, limit=limit, jobs=BENCH_CPUS)


@app.function(image=image, cpu=4, memory=8192, timeout=3600, volumes={DATA: datasets})
def drum_bench(suite: str, path: str | None = None, limit: int | None = None, drums_only: bool = False,
               kits: str = "all") -> dict:
    from tastr.cloud.jobs import run_drum_bench

    return run_drum_bench(suite, f"{DATA}/{path}" if path else None, limit, jobs=4, drums_only=drums_only, kits=kits)


# The drum bench with the pretrained drum model (tastr[drums]).
@app.function(image=drums_image, cpu=4, memory=8192, timeout=3600, volumes={DATA: datasets})
def drum_bench_model(suite: str, path: str | None = None, limit: int | None = None, drums_only: bool = False,
                     kits: str = "all") -> dict:
    from tastr.cloud.jobs import run_drum_bench

    return run_drum_bench(suite, f"{DATA}/{path}" if path else None, limit, jobs=4, drums_only=drums_only, kits=kits)


@app.function(image=image, cpu=BENCH_CPUS, memory=16384, timeout=3600, volumes={DATA: datasets})
def vibe_bench(dataset: str, path: str, limit: int | None = None) -> dict:
    from tastr.cloud.jobs import run_vibe_bench

    return run_vibe_bench(dataset, path, DATA, limit=limit, jobs=BENCH_CPUS)


@app.function(image=image, cpu=2, memory=4096, timeout=6 * 3600, volumes={DATA: datasets_rw})
def fetch(dataset: str, path: str, archives: list[int] | None = None) -> dict:
    from tastr.cloud.fetch import fetch as run_fetch

    return run_fetch(dataset, f"{DATA}/{path}", archives)


@app.function(image=stems_image, gpu="T4", memory=8192, timeout=15 * 60)
def stems(audio: bytes, suffix: str) -> dict[str, bytes]:
    from tastr.cloud.jobs import run_stems

    return run_stems(audio, suffix)
