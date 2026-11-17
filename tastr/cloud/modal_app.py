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

from tastr.cloud.rust_image import with_rust_engine
from tastr.engine.bench.essentia_heads import download_command

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

# With the Rust engine's library (tastr/cloud/rust_image.py), which makes every card and runs every bench step.
_engine_base = (
    with_rust_engine(modal.Image.debian_slim(python_version="3.12").apt_install("ffmpeg", "libsndfile1"))
    .pip_install(*ENGINE_DEPS, *PINS, "zstandard")  # zstandard: `fetch acousticbrainz` reads .tar.zst dumps
    .env({"NUMBA_CACHE_DIR": "/tmp/numba"})
)
image = _engine_base.add_local_python_source("tastr")
# Research only (docs/essentia.md): Essentia (AGPL-3.0) and MTG's classifier weights (non-commercial only),
# downloaded at build time and never committed.
ESSENTIA_PIN = "essentia-tensorflow==2.1b6.dev1389"  # the last build with a Python 3.12 Linux wheel
essentia_image = (
    _engine_base.pip_install(ESSENTIA_PIN)
    .run_commands(download_command())
    .add_local_python_source("tastr")
)
# The same plus Demucs, with the htdemucs_ft weights baked in so a cold start does not download them.
_torch_base = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "libsndfile1")
    .pip_install(*ENGINE_DEPS, *STEMS_DEPS, *PINS)
    .run_commands("python -c \"from demucs.pretrained import get_model; get_model('htdemucs_ft')\"")
    .env({"NUMBA_CACHE_DIR": "/tmp/numba"})
)
stems_image = _torch_base.add_local_python_source("tastr")
# For the pretrained drum model (PyTorch comes with Demucs). Its weights download into /tmp on first use
# (about 5 MB, docs/drums.md); one torch thread per bench worker, since the 4 workers share the CPUs.
# Local files go last: Modal refuses build steps after them.
# The drum bench analyzes through the Rust engine, so this image builds it in (the stems image doesn't need it).
drums_image = (
    with_rust_engine(_torch_base)
    .env({"TASTR_HOME": "/tmp/tastr", "TASTR_DRUM_MODEL": "adtof", "OMP_NUM_THREADS": "1"})
    .add_local_python_source("tastr")
)
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
def bench(dataset: str, path: str | None = None, limit: int | None = None, jobs: int | None = None,
          rekordbox: str | None = None) -> dict:
    called = time.time()
    from tastr.cloud.jobs import run_bench

    report = run_bench(dataset, path, DATA, limit=limit, jobs=jobs or BENCH_CPUS, rekordbox=rekordbox)
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
               kits: str = "all", open_hats: str | None = None) -> dict:
    from tastr.cloud.jobs import run_drum_bench

    return run_drum_bench(suite, f"{DATA}/{path}" if path else None, limit, jobs=4, drums_only=drums_only, kits=kits,
                          open_hats=open_hats)


# The drum bench with the pretrained drum model (tastr[drums]).
@app.function(image=drums_image, cpu=4, memory=8192, timeout=3600, volumes={DATA: datasets})
def drum_bench_model(suite: str, path: str | None = None, limit: int | None = None, drums_only: bool = False,
                     kits: str = "all", open_hats: str | None = None) -> dict:
    from tastr.cloud.jobs import run_drum_bench

    return run_drum_bench(suite, f"{DATA}/{path}" if path else None, limit, jobs=4, drums_only=drums_only, kits=kits,
                          open_hats=open_hats)


@app.function(image=image, cpu=BENCH_CPUS, memory=16384, timeout=3600, volumes={DATA: datasets})
def vibe_bench(dataset: str, path: str, limit: int | None = None) -> dict:
    from tastr.cloud.jobs import run_vibe_bench

    return run_vibe_bench(dataset, path, DATA, limit=limit, jobs=BENCH_CPUS)


