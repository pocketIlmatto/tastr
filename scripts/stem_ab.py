"""Listening test for stem separation settings: split a few tracks several ways on a Modal GPU (docs/stems-tuning.md).

    modal run scripts/stem_ab.py --audio-dir PLAYLIST/audio --out-dir stem-ab [--limit 5] [--gpu T4]

Each track is separated with every config in CONFIGS. The stems land in OUT/<track>/<letter>/, with the letters
shuffled per track so the listening is blind; OUT/answers.json says which letter is which config. OUT/timings.csv
has the GPU seconds per track and config but no letters, and the output never pairs a letter with a config (the
runs finish in shuffled order and every file gets the same date), so only answers.json gives the answers away.
The audio goes from this machine through Modal's temporary storage and back; nothing touches R2. Keep OUT out of
the repo (it holds your music).
"""

from __future__ import annotations

import csv
import json
import os
import random
from pathlib import Path

import modal

CONFIGS = {  # label: (model, shifts, overlap); "today" is what the app runs (tastr/stems.py)
    "today": ("htdemucs", 1, 0.25),
    "shifts2-overlap0.5": ("htdemucs", 2, 0.5),
    "ft": ("htdemucs_ft", 1, 0.25),
    "ft-shifts2-overlap0.5": ("htdemucs_ft", 2, 0.5),
}
AUDIO = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".aiff", ".aif", ".ogg", ".opus"}

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "libsndfile1")
    .pip_install("numpy<2.3", "scipy<1.16", "librosa==0.11.0", "numba==0.61.2", "llvmlite==0.44.0",
                 "soundfile>=0.12", "mutagen>=1.47", "demucs>=4.0.1")
    # Bake both models' weights in, so runs don't download them (htdemucs_ft is four models, about 320 MB).
    .run_commands("python -c \"from demucs.pretrained import get_model; get_model('htdemucs'); get_model('htdemucs_ft')\"")
    .env({"NUMBA_CACHE_DIR": "/tmp/numba"})
    .add_local_python_source("tastr")
)
app = modal.App("tastr-stem-ab")


@app.function(image=image, gpu="T4", memory=8192, timeout=30 * 60)
def separate(audio: bytes, suffix: str, model: str, shifts: int, overlap: float) -> tuple[dict[str, bytes], float]:
    import tempfile
    import time

    from tastr import stems

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"track{suffix}"
        src.write_bytes(audio)
        out = Path(tmp) / "stems"
        start = time.monotonic()
        stems.separate(src, out, model_name=model, shifts=shifts, overlap=overlap)
        seconds = time.monotonic() - start
        return {n: stems.stem_path(out, n).read_bytes() for n in stems.STEM_NAMES}, seconds


@app.local_entrypoint()
def main(audio_dir: str, out_dir: str = "stem-ab", limit: int = 5, gpu: str = "T4", seed: int = 0):
    tracks = sorted(p for p in Path(audio_dir).iterdir() if p.suffix.lower() in AUDIO)[:limit]
    if not tracks:
        raise SystemExit(f"No audio files in {audio_dir}")
    out, rng = Path(out_dir), random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    fn = separate.with_options(gpu=gpu)
    jobs, answers = [], {}
    for t in tracks:
        letters = list("ABCD"[: len(CONFIGS)])
        rng.shuffle(letters)
        answers[t.stem] = dict(zip(letters, CONFIGS))
        data = t.read_bytes()
        jobs += [(t, letter, label, (data, t.suffix.lower(), *CONFIGS[label])) for letter, label in zip(letters, CONFIGS)]
    rng.shuffle(jobs)  # so neither the output order nor file dates follow the configs
    print(f"{len(tracks)} tracks x {len(CONFIGS)} configs = {len(jobs)} runs on {gpu}")
    rows = []
    for (t, letter, label, _), (result, seconds) in zip(jobs, fn.starmap([j[3] for j in jobs])):
        folder = out / t.stem / letter
        folder.mkdir(parents=True, exist_ok=True)
        for name, flac in result.items():
            (folder / f"{name}.flac").write_bytes(flac)
        rows.append({"track": t.stem, "config": label, "gpu": gpu, "seconds": round(seconds, 1)})
        print(f"{len(rows)}/{len(jobs)} done")
    (out / "answers.json").write_text(json.dumps(answers, indent=2))
    with open(out / "timings.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["track"], list(CONFIGS).index(r["config"]))))
    stamp = max(p.stat().st_mtime for p in out.rglob("*"))
    for p in out.rglob("*"):
        os.utime(p, (stamp, stamp))
    from stem_ab_page import build  # noqa: PLC0415 - next to this script

    print(f"Stems in {out}/<track>/<letter>/. Open {build(out)} to listen before opening answers.json.")
