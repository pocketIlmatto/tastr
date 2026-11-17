"""Stem separation, on demand per track: vocals, drums, bass and other, with Demucs (`htdemucs_ft`, chosen in a blind listening test: docs/stems-tuning.md).

Demucs and PyTorch are large, so they are an optional extra (`pip install 'tastr[stems]'`); without
them `available()` is False and the API answers 501 with that install hint. Stems are FLAC files in
`$TASTR_HOME/stems/<sha256 prefix>/` (one folder per audio file, so re-analysis keeps them) and are
deleted with the track. Next to them, `stems.json` records the model and settings that made them (`made_with()`),
so stems from an older default can be told apart and separated again; a folder without it predates the record and
was made with `htdemucs` (LEGACY).

The web server runs one separation at a time in a background thread (`Jobs`); `tastr stems ID`
runs the same `separate_track` in the foreground. With `TASTR_STEMS_BACKEND=modal`, Demucs runs on a
Modal GPU instead (`tastr[cloud]`, docs/cloud.md) and only the stems come back here.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import shutil
import threading
import time
import types
from pathlib import Path
from typing import Callable

from . import blobs, runner
from .markers import Markers, legacy_files

STEM_NAMES = ("vocals", "bass", "other", "drums")
MODEL = "htdemucs_ft"  # four fine-tuned models, one per stem; about 2.3x the GPU time of `htdemucs`
HF_REPO = "adefossez/HTDemucs"  # where Demucs 4.1 keeps its pretrained models on the Hugging Face Hub
SHIFTS, OVERLAP = 1, 0.25  # Demucs' apply_model defaults, what the app uses
INFO_NAME = "stems.json"  # the model and settings that made a folder's stems
# Stems made before the record existed: every one of them came from `htdemucs` with the defaults.
LEGACY = {"model": "htdemucs", "shifts": 1, "overlap": 0.25}
INSTALL_HINT = "Stem separation needs Demucs, an optional install (about 2 GB with PyTorch): pip install 'tastr[stems]'"

Progress = Callable[[float], None]
log = logging.getLogger(__name__)  # `tastr serve` prints it (see web.server.configure_logging)


def backend() -> str:
    return os.environ.get("TASTR_STEMS_BACKEND", "local")


def available() -> bool:
    if backend() == "modal":
        from .cloud import remote

        return remote.available()
    return importlib.util.find_spec("demucs") is not None


def stems_root() -> Path:
    return Path(os.environ.get("TASTR_HOME", Path.home() / ".tastr")) / "stems"


def stems_dir(sha256: str | None, track_id: int) -> Path:
    return stems_root() / (sha256[:16] if sha256 else f"track-{track_id}")


def legacy_marker(folder: Path) -> Path:
    """Where older servers kept a queued or running separation's audio path (read once by `Jobs.resume()`)."""
    return folder.with_name(folder.name + ".job")


def stem_path(folder: Path, name: str) -> Path:
    return folder / f"{name}.flac"


def is_ready(folder: Path) -> bool:
    """Every stem is here, or in the audio bucket (tastr/blobs.py)."""
    return all(stem_path(folder, n).is_file() for n in STEM_NAMES) or (
        blobs.enabled() and all(blobs.in_bucket(stem_path(folder, n)) for n in STEM_NAMES))


def remove(folder: Path) -> None:
    shutil.rmtree(folder, ignore_errors=True)
    blobs.remove_tree(folder)
    _made_with.pop(folder, None)


def info_path(folder: Path) -> Path:
    return folder / INFO_NAME