@app.function(image=essentia_image, cpu=4, memory=8192, timeout=3600, volumes={DATA: datasets})
def essentia_bench(dataset: str, path: str, limit: int | None, shard: int = 0, shards: int = 1) -> list[dict]:
    """One shard of the Essentia classifier bench: engine row + head predictions per clip."""
    from pathlib import Path

    from tastr.cloud.jobs import load_items
    from tastr.engine.bench import essentia_heads as eh

    folder = Path(DATA) / path
    items = eh.pick(eh.held_out(load_items(dataset, folder), dataset), limit)
    return eh.analyze(items[shard::shards], eh.extra_labels(folder))


@app.function(image=image, cpu=2, memory=4096, timeout=6 * 3600, volumes={DATA: datasets_rw})
def fetch(dataset: str, path: str, archives: str = "") -> dict:
    """`archives`: a list of ints from `tastr fetch --remote`, or "0,1,2" from `modal run`, whose CLI can't
    parse a list annotation."""
    from tastr.cloud.fetch import fetch as run_fetch

    if isinstance(archives, str):
        archives = [int(a) for a in archives.split(",") if a.strip()] or None
    return run_fetch(dataset, f"{DATA}/{path}", archives)


@app.function(image=stems_image, gpu="T4", memory=8192, timeout=15 * 60)
def stems(audio: bytes | str, suffix: str, put_urls: dict[str, str] | None = None) -> dict:
    from tastr.cloud.jobs import run_stems

    return run_stems(audio, suffix, put_urls)


# Lyrics from the vocal stem (tastr/transcribe.py, an experiment): faster-whisper on CUDA 12 with cuDNN 9, and
# Whisper large-v3-turbo's weights (MIT, about 1.6 GB) baked in so a cold start doesn't download them.
WHISPER_DEPS = ["faster-whisper>=1.1.1", "av>=11,<14.3"]  # the `transcribe` extra; newer PyAV breaks it
whisper_image = (
    modal.Image.from_registry("nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04", add_python="3.12")
    .apt_install("ffmpeg")
    .pip_install(*WHISPER_DEPS)
    .run_commands("python -c \"from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu')\"")
    .add_local_python_source("tastr")
)


# 4-8 s of T4 time for a vocal stem in the trial (2026-11-17), plus loading the model on a cold start.
@app.function(image=whisper_image, gpu="T4", memory=8192, timeout=15 * 60)
def transcribe(audio: bytes, suffix: str, language: str | None = None) -> dict:
    from tastr.cloud.jobs import run_transcribe

    return run_transcribe(audio, suffix, language)


@app.local_entrypoint()
def transcribe_files(files: str, out: str, separate: bool = True, language: str = ""):
    """Try Whisper lyrics on a few files without deploying anything (tastr/transcribe.py):

        modal run tastr/cloud/modal_app.py::transcribe_files --files "a.flac,b.m4a" --out ~/tastr-whisper-try

    Each file is a full track, separated here into stems with `stems` (skip that with --no-separate when the files
    are vocal stems already). What Whisper heard goes to <out>/<file>.json, with LRCLIB's lyrics for the same track
    when it has them and the score against them; only the numbers are printed. Keep <out> out of the repository:
    it holds the songs' words.
    """
    import json
    from pathlib import Path

    from tastr import transcribe as tr

    dest = Path(out).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    rows = []
    for f in [Path(x.strip()).expanduser() for x in files.split(",") if x.strip()]:
        audio, suffix, sep_seconds = f.read_bytes(), f.suffix, None
        if separate:
            t0 = time.time()
            audio, suffix = stems.remote(audio, suffix)["vocals"], ".flac"
            sep_seconds = round(time.time() - t0, 1)
        t0 = time.time()
        heard = transcribe.remote(audio, suffix, language or None)
        heard["call_seconds"] = round(time.time() - t0, 1)
        ref = _lrclib_for(f)
        row = {"file": f.name, "separate_seconds": sep_seconds, "audio_seconds": heard["audio_seconds"],
               "load_seconds": heard["load_seconds"], "whisper_seconds": heard["seconds"],
               "call_seconds": heard["call_seconds"], "language": heard["language"], "lines": len(heard["lines"]),
               "lrclib": ref.get("status")}
        if ref.get("status") == "found":
            row |= tr.score(heard, ref.get("plain"), ref.get("synced"))
        rows.append(row)
        (dest / f"{f.stem}.json").write_text(json.dumps({"row": row, "heard": heard, "lrclib": ref}, indent=1))
        print(json.dumps(row))
    (dest / "summary.json").write_text(json.dumps(rows, indent=1))


