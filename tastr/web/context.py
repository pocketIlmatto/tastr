"""What the web server's routes share: the settings, the database, the background job queues, the hosted
limits and the admin log. `create_app` (server.py) builds one AppContext and hands it to each group of routes
in routes/."""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
from contextlib import closing, nullcontext
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from .. import blobs, db, drum_jobs, lookup, service, stems
from ..engine import browser_card
from ..filemeta import describe, legacy_upload_prefix
from .analysis_jobs import AnalysisJobs, ref
from .hosted import (AccessVerifier, AnalysisSlots, LinkSigner, Settings, file_bytes, inline_script_hashes,
                     security_headers, today)

log = logging.getLogger("tastr.web.server")  # the name these lines had before the routes moved out of server.py
STATIC_DIR = Path(__file__).parent / "static"


def tastr_home() -> Path:
    return Path(os.environ.get("TASTR_HOME", Path.home() / ".tastr"))


def upload_dir() -> Path:
    d = tastr_home() / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _results(results) -> dict[str, Any]:
    return {
        "results": [
            {"track_id": r.track_id, "title": r.source.title or Path(r.source.uri).stem, "error": r.error}
            for r in results
        ]
    }


def _unprefixed(title: str | None, uri: str | None) -> str | None:
    return title.removeprefix(legacy_upload_prefix(uri)) if title and uri else title


def _failure_reason(error: str | None) -> str:
    """A short reason for the admin log, without the error text (which can name server paths)."""
    kind = (error or "").split(":", 1)[0]
    if kind == "DecodeError":
        return "it couldn't be decoded as audio"
    if _ran_out(error):
        return f"it ran out of {_ran_out(error)} on Modal ({kind or 'unknown'})"
    return f"the analysis hit an error ({kind or 'unknown'})"


def _ran_out(error: str | None) -> str | None:
    """"memory" or "time" when a Modal analysis was stopped for using too much of it, else None."""
    e = (error or "").lower()
    if "timeout" in e or "timed out" in e:
        return "time"
    if "memory" in e or "oom" in e.split(":", 1)[0]:
        return "memory"
    return None


def _hosted_error(error: str | None, settings: Settings) -> str:
    """What a friend sees when an analysis fails: no error text (it can name server paths), but why if it's known."""
    if _ran_out(error):
        return (f"The server ran out of {_ran_out(error)} analyzing this file. Tracks up to {settings.max_minutes:g} "
                "minutes work best; try a shorter file.")
    return "Couldn't analyze this file."


def runs_on(backend: str) -> str:
    return "modal" if backend == "modal" else "app"


def analysis_source() -> str:
    return runs_on(os.environ.get("TASTR_ANALYSIS_BACKEND", "local"))


