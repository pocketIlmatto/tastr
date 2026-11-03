"""Admin log lines from outside the server: GitHub Actions runs on main.

A hosted server checks every TASTR_WATCH_MINUTES (background.start) and writes what's new to the
activity log (db.add_event). What it has already logged is kept in the `meta` table, so a restart or a
Durable Object that was idle picks up where it left off.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from .. import http

GITHUB_API = "https://api.github.com"
FIRST_LOOK = timedelta(hours=24)  # the first check logs what finished in the last day, not the whole history
MAX_RUNS = 20  # runs logged per check at most
LAST_RUN = "watch_github_last_run"

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
    # Through tastr/http.py, so it also works on the Cloudflare Worker (its fetch, not sockets).
    with http.request("GET", f"{GITHUB_API}/repos/{repo}/actions/runs?branch=main&status=completed&per_page=30",
                      {"Accept": "application/vnd.github+json", "User-Agent": "tastr", "X-GitHub-Api-Version": "2022-11-28",
                       **({"Authorization": f"Bearer {token}"} if token else {})}, timeout=timeout) as r:
        if r.status != 200:
            limited = r.status in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0"
            hint = (" (rate limit: without TASTR_GITHUB_TOKEN the limit is shared with everyone on this server's "
                    "IP address, as on Cloudflare)") if limited and not token else ""
            raise http.HTTPError(f"GitHub answered {r.status}{hint}", r.status)
        return json.loads(r.read()).get("workflow_runs", [])


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