def _lrclib_for(f) -> dict:
    """LRCLIB's lyrics for a file, found by its artist, title, album and length tags."""
    import mutagen

    from tastr import lyrics
    from tastr.lookup.base import ProviderError, TrackQuery

    m = mutagen.File(str(f), easy=True)
    tags = {k: (v[0] if isinstance(v, list) and v else v) for k, v in (m.tags or {}).items()} if m else {}
    if not (tags.get("artist") and tags.get("title")):
        return {"status": "no_tags"}
    q = TrackQuery(artist=tags["artist"], title=tags["title"], album=tags.get("album"),
                   duration=getattr(m.info, "length", None))
    try:
        return lyrics.LRCLIB_PROVIDER.lookup(q)
    except ProviderError as e:
        return {"status": "error", "error": str(e)}


@app.local_entrypoint()
def run_branch(kind: str, dataset: str, path: str, limit: int = 0, out: str = "", drum_model: bool = False,
               open_hats: str = "", drums_only: bool = False, kits: str = "all"):
    """Bench a branch's code without deploying it, so the shared app stays as is:

        modal run tastr/cloud/modal_app.py --kind bench --dataset beatport-tempo --path beatport-key
        modal run tastr/cloud/modal_app.py --kind tempo --dataset hjdb --path beat-this   # any spec-bench
        modal run tastr/cloud/modal_app.py --kind drums --dataset idmt --path idmt-smt-drums --limit 10
        modal run tastr/cloud/modal_app.py --kind drums --dataset mdb --path mdb-drums --drum-model --open-hats model
        modal run tastr/cloud/modal_app.py --kind drums-model --dataset mdb --path mdb-drums   # with the drum model
        modal run tastr/cloud/modal_app.py --kind drums-model-stems --dataset mdb --path mdb-drums   # MDB drum stems
        modal run tastr/cloud/modal_app.py --kind drums-model-test-kits --dataset stemgmd-mix --path stemgmd-single-hits
        modal run tastr/cloud/modal_app.py --kind key-profiles --dataset giantsteps-key --path giantsteps-key

    `modal run` uploads the working tree to a temporary app that stops when the run ends.
    """
    import json

    if kind == "essentia":  # modal run tastr/cloud/modal_app.py --kind essentia --dataset deam --path deam --limit 200
        from tastr.engine.bench import essentia_heads as eh

        shards = 8
        rows = [r for part in essentia_bench.starmap([(dataset, path, limit or None, i, shards) for i in range(shards)])
                for r in part]
        report = {"dataset": dataset, "report": eh.score(rows), "rows": rows}
        if out:
            with open(out, "w") as f:
                f.write(json.dumps(report, indent=1))
        print(eh.format_report(report["report"]))
        return
    if kind == "bench":
        report = bench.remote(dataset, path, limit or None)
    elif kind == "drums":
        fn = drum_bench_model if drum_model else drum_bench
        report = fn.remote(dataset, path if path != "-" else None, limit or None, drums_only, kits, open_hats or None)
    elif kind.startswith("drums-model"):  # -stems: MDB drum stems; -test-kits: the held-out StemGMD kits
        report = drum_bench_model.remote(dataset, path if path != "-" else None, limit or None,
                                         drums_only=kind.endswith("-stems"),
                                         kits="test" if kind.endswith("-test-kits") else "all",
                                         open_hats=open_hats or None)
    elif kind == "key-profiles":
        report = key_profiles.remote(dataset, path, limit or None)
    else:
        report = spec_bench.remote(kind, dataset, path, limit or None)
    text = json.dumps(report, indent=1)
    if out:
        with open(out, "w") as f:
            f.write(text)
    summary = report.get("summary", {k: v for k, v in report.items() if k not in ("tracks", "rows")})
    print(json.dumps(summary, indent=1)[:4000])