def settings(model_name: str = MODEL, shifts: int = SHIFTS, overlap: float = OVERLAP, device: str | None = None) -> dict:
    """What a separation with these settings records in `stems.json`."""
    try:
        version = importlib.metadata.version("demucs")
    except importlib.metadata.PackageNotFoundError:
        version = None
    out = {"model": model_name, "shifts": shifts, "overlap": overlap, "demucs": version, "device": device,
           "made_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    return {k: v for k, v in out.items() if v is not None}


_made_with: dict[Path, dict] = {}  # folder -> its record, read once (the bucket's copy costs a request)


def made_with(folder: Path) -> dict:
    """The model and settings that made the folder's stems, from its `stems.json`, plus `recorded` (False: there
    was no record, so they are LEGACY htdemucs stems) and `outdated` (made with another model than MODEL). Only
    meaningful once the stems are ready. `latest`: MODEL, what separating again would use."""
    info = _made_with.get(folder)
    if info is None:
        info = _read_info(folder)
        _made_with[folder] = info
    return {**info, "outdated": info.get("model") != MODEL, "latest": MODEL}


def _read_info(folder: Path) -> dict:
    path = info_path(folder)
    try:
        if path.is_file():
            data = json.loads(path.read_text())
        elif blobs.in_bucket(path):
            data = json.loads(blobs.open_read(path).read())
        else:
            data = None
    except (OSError, ValueError):
        log.warning("Unreadable %s; counting its stems as %s", path, LEGACY["model"], exc_info=True)
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("model"), str):
        return {**LEGACY, "recorded": False}
    return {**data, "recorded": True}


def _record(folder: Path, info: dict, *, bucket_only: bool = False) -> None:
    """Write the folder's `stems.json` on disk (the caller pushes it), or with `bucket_only` straight to the
    bucket, for stems that never touch this server's disk."""
    body = json.dumps(info, indent=1).encode()
    path = info_path(folder)
    if bucket_only:
        blobs.client().put(blobs.key(path), body, "application/json")
        blobs.reset_listings()
    else:
        path.write_bytes(body)
    _made_with.pop(folder, None)


