"""On-demand drum model runs for the web server: "Detect drums with the model" re-analyzes one track with
the optional pretrained drum model (engine/adt.py) in the background, like a stem separation.

Everyday analysis uses the built-in drum lanes. The model is slower (and on a hosted server runs in its
own, larger Modal function), so it runs only for the tracks someone asks for, one at a time; later
requests wait their turn. Each queued or running job leaves `<TASTR_HOME>/drum-jobs/<track id>.job` until
it ends, and `resume()` restarts them, so a server that stopped meanwhile doesn't lose them.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

INSTALL_HINT = ("Detecting drums with the model needs PyTorch, an optional install: pip install 'tastr[drums]' "
                "(it shares PyTorch with tastr[stems])")

log = logging.getLogger(__name__)


def available() -> bool:
    """Whether this server can run the model: on Modal (a hosted server), or with PyTorch installed here."""
    from .engine import adt

    if os.environ.get("TASTR_ANALYSIS_BACKEND", "local") == "modal":
        return not adt.switched_off() and importlib.util.find_spec("modal") is not None
    return adt.installed()


def jobs_root() -> Path:
    return Path(os.environ.get("TASTR_HOME", Path.home() / ".tastr")) / "drum-jobs"


class Jobs:
    """`run(track_id)` re-analyzes one track with the model and raises on failure."""

    def __init__(self, run: Callable[[int], None]) -> None:
        self._run_track = run
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tastr-drums")
        self._lock = threading.Lock()
        self._state: dict[int, dict] = {}

    def status(self, track_id: int) -> dict:
        with self._lock:
            return dict(self._state.get(track_id) or {"state": "none"})

    def busy(self, track_id: int) -> bool:
        return self.status(track_id)["state"] in ("queued", "running")

    def start(self, track_id: int) -> dict:
        with self._lock:
            job = self._state.get(track_id)
            if job and job["state"] in ("queued", "running"):
                return dict(job)
            self._state[track_id] = {"state": "queued"}
        jobs_root().mkdir(parents=True, exist_ok=True)
        self._marker(track_id).touch()
        self._pool.submit(self._run, track_id)
        return self.status(track_id)

    def resume(self) -> int:
        """Restart the jobs a previous run of the server left unfinished; returns how many."""
        n = 0
        for marker in sorted(jobs_root().glob("*.job")):
            try:
                track_id = int(marker.stem)
            except ValueError:
                marker.unlink(missing_ok=True)
                continue
            log.info("Resuming the drum model for track %s", track_id)
            self.start(track_id)
            n += 1
        return n

    def active(self) -> int:
        with self._lock:
            return sum(job["state"] in ("queued", "running") for job in self._state.values())

    def forget(self, track_id: int) -> None:
        """For a deleted track: a queued job is skipped; a finished job's state goes."""
        with self._lock:
            job = self._state.get(track_id)
            if job and job["state"] in ("queued", "running"):
                job["cancelled"] = True
            else:
                self._state.pop(track_id, None)

    def _marker(self, track_id: int) -> Path:
        return jobs_root() / f"{track_id}.job"

    def _set(self, track_id: int, **kw) -> None:
        with self._lock:
            self._state[track_id].update(kw)

    def _run(self, track_id: int) -> None:
        with self._lock:
            cancelled = self._state[track_id].get("cancelled")
        if not cancelled:
            self._set(track_id, state="running")
            log.info("Detecting drums with the model for track %s", track_id)
            start = time.monotonic()
            try:
                self._run_track(track_id)
            except Exception as e:  # noqa: BLE001 - the error is shown in the UI
                log.exception("The drum model failed for track %s", track_id)
                self._set(track_id, state="error", error=str(e) or type(e).__name__)
            else:
                log.info("Drum model done for track %s (%.0f s)", track_id, time.monotonic() - start)
                self._set(track_id, state="done")
        with self._lock:
            self._marker(track_id).unlink(missing_ok=True)
            if self._state[track_id].get("cancelled"):
                del self._state[track_id]