class AppContext:
    """One server's shared state. `settings` default to the environment (TASTR_HOSTED etc.); `verifier` maps an
    Access token to an email (tests pass a stub; hosted mode otherwise checks tokens against the Access team's keys)."""

    def __init__(self, db_path: str | Path | None, settings: Settings, verifier=None):
        self.db_path = db_path
        self.settings = settings
        self.hosted = hosted = settings.hosted
        self.browser_engine = browser_card.enabled()
        if hosted and verifier is None:
            verifier = AccessVerifier(settings.access_team_domain, settings.access_aud)
        self.verifier = verifier
        self.slots = AnalysisSlots(settings.max_analyses) if hosted else None
        # Without TASTR_LINK_SECRET the Strudel links last only until the server restarts.
        self.signer = LinkSigner(settings.link_secret or secrets.token_bytes(32)) if hosted else None
        # With the audio bucket on, the page plays audio from signed links to it (tastr/blobs.py).
        self.extra_headers = (security_headers(inline_script_hashes((STATIC_DIR / "index.html").read_text()),
                                               media_origin=blobs.origin())
                              if hosted else {})
        # Their unfinished jobs are kept in the database (tastr/markers.py), so a restart picks them up.
        self.stem_jobs = stems.Jobs(db_path)
        self.analyses = AnalysisJobs(db_path, legacy_folder=tastr_home() / "analysis-jobs")
        self.drum_runs = drum_jobs.Jobs(self.run_drum_model, db_path)
        self.last_seen: dict[str, str] = {}  # email -> time of their last request, since the server started
        self.snapshot_day = ""  # the UTC day storage was last recorded for (storage_days)

    def conn(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

    def run_drum_model(self, track_id: int) -> None:
        """Re-analyze one track with the drum model (a drum_jobs job); raises with a message for the UI."""
        with closing(self.conn()) as c:
            with self.analysis_slot():
                result = service.reanalyze_track(c, track_id, drum_model=True)
        if result is None:
            raise LookupError("The track was deleted.")
        if result.error:
            if self.hosted:
                log.warning("drum model run for track %s failed: %s", track_id, result.error)
                raise RuntimeError("Couldn't analyze the file with the drum model.")
            raise RuntimeError(f"Couldn't analyze the file with the drum model: {result.error}")

    def analysis_slot(self):
        return self.slots or nullcontext()

    # The admin log and the hosted limits.

    def event(self, user: str | None, kind: str, detail: str, ok: bool = True, link: str | None = None,
              source: str = "app") -> None:
        """A line in the admin activity log (hosted only; your own machine keeps no log). `source`: where it
        happened, one of db.EVENT_SOURCES."""
        if self.hosted:
            with closing(self.conn()) as c:
                db.add_event(c, user, kind, detail, ok, link, source)

    def count_visit(self, user: str) -> None:
        """A visit is a person's first request after VISIT_IDLE without one; their very first is a first sign-in."""
        with closing(self.conn()) as c:
            first = not db.has_usage(c, user)
            db.add_usage(c, user, "visits", today())
        if first:
            self.event(user, "signin", "Signed in for the first time", source="cloudflare")

    def count_blocked(self) -> None:
        with closing(self.conn()) as c:
            db.add_blocked(c, today())

    def scan_library(self, c: sqlite3.Connection) -> list[dict[str, Any]]:
        """Each track's owner, upload time, last analysis and bytes stored (audio, and stems separately): on
        disk, or in the audio bucket when that is on (tastr/blobs.py)."""
        inbucket = {k: size for f in blobs.FOLDERS for k, (size, _) in blobs.listing(f + "/").items()}
        rows = []
        for owner, tid, sha, path, at, created in c.execute(
                "SELECT owner, id, sha256, local_path, analyzed_at, created_at FROM tracks"):
            folder = stems.stems_dir(sha, tid)
            if inbucket:
                audio = inbucket.get(blobs.key(path) or "", 0) if path else 0
                prefix = (blobs.key(folder / "x") or "")[:-1]
                stem_b = sum(size for k, size in inbucket.items() if prefix and k.startswith(prefix))
            else:
                audio = file_bytes(path)
                stem_b = sum(f.stat().st_size for f in folder.iterdir() if f.is_file()) if folder.is_dir() else 0
            rows.append({"owner": owner, "analyzed_at": at, "created_at": created or at, "audio": audio,
                         "stems": stem_b, "bytes": audio + stem_b})
        return rows

    def snapshot_storage(self, rows: list[dict[str, Any]] | None = None) -> None:
        """Record today's bytes stored per person (storage_days), for the admin view's Storage chart."""
        try:
            with closing(self.conn()) as c:
                rows = rows if rows is not None else self.scan_library(c)
                by: dict[str | None, int] = {}
                for r in rows:
                    by[r["owner"]] = by.get(r["owner"], 0) + r["bytes"]
                db.snapshot_storage(c, today(), by)
        except Exception:  # noqa: BLE001 - the chart misses a day; nothing else depends on it
            log.exception("Recording storage per person failed")

    def cap(self, c: sqlite3.Connection, user: str, kind: str) -> int:
        """Today's cap on `kind` ("tracks" or "stems") for `user`: an admin's change to it, else the server's."""
        s = self.settings
        return db.daily_cap(c, user, kind, today(), s.daily_tracks if kind == "tracks" else s.daily_stems)

    def require_admin(self, request: Request) -> str:
        if not self.hosted or request.state.user not in self.settings.admins:
            raise HTTPException(404, "Not Found")
        return request.state.user

    def refuse(self, user: str, status: int, message: str, limit: str, name: str | None = None):
        """Refuse a request over one of the hosted limits, and log which limit it hit (and for which file)."""
        self.event(user, "limit", f"Hit the {limit}" + (f": {name}" if name else ""), ok=False)
        return HTTPException(status, message)

    # Who owns what.

    def owns(self, user: str, track_id: int) -> bool:
        with closing(self.conn()) as c:
            exists, owner = db.track_owner(c, track_id)
        return exists and owner == user

    def owner_of(self, request: Request) -> str | None:
        """Whose library a request reads and writes: the signed-in email when hosted, else everything."""
        return request.state.user if self.hosted else None

    def results(self, rs, user: str | None = None, job: str | None = None, stages: list | None = None) -> dict[str, Any]:
        if self.hosted:  # error text can name server paths; keep it in the log
            for i, r in enumerate(rs):
                if r.error:
                    tag = f" (ref {ref(job)})" if job else ""
                    log.warning("analysis failed for %s%s: %s", r.source.uri, tag, r.error)
                    at = f" at step {stages[i]}" if stages and i < len(stages) and stages[i] else ""
                    self.event(user, "failed", f"Analysis failed: {Path(r.source.uri or 'upload').name}{at}, "
                               f"{_failure_reason(r.error)}{tag}", ok=False, source=analysis_source())
                    r.error = _hosted_error(r.error, self.settings)
        return _results(rs)

    # Tracks and their cards.

    def card_body(self, c: sqlite3.Connection, track_id: int) -> dict[str, Any]:
        card = db.load_card(c, track_id)
        row = c.execute("SELECT analyzed_at FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if card is None:
            raise HTTPException(404, "no such track")
        card.source.title = _unprefixed(card.source.title, card.source.uri)
        body = {"id": track_id, **card.to_dict(), "metadata": describe(card, row[0] if row else None)}
        # Waveform/spectrogram data is large, so the UI fetches it from /views only when a view needs it.
        views = body["extras"].pop("views", None)
        body["has_views"] = views is not None
        body["has_audio"] = self.audio_path(card) is not None
        body["stems_available"] = stems.available()
        body["stems_state"] = self.stem_jobs.status(self.stems_dir(card, track_id))["state"]
        body["drum_model_available"] = drum_jobs.available()
        body["drum_model_state"] = self.drum_runs.status(track_id)["state"]
        body["lookups"] = lookup.describe(card, db.get_lookups(c, track_id))
        body["lookup_providers"] = [{"name": p.name, "label": p.label} for p in lookup.enabled()]
        return body

    @staticmethod
    def stems_dir(card, track_id: int) -> Path:
        return stems.stems_dir(card.source.sha256, track_id)

    def card_or_404(self, track_id: int):
        with closing(self.conn()) as c:
            card = db.load_card(c, track_id)
        if card is None:
            raise HTTPException(404, "no such track")
        return card

    @staticmethod
    def track_or_404(c: sqlite3.Connection, track_id: int) -> None:
        if c.execute("SELECT 1 FROM tracks WHERE id = ?", (track_id,)).fetchone() is None:
            raise HTTPException(404, "no such track")

    @staticmethod
    def audio_path(card) -> Path | None:
        """Where the track's audio is: on disk, or (only) in the audio bucket; None when it's gone."""
        p = card.source.local_path
        return Path(p) if blobs.exists(p) else None
