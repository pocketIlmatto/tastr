"""Listening test for stem separation settings: split a few tracks several ways on a Modal GPU (docs/stems-tuning.md).

    modal run scripts/stem_ab.py --audio-dir PLAYLIST/audio --out-dir stem-ab [--limit 5] [--gpu T4] [--set six]

Each track is separated with every config in CONFIGS (`--set six`: SIX, three ways to get guitar and piano stems). The stems land in OUT/<track>/<letter>/, with the letters
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
# Six stems (vocals, drums, bass, guitar, piano, other), three ways. "roformer-sw" is a quality yardstick only: its
# weights have no clear licence or known trainer, so the app can't ship it (docs/stems-tuning.md).
SIX = {
    "6s": "htdemucs_6s alone",
    "hybrid": "htdemucs_ft for vocals, drums and bass; htdemucs_6s for guitar and piano; other = mix minus the five",
    "roformer-sw": "BS-RoFormer SW 6-stem (bs-roformer-infer 0.1.5)",
}
SIX_STEMS = ("vocals", "drums", "bass", "guitar", "piano", "other")
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
# The 6-stem runs add htdemucs_6s and the BS-RoFormer SW checkpoint (about 700 MB, sha256-checked by the package).
image6 = (
    image.pip_install("bs-roformer-infer==0.1.5")
    .run_commands("python -c \"from demucs.pretrained import get_model; get_model('htdemucs_6s')\"",
                  "python -c \"from bs_roformer import DEFAULT_MODEL, ensure_model_assets; ensure_model_assets(DEFAULT_MODEL)\"")
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


def _demucs(model_name: str, mix, device: str) -> dict:
    """Separate `mix` (channels, samples at 44.1 kHz, float32) with a Demucs model, as tastr/stems.py does."""
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    model = get_model(model_name)
    model.eval()
    x = torch.from_numpy(mix)
    ref = x.mean(0)
    x = (x - ref.mean()) / (ref.std() + 1e-8)
    with torch.no_grad():
        out = apply_model(model, x[None], shifts=1, overlap=0.25, device=device)[0]
    out = out * ref.std() + ref.mean()
    return {name: out[i].cpu().numpy() for i, name in enumerate(model.sources)}


def _roformer(mix, device: str) -> dict:
    import torch
    import yaml
    from bs_roformer import DEFAULT_MODEL, ensure_model_assets, get_model_from_config
    from bs_roformer.inference import SafeLoaderWithTuple
    from bs_roformer.utils import demix_track
    from ml_collections import ConfigDict

    ckpt, cfg_path = ensure_model_assets(DEFAULT_MODEL)
    with open(cfg_path) as f:
        config = ConfigDict(yaml.load(f, Loader=SafeLoaderWithTuple))
    model = get_model_from_config("bs_roformer", config)
    model.load_state_dict(torch.load(ckpt, map_location="cpu"))
    model.to(device).eval()
    res, _ = demix_track(config, model, torch.from_numpy(mix), device)
    return {name: res[name] for name in SIX_STEMS}


@app.function(image=image6, gpu="T4", memory=16384, timeout=30 * 60)
def separate6(audio: bytes, suffix: str, label: str) -> tuple[dict[str, bytes], float]:
    """One 6-stem config from SIX. The seconds count the GPU work, not loading the audio."""
    import io
    import tempfile
    import time

    import numpy as np
    import soundfile as sf

    from tastr.engine.audio import load_audio

    sr = 44100
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"track{suffix}"
        src.write_bytes(audio)
        mix = load_audio(src, sr, mono=False)
    if mix.shape[0] < 2:
        mix = np.repeat(mix[:1], 2, axis=0)
    mix = np.ascontiguousarray(mix[:2], dtype=np.float32)
    start = time.monotonic()
    if label == "6s":
        got = _demucs("htdemucs_6s", mix, "cuda")
    elif label == "hybrid":
        ft, six = _demucs("htdemucs_ft", mix, "cuda"), _demucs("htdemucs_6s", mix, "cuda")
        got = {n: ft[n] for n in ("vocals", "drums", "bass")} | {n: six[n] for n in ("guitar", "piano")}
        got["other"] = mix - sum(got.values())
    elif label == "roformer-sw":
        got = _roformer(mix, "cuda")
    else:
        raise ValueError(label)
    seconds = time.monotonic() - start
    peak = max(float(np.abs(a).max()) for a in got.values())
    scale = 1 / peak if peak > 1 else 1  # one factor for every stem, as tastr/stems.py does
    out = {}
    for name in SIX_STEMS:
        buf = io.BytesIO()
        sf.write(buf, (got[name] * scale).T, sr, format="FLAC", subtype="PCM_16")
        out[name] = buf.getvalue()
    return out, seconds


@app.local_entrypoint()
def main(audio_dir: str, out_dir: str = "stem-ab", limit: int = 5, gpu: str = "T4", seed: int = 0, set: str = "settings"):
    tracks = sorted(p for p in Path(audio_dir).iterdir() if p.suffix.lower() in AUDIO)[:limit]
    if not tracks:
        raise SystemExit(f"No audio files in {audio_dir}")
    if set not in ("settings", "six"):
        raise SystemExit("--set is settings (the 4-stem Demucs settings) or six (the 6-stem configs)")
    configs = SIX if set == "six" else CONFIGS
    out, rng = Path(out_dir), random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    fn = (separate6 if set == "six" else separate).with_options(gpu=gpu)
    jobs, answers = [], {}
    for t in tracks:
        letters = list("ABCD"[: len(configs)])
        rng.shuffle(letters)
        answers[t.stem] = dict(zip(letters, configs))
        data = t.read_bytes()
        args = (lambda label: (label,)) if set == "six" else (lambda label: CONFIGS[label])
        jobs += [(t, letter, label, (data, t.suffix.lower(), *args(label))) for letter, label in zip(letters, configs)]
    rng.shuffle(jobs)  # so neither the output order nor file dates follow the configs
    print(f"{len(tracks)} tracks x {len(configs)} configs = {len(jobs)} runs on {gpu}")
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
        w.writerows(sorted(rows, key=lambda r: (r["track"], list(configs).index(r["config"]))))
    stamp = max(p.stat().st_mtime for p in out.rglob("*"))
    for p in out.rglob("*"):
        os.utime(p, (stamp, stamp))
    import runpy

    build = runpy.run_path(str(Path(__file__).with_name("stem_ab_page.py")))["build"]
    print(f"Stems in {out}/<track>/<letter>/. Open {build(out)} to listen before opening answers.json.")
