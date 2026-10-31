"""Analysis as a background job, so the page can show which step each upload is on.

POST /api/analyze?background=1 stores the upload, starts a job and answers at once with its id; the page
then polls GET /api/analyze/jobs/{id}. A job is {state: queued|running|done|error, stage, file_index,
file_count, position?, results?, error?}; `stage` is "waiting" (for an analysis slot), "fetch" (a link),
"decode", or an engine stage (core, sections, tags, suggestions, views).

Each job is also a small JSON file under $TASTR_HOME/analysis-jobs/, so a job a restarted server left
unfinished reads as failed ("upload it again") instead of vanishing. Finished jobs are forgotten after an hour.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

KEEP_SECONDS = 3600
RESTARTED = "The server restarted before this finished. Upload it again."


class AnalysisJobs:
    def __init__(self, folder: Path):
        self.folder = folder
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._seq = 0

    def resume(self) -> int:
        """Mark the jobs a previous run of the server left unfinished as failed; returns how many."""
        n = 0
        for f in self.folder.glob("*.json") if self.folder.is_dir() else []:
            try:
                job = json.loads(f.read_text())
            except (OSError, ValueError):
                f.unlink(missing_ok=True)
                continue
            if job.get("state") in ("queued", "running"):
                job.update(state="error", error=RESTARTED)
                f.write_text(json.dumps(job))
                n += 1
        return n

    def start(self, owner: str | None, file_count: int, work: Callable[["Progress"], dict[str, Any]]) -> dict[str, Any]:
        """Run `work(progress)` in a thread; it returns the job's results ({"results": [...]}) or raises
        JobError with a message for the page. Returns the new job's status."""
        self._forget_old()
        with self._lock:
            self._seq += 1
            job = {"id": uuid.uuid4().hex, "owner": owner, "state": "queued", "stage": "waiting", "file_index": 0,
                   "file_count": file_count, "created": time.time(), "_seq": self._seq}
            self._jobs[job["id"]] = job
        self._save(job)
        threading.Thread(target=self._run, args=(job, work), name="tastr-analysis", daemon=True).start()
        return self.status(job["id"], owner)

    def status(self, job_id: str, owner: str | None) -> dict[str, Any] | None:
        """The job, or None if there's no such job or it is someone else's (`owner` None: anyone's)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                out = {k: v for k, v in job.items() if not k.startswith("_") and k != "owner"}
                if job["state"] == "queued":
                    out["position"] = sum(j["state"] == "queued" and j["_seq"] < job["_seq"] for j in self._jobs.values())
                job_owner = job["owner"]
            else:
                out, job_owner = self._load(job_id)
        if out is None or (owner is not None and job_owner != owner):
            return None
        return out

    def _load(self, job_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """A job from an earlier run of the server (see resume)."""
        if not job_id.isalnum():
            return None, None
        try:
            job = json.loads((self.folder / f"{job_id}.json").read_text())
        except (OSError, ValueError):
            return None, None
        return {k: v for k, v in job.items() if not k.startswith("_") and k != "owner"}, job.get("owner")

    def _run(self, job: dict[str, Any], work) -> None:
        progress = Progress(self, job)
        try:
            results = work(progress)
        except JobError as e:
            self._update(job, state="error", error=str(e))
        except Exception as e:  # noqa: BLE001 - the page gets a short message, the log the details
            log.exception("analysis job failed")
            self._update(job, state="error", error=f"{type(e).__name__}: {e}")
        else:
            self._update(job, state="done", stage=None, **results)

    def _update(self, job: dict[str, Any], **kw) -> None:
        with self._lock:
            job.update(kw)
        self._save(job)

    def _save(self, job: dict[str, Any]) -> None:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with self._lock:
                text = json.dumps({k: v for k, v in job.items() if not k.startswith("_")})
            (self.folder / f"{job['id']}.json").write_text(text)
        except OSError:
            log.warning("couldn't save analysis job %s", job["id"])

    def _forget_old(self) -> None:
        cutoff = time.time() - KEEP_SECONDS
        with self._lock:
            old = [k for k, j in self._jobs.items() if j["state"] in ("done", "error") and j["created"] < cutoff]
            for k in old:
                del self._jobs[k]
        for f in self.folder.glob("*.json") if self.folder.is_dir() else []:
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            except OSError:
                pass


class JobError(Exception):
    """A failure the page can show as is, e.g. a busy server."""


class Progress:
    """What a job's work reports: which file it is on, and which step."""

    def __init__(self, jobs: AnalysisJobs, job: dict[str, Any]):
        self._jobs, self._job = jobs, job

    def running(self) -> None:
        self._jobs._update(self._job, state="running")

    def stage(self, name: str, file_index: int | None = None) -> None:
        kw = {"stage": name} | ({"file_index": file_index} if file_index is not None else {})
        self._jobs._update(self._job, **kw)

    @property
    def last_stage(self) -> str | None:
        return self._job.get("stage")
