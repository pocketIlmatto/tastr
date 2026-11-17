"""Karaoke: a track without its vocals, made by mixing every stem but the vocals (bass, drums and other).

The mix is one ordinary audio file, so the player plays it like the full mix: it works in the playlist and on a
phone's locked screen, which mixing four stems live in the browser wouldn't (decoding four whole stems takes
hundreds of MB on a phone).

- It needs the track's stems (tastr/stems.py) and is mixed the first time someone asks for it, in the background
  (`Jobs`, one at a time), then kept next to the stems as `instrumental.flac` (16-bit FLAC, like the stems). It goes
  with the stems: deleting them deletes it, and separating them again drops it, so the next request mixes the new
  ones.
- Mixing is a sum, a few seconds of CPU. With TASTR_STEMS_BACKEND=modal and the audio bucket on (the hosted site,
  whose server has no numpy) it runs in Modal's `karaoke` function, which reads the stems from signed links and
  writes the mix to a signed upload link, like key lock.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
import time
from pathlib import Path

from . import blobs, runner, stems

PARTS = tuple(n for n in stems.STEM_NAMES if n != "vocals")  # what the mix is made of
INSTALL_HINT = "Karaoke needs numpy and soundfile: pip install numpy soundfile"

log = logging.getLogger(__name__)


def backend() -> str:
    """"modal" when stems run on Modal and the audio bucket is on (Modal reads and writes the bucket), else "local"."""
    return "modal" if stems.backend() == "modal" and blobs.enabled() else "local"


def available() -> bool:
    if backend() == "modal":
        from .cloud import remote

        return remote.available()
    return all(importlib.util.find_spec(m) is not None for m in ("numpy", "soundfile"))


def is_ready(folder: Path) -> bool:
    return blobs.exists(stems.instrumental_path(folder))


def mix_files(sources: list[str | Path], dst: Path) -> None:
    """Sum the stems in `sources` into one 16-bit FLAC at `dst`. Demucs' stems add up to the track, so the sum is
    no louder than the original; a hot master can still go past full scale, and then the whole mix is scaled
    down rather than clipped. Tests replace this."""
    import numpy as np
    import soundfile as sf

    total, rate = None, None
    for src in sources:
        x, sr = sf.read(str(src), dtype="float32", always_2d=True)  # (n, channels)
        if rate is None:
            total, rate = x, sr
            continue
        if sr != rate:
            raise ValueError(f"{Path(src).name} is at {sr} Hz, the other stems at {rate} Hz")
        n = min(len(total), len(x))
        total = total[:n] + x[:n, : total.shape[1]]
    if total is None:
        raise ValueError("no stems to mix")
    peak = float(np.abs(total).max()) if total.size else 0.0
    if peak > 1.0:
        total /= peak
    dst.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), total, rate, format="FLAC", subtype="PCM_16")


def make(folder: Path) -> None:
    """Mix the folder's stems into its instrumental, unless it is there already."""
    out = stems.instrumental_path(folder)
    if blobs.exists(out):
        return
    if not stems.is_ready(folder):
        raise RuntimeError("This track has no stems to mix")
    sources = {p: stems.stem_path(folder, p) for p in PARTS}
    if backend() == "modal":
        from .cloud import remote

        remote.karaoke({p: blobs.signed_url(src) for p, src in sources.items()}, blobs.put_url(out))
        blobs.reset_listings()
        if not blobs.in_bucket(out):
            raise RuntimeError("Karaoke produced no mix")
        return
    tmp = out.with_name(out.stem + ".partial.flac")
    try:
        mix_files([blobs.ensure(src) or src for src in sources.values()], tmp)
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    blobs.push(out)


class Jobs:
    """Background mixes for the web server, one at a time; later requests wait their turn. Keyed by the stems'
    folder. Nothing to resume after a restart: the page asks again."""

    def __init__(self) -> None:
        self._lane = runner.current().lane("karaoke", 1)
        self._lock = threading.Lock()
        self._state: dict[Path, dict] = {}

    def status(self, folder: Path) -> dict:
        """{"state": none | queued | running | ready | error, "error"?}."""
        with self._lock:
            job = dict(self._state.get(folder) or {})
        if job.get("state") in ("queued", "running", "error"):
            return job
        return {"state": "ready" if is_ready(folder) else "none"}

    def start(self, folder: Path) -> dict:
        with self._lock:
            job = self._state.get(folder)
            if not (job and job["state"] in ("queued", "running")):
                self._state[folder] = {"state": "queued"}
                self._lane.submit(self._run, folder)
        return self.status(folder)

    def forget(self, folder: Path) -> None:
        """For deleted or re-separated stems: drop a finished job's state."""
        with self._lock:
            job = self._state.get(folder)
            if job and job["state"] not in ("queued", "running"):
                del self._state[folder]

    def _run(self, folder: Path) -> None:
        with self._lock:
            self._state[folder] = {"state": "running"}
        start = time.monotonic()
        try:
            make(folder)
        except Exception as e:  # noqa: BLE001 - the page shows it
            log.exception("Karaoke mix failed for %s", folder)
            with self._lock:
                self._state[folder] = {"state": "error", "error": f"{type(e).__name__}: {e}"}
            return
        log.info("Karaoke mix ready for %s (%.1f s)", folder, time.monotonic() - start)
        with self._lock:
            self._state.pop(folder, None)
