"""Stem separation, on demand per track: vocals, drums, bass and other, with Demucs (`htdemucs`).

Demucs and PyTorch are large, so they are an optional extra (`pip install 'tastr[stems]'`); without
them `available()` is False and the API answers 501 with that install hint. Stems are FLAC files in
`$TASTR_HOME/stems/<sha256 prefix>/` (one folder per audio file, so re-analysis keeps them) and are
deleted with the track.

The web server runs one separation at a time in a background thread (`Jobs`); `tastr stems ID`
runs the same `separate_track` in the foreground. With `TASTR_STEMS_BACKEND=modal`, Demucs runs on a
Modal GPU instead (`tastr[cloud]`, docs/cloud.md) and only the stems come back here.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

STEM_NAMES = ("vocals", "bass", "other", "drums")
MODEL = "htdemucs"
INSTALL_HINT = "Stem separation needs Demucs, an optional install (about 2 GB with PyTorch): pip install 'tastr[stems]'"

Progress = Callable[[float], None]


def backend() -> str:
    return os.environ.get("TASTR_STEMS_BACKEND", "local")


def available() -> bool:
    return importlib.util.find_spec("modal" if backend() == "modal" else "demucs") is not None


def stems_dir(sha256: str | None, track_id: int) -> Path:
    home = Path(os.environ.get("TASTR_HOME", Path.home() / ".tastr"))
    return home / "stems" / (sha256[:16] if sha256 else f"track-{track_id}")


def stem_path(folder: Path, name: str) -> Path:
    return folder / f"{name}.flac"


def is_ready(folder: Path) -> bool:
    return all(stem_path(folder, n).is_file() for n in STEM_NAMES)


def remove(folder: Path) -> None:
    shutil.rmtree(folder, ignore_errors=True)


def separate(audio_path: str | Path, out_dir: Path, progress: Progress | None = None) -> None:
    """Split one file into STEM_NAMES FLAC files in `out_dir` with Demucs. Tests replace this."""
    if backend() == "modal":
        from .cloud import remote

        remote.stems(audio_path, out_dir, progress)
        return
    import numpy as np
    import soundfile as sf
    import torch
    from demucs import apply as demucs_apply
    from demucs.pretrained import get_model

    from .engine.audio import load_audio

    model = get_model(MODEL)  # downloads the weights (about 80 MB) the first time
    model.eval()
    wav = load_audio(audio_path, model.samplerate, mono=False)  # (channels, n); m4a/aac go through ffmpeg
    if wav.shape[0] < model.audio_channels:
        wav = np.repeat(wav[:1], model.audio_channels, axis=0)
    mix = torch.from_numpy(np.ascontiguousarray(wav[: model.audio_channels], dtype=np.float32))
    ref = mix.mean(0)
    mix = (mix - ref.mean()) / (ref.std() + 1e-8)  # the normalization `demucs.separate` uses
    device = os.environ.get("TASTR_STEMS_DEVICE") or _default_device(torch)

    real_tqdm = demucs_apply.tqdm
    if progress is not None:
        # apply_model only reports progress through tqdm, so count the chunks it iterates over.
        def counting(items, **_):
            items = list(items)
            for i, x in enumerate(items):
                yield x
                progress((i + 1) / len(items))

        demucs_apply.tqdm = types.SimpleNamespace(tqdm=counting)
    try:
        with torch.no_grad():
            sources = demucs_apply.apply_model(model, mix[None], device=device, progress=progress is not None)[0]
    finally:
        demucs_apply.tqdm = real_tqdm
    sources = sources * ref.std() + ref.mean()
    # Hot masters decode past 1.0; scale every stem by the same amount so 16-bit FLAC doesn't clip them.
    peak = float(sources.abs().max())
    if peak > 1.0:
        sources = sources / peak
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, audio in zip(model.sources, sources):
        if name in STEM_NAMES:
            sf.write(stem_path(out_dir, name), audio.cpu().numpy().T, model.samplerate, subtype="PCM_16")


def _default_device(torch) -> str:
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    return "mps" if mps is not None and mps.is_available() else "cpu"  # Apple GPU: about 2x a Mac's CPU


def separate_track(audio_path: str | Path, folder: Path, progress: Progress | None = None) -> None:
    """Separate into a scratch folder, then move it into place, so a half-done run never looks ready."""
    tmp = folder.with_name(folder.name + ".partial")
    remove(tmp)
    try:
        separate(audio_path, tmp, progress)
        missing = [n for n in STEM_NAMES if not stem_path(tmp, n).is_file()]
        if missing:
            raise RuntimeError(f"Separation produced no {', '.join(missing)} stem")
        remove(folder)
        tmp.rename(folder)
    finally:
        remove(tmp)


class Jobs:
    """Background separations for the web server, one at a time; later requests wait their turn."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tastr-stems")
        self._lock = threading.Lock()
        self._state: dict[Path, dict] = {}

    def status(self, folder: Path) -> dict:
        with self._lock:
            job = self._state.get(folder)
            if job and job["state"] in ("queued", "running", "error"):
                return dict(job)
        if is_ready(folder):
            return {"state": "ready", "stems": list(STEM_NAMES)}
        return {"state": "none"}

    def start(self, audio_path: Path, folder: Path) -> dict:
        with self._lock:
            job = self._state.get(folder)
            if job and job["state"] in ("queued", "running"):
                return dict(job)
            self._state[folder] = {"state": "queued", "progress": 0.0}
        self._pool.submit(self._run, audio_path, folder)
        return self.status(folder)

    def forget(self, folder: Path) -> None:
        """For a deleted track: a finished job's state goes; a running one deletes its stems when it ends."""
        with self._lock:
            job = self._state.get(folder)
            if job and job["state"] in ("queued", "running"):
                job["cancelled"] = True
            else:
                self._state.pop(folder, None)

    def _set(self, folder: Path, **kw) -> None:
        with self._lock:
            self._state[folder].update(kw)

    def _run(self, audio_path: Path, folder: Path) -> None:
        with self._lock:
            cancelled = self._state[folder].get("cancelled")
        if not cancelled:
            self._set(folder, state="running")
            try:
                separate_track(audio_path, folder, lambda p: self._set(folder, progress=round(min(max(p, 0.0), 1.0), 3)))
            except Exception as e:  # noqa: BLE001 - the error is shown in the UI
                self._set(folder, state="error", error=f"{type(e).__name__}: {e}")
        with self._lock:
            job = self._state[folder]
            if job.get("cancelled"):  # the track was deleted meanwhile
                del self._state[folder]
                remove(folder)
            elif job["state"] != "error":
                del self._state[folder]  # the files on disk say "ready" from here on