def separate(audio_path: str | Path, out_dir: Path, progress: Progress | None = None, *,
             model_name: str = MODEL, shifts: int = SHIFTS, overlap: float = OVERLAP) -> dict | None:
    """Split one file into STEM_NAMES FLAC files in `out_dir` with Demucs, and return the `settings()` it used
    (None: unknown). Tests replace this.

    `model_name`, `shifts` and `overlap` default to what the app uses; scripts/stem_ab.py tries others
    (docs/stems-tuning.md). They apply to the local Demucs only, not to the Modal backend."""
    if backend() == "modal":
        from .cloud import remote

        if progress:
            progress(0.05)  # Modal reports no progress; show that the job has started
        result = remote.stems(audio_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, data in result.items():
            if name in STEM_NAMES:
                stem_path(out_dir, name).write_bytes(data)
        if progress:
            progress(1.0)
        return _remote_settings(result)
    import numpy as np
    import soundfile as sf
    import torch
    from demucs import apply as demucs_apply
    from demucs.pretrained import get_model

    from .engine.audio import load_audio

    with _hub_offline_if_cached(model_name):
        model = get_model(model_name)  # downloads the weights (about 320 MB) the first time
    model.eval()
    wav = load_audio(blobs.ensure(audio_path) or audio_path, model.samplerate, mono=False)  # (channels, n); m4a/aac go through ffmpeg
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
            sources = demucs_apply.apply_model(model, mix[None], shifts=shifts, overlap=overlap, device=device,
                                               progress=progress is not None)[0]
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
    return settings(model_name, shifts, overlap, device)


def _remote_settings(result: dict) -> dict | None:
    """The settings a Modal separation sent back with its stems (tastr/cloud/jobs.py run_stems), if any."""
    got = result.get("settings")
    return got if isinstance(got, dict) and isinstance(got.get("model"), str) else None


@contextlib.contextmanager
def _hub_offline_if_cached(model_name: str = MODEL):
    """Keep Hugging Face offline while Demucs loads a model that is already in its cache.

    Demucs 4.1 loads its models (MODEL included) from the Hugging Face Hub (adefossez/HTDemucs). Even with the files
    cached, each load asks the Hub whether they changed, which prints an "unauthenticated requests"
    warning when no HF_TOKEN is set. HF_HUB_OFFLINE is read once at import, so set the constant.
    """
    try:
        import yaml
        from huggingface_hub import constants, try_to_load_from_cache
    except ImportError:  # Demucs 4.0 downloads from Meta's server and doesn't use the Hub
        yield
        return
    bag = try_to_load_from_cache(HF_REPO, f"{model_name}.yaml")  # a model kept elsewhere just loads online
    cached = isinstance(bag, str)
    if cached:
        with open(bag) as f:
            sigs = yaml.safe_load(f).get("models", [])
        cached = all(isinstance(try_to_load_from_cache(HF_REPO, f"{s}.safetensors"), str) for s in sigs)
    if not cached or constants.HF_HUB_OFFLINE:
        yield
        return
    constants.HF_HUB_OFFLINE = True
    try:
        yield
    finally:
        constants.HF_HUB_OFFLINE = False


def _default_device(torch) -> str:
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    return "mps" if mps is not None and mps.is_available() else "cpu"  # Apple GPU: about 2x a Mac's CPU


def separate_track(audio_path: str | Path, folder: Path, progress: Progress | None = None) -> None:
    """Separate into a scratch folder, then move it into place, so a half-done run never looks ready. With
    the audio bucket on, the stems end up there (and Modal writes them there itself)."""
    if blobs.enabled() and backend() == "modal":
        _separate_into_bucket(audio_path, folder, progress)
        return
    tmp = folder.with_name(folder.name + ".partial")
    remove(tmp)
    try:
        info = separate(audio_path, tmp, progress) or settings()  # unknown: what separate() uses by default
        missing = [n for n in STEM_NAMES if not stem_path(tmp, n).is_file()]
        if missing:
            raise RuntimeError(f"Separation produced no {', '.join(missing)} stem")
        _record(tmp, info)  # before the move, so ready stems always come with their record
        # Swap the new set in with two renames, so stems being separated again are gone only for that moment. In the
        # bucket, push() below writes over the old keys, so the old set stays playable there until the new one is in.
        old = folder.with_name(folder.name + ".old")
        shutil.rmtree(old, ignore_errors=True)
        if folder.exists():
            folder.rename(old)
        tmp.rename(folder)
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _made_with.pop(folder, None)
    for path in [stem_path(folder, n) for n in STEM_NAMES] + [info_path(folder)]:
        blobs.push(path)


def staging_dir(folder: Path) -> Path:
    """Where a separation on Modal puts its stems in the bucket before they are copied over `folder`'s."""
    return folder.with_name(folder.name + ".staging")


def _separate_into_bucket(audio_path: str | Path, folder: Path, progress: Progress | None) -> None:
    """Modal uploads each stem to a signed link in the bucket, so no stem passes through here. The links are for
    a staging folder; once all four stems are there they are copied over the folder's (with stems.json) and the
    staging copy goes. So stems being separated again stay playable until the new set is in place, and a failed
    run leaves them as they were."""
    from .cloud import remote

    if progress:
        progress(0.05)  # Modal reports no progress; show that the job has started
    stage = staging_dir(folder)
    blobs.remove_tree(stage)  # a failed earlier run may have left some
    try:
        result = remote.stems(audio_path, {n: blobs.put_url(stem_path(stage, n)) for n in STEM_NAMES})
        blobs.reset_listings()
        missing = [n for n in STEM_NAMES if not blobs.in_bucket(stem_path(stage, n))]
        if missing:
            raise RuntimeError(f"Separation produced no {', '.join(missing)} stem")
        for n in STEM_NAMES:
            blobs.copy(stem_path(stage, n), stem_path(folder, n))
        _record(folder, _remote_settings(result) or settings(), bucket_only=True)
    finally:
        blobs.remove_tree(stage)
    shutil.rmtree(folder, ignore_errors=True)  # this server's cached copies of the old stems
    if progress:
        progress(1.0)


class Jobs:
    """Background separations for the web server, one at a time; later requests wait their turn.

    Each queued or running job leaves a marker in the database (tastr/markers.py) until it ends, and
    `resume()` restarts them, so a server that stopped meanwhile (a hosted machine that auto-stops, a crash)
    doesn't lose them. `db_path`: the database that keeps the markers (None: the default one).
    """

    def __init__(self, db_path=None) -> None:
        self.markers = Markers(db_path, "stems")
        self._lane = runner.current().lane("stems", 1)  # one at a time
        self._lock = threading.Lock()
        self._state: dict[Path, dict] = {}
        self._seq = 0

    def status(self, folder: Path) -> dict:
        """The job's state; a queued one also says how many jobs are ahead of it (`position`), a running one
        how long it has been running (`elapsed`, seconds), so the page can show time left.

        While stems are separated again (`start(again=True)`), the current ones stay: the state is "ready" and
        `again` holds the new job's state (queued, running, or error when it failed and the old set stayed)."""
        job, again = None, False
        with self._lock:
            got = self._state.get(folder)
            if got and got["state"] in ("queued", "running", "error"):
                job = {k: v for k, v in got.items() if not k.startswith("_") and k != "again"}
                if got["state"] == "queued":
                    job["position"] = sum(j["state"] in ("queued", "running") and j["_seq"] < got["_seq"] for j in self._state.values())
                elif got["state"] == "running" and "_started" in got:
                    job["elapsed"] = round(time.monotonic() - got["_started"], 1)
                again = got.get("again")
        if job and not again:
            return job
        # A job that separates again only starts on ready stems, and nothing removes them while it runs; so they
        # count as ready even in the moment the new set is moved in (separate_track).
        if again or is_ready(folder):
            out = {"state": "ready", "stems": list(STEM_NAMES), "made_with": made_with(folder)}
            return {**out, "again": job} if job else out
        return {"state": "none"}

    def busy(self, folder: Path) -> bool:
        """Whether a separation for the folder is queued or running (a first one, or one that separates again)."""
        with self._lock:
            job = self._state.get(folder)
            return bool(job and job["state"] in ("queued", "running"))

    def start(self, audio_path: Path, folder: Path, again: bool = False) -> dict:
        """Queue a separation. `again`: the folder has stems already and this one replaces them; they stay
        playable until the new set is in place (separate_track)."""
        with self._lock:
            job = self._state.get(folder)
            if job and job["state"] in ("queued", "running"):
                return dict(job)
            self._seq += 1
            self._state[folder] = {"state": "queued", "progress": 0.0, "_seq": self._seq, "again": again}
        self.markers.put(folder.name, {"audio": str(audio_path), **({"again": True} if again else {})})
        self._lane.submit(self._run, audio_path, folder)
        return self.status(folder)

    def resume(self) -> int:
        """Restart the jobs a previous run of the server left unfinished; returns how many."""
        for f in legacy_files(stems_root(), "*.job"):  # from a server that kept them as files
            self.markers.put(f.stem, {"audio": f.read_text().strip()})
            f.unlink(missing_ok=True)
        n = 0
        for key, data, _ in self.markers.items():
            folder, audio = stems_root() / key, Path(data.get("audio") or "")
            again = bool(data.get("again"))
            if (is_ready(folder) and not again) or not data.get("audio") or not blobs.exists(audio):
                self.markers.remove(key)
                continue
            log.info("Resuming stem separation for %s", audio)
            self.start(audio, folder, again=again and is_ready(folder))
            n += 1
        return n

    def active(self) -> int:
        """Jobs queued or running, for the admin view."""
        with self._lock:
            return sum(job["state"] in ("queued", "running") for job in self._state.values())

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
            self._set(folder, state="running", _started=time.monotonic())
            log.info("Separating stems for %s", audio_path)
            start = time.monotonic()
            try:
                separate_track(audio_path, folder, lambda p: self._set(folder, progress=round(min(max(p, 0.0), 1.0), 3)))
            except Exception as e:  # noqa: BLE001 - the error is shown in the UI
                log.exception("Stem separation failed for %s", audio_path)
                self._set(folder, state="error", error=f"{type(e).__name__}: {e}")
            else:
                log.info("Stems ready for %s (%.0f s)", audio_path, time.monotonic() - start)
        with self._lock:
            self.markers.remove(folder.name)
            job = self._state[folder]
            if job.get("cancelled"):  # the track was deleted meanwhile
                del self._state[folder]
                remove(folder)
            elif job["state"] != "error":
                del self._state[folder]  # the files on disk say "ready" from here on
