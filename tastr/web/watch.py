"""Admin log lines from outside the server: GitHub Actions runs on main, and Litestream's backups to R2.

A hosted server checks both every TASTR_WATCH_MINUTES (background.start) and writes what's new to the
activity log (db.add_event). What it has already logged is kept in the `meta` table, so a restart or a
machine Fly stopped for being idle picks up where it left off.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Callable

GITHUB_API = "https://api.github.com"
FIRST_LOOK = timedelta(hours=24)  # the first check logs what finished in the last day, not the whole history
MAX_RUNS = 20  # runs logged per check at most
BACKUP_STALE = timedelta(hours=30)  # Litestream snapshots daily; longer than this without one is a problem
LAST_RUN, LAST_SNAPSHOT, PROBLEM_DAY = "watch_github_last_run", "watch_litestream_last_snapshot", "watch_litestream_problem_day"

Event = dict[str, Any]  # {user, kind, detail, ok, link, source}


def _when(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _took(start: str | None, end: str | None) -> str:
    a, b = _when(start), _when(end)
    if not a or not b:
        return ""
    s = max(0, int((b - a).total_seconds()))
    return f" ({s // 60} min)" if s >= 60 else f" ({s} s)"


# ---- GitHub Actions ----

def fetch_runs(repo: str, token: str = "", timeout: float = 10) -> list[dict[str, Any]]:
    """The latest finished workflow runs on main. A public repository needs no token; a private one needs a
    fine-grained token with read access to Actions."""
    req = urllib.request.Request(f"{GITHUB_API}/repos/{repo}/actions/runs?branch=main&status=completed&per_page=30",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "tastr",
                                          "X-GitHub-Api-Version": "2022-11-28",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https host
        return json.load(r).get("workflow_runs", [])


def allowed(run: dict[str, Any], workflows: tuple[str, ...]) -> bool:
    """Whether `run` is from one of `workflows`, each a workflow's name ("tests") or its file ("test.yml")."""
    names = {(run.get("name") or "").lower(), (run.get("path") or "").rsplit("/", 1)[-1].lower()}
    return any(w.lower() in names for w in workflows)


def run_events(runs: list[dict[str, Any]], last_id: int | None, now: datetime,
               workflows: tuple[str, ...]) -> tuple[list[Event], int | None]:
    """Log lines for the runs of `workflows` newer than `last_id`, oldest first, and the newest run id seen."""
    new = [r for r in runs if isinstance(r.get("id"), int) and (last_id is None or r["id"] > last_id) and allowed(r, workflows)]
    if last_id is None:
        new = [r for r in new if (_when(r.get("updated_at")) or now) >= now - FIRST_LOOK]
    newest = max([r["id"] for r in runs if isinstance(r.get("id"), int)] + ([last_id] if last_id else []), default=None)
    out = []
    for r in sorted(new, key=lambda r: r["id"])[-MAX_RUNS:]:
        conclusion = r.get("conclusion") or "finished"
        verdict = {"success": "passed", "failure": "failed", "cancelled": "was cancelled", "timed_out": "timed out",
                   "skipped": "was skipped"}.get(conclusion, conclusion.replace("_", " "))
        subject = ((r.get("head_commit") or {}).get("message") or "").split("\n", 1)[0][:100]
        name = r.get("name") or "Workflow"
        out.append({"user": None, "kind": "ci", "source": "github", "link": r.get("html_url"),
                    "ok": conclusion in ("success", "skipped", "neutral", "cancelled"),
                    "detail": f"{name} {verdict} on main{_took(r.get('run_started_at'), r.get('updated_at'))}"
                              + (f": {subject}" if subject else "")})
    return out, newest


# ---- Litestream ----

def _table(text: str) -> list[dict[str, str]]:
    """Litestream's column output (a header line, then rows split on whitespace) as dicts."""
    lines = [ln.split() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    head = [h.lower() for h in lines[0]]
    return [dict(zip(head, row)) for row in lines[1:] if len(row) == len(head)]


def litestream_snapshots(db_path: str, run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                         config: str = "/etc/litestream.yml") -> list[dict[str, str]]:
    """Snapshots in the replica for `db_path`: [{replica, generation, index, size, created}]. Raises on failure."""
    exe = shutil.which("litestream") or "litestream"
    p = run([exe, "snapshots", "-config", config, str(db_path)], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "litestream failed").strip().splitlines()[-1][:200])
    return _table(p.stdout)


def backup_events(snapshots: list[dict[str, str]] | None, error: str | None, last: str | None, problem_day: str | None,
                  now: datetime) -> tuple[list[Event], str | None, str | None]:
    """Log lines for snapshots newer than `last` (a `created` time), and at most one problem line a day when
    the check fails or the newest snapshot is older than BACKUP_STALE. Returns (events, newest seen, problem day)."""
    out: list[Event] = []
    day = now.strftime("%Y-%m-%d")
    snaps = sorted((s for s in snapshots or [] if _when(s.get("created"))), key=lambda s: s["created"])
    newest = snaps[-1]["created"] if snaps else last
    fresh = [s for s in snaps if last is None or s["created"] > last]
    if last is None:  # first check: just the newest, so the log doesn't fill with old snapshots
        fresh = fresh[-1:]
    for s in fresh:
        size = int(s["size"]) if s.get("size", "").isdigit() else 0
        out.append({"user": None, "kind": "backup", "source": "r2", "ok": True, "link": None,
                    "detail": f"Litestream snapshot to R2 ({size / 1024 ** 2:.1f} MB, generation {s.get('generation', '?')[:8]})"})
    problem = None
    if error:
        problem = f"Couldn't check the R2 backup: {error}"
    elif not snaps:
        problem = "The R2 backup has no snapshots"
    elif now - _when(snaps[-1]["created"]) > BACKUP_STALE:
        hours = int((now - _when(snaps[-1]["created"])).total_seconds() // 3600)
        problem = f"No Litestream snapshot in R2 for {hours} hours"
    if problem and problem_day != day:
        out.append({"user": None, "kind": "backup", "source": "r2", "ok": False, "link": None, "detail": problem})
        problem_day = day
    return out, newest, problem_day
