"""Web UI + JSON API. See docs/modules/ui.md."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager, closing, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import Body, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import db, drum_jobs, lookup, service, stems
from ..engine import browser_card, rust
from . import watch
from .analysis_jobs import AnalysisJobs, JobError, new_id, ref
from ..filemeta import describe, legacy_upload_prefix
from ..ingest import AUDIO_EXTENSIONS, IngestError, is_url, resolve_inputs
from .hosted import (HEALTH_PATH, LINK_DAYS, PUBLIC_PREFIX, TOKEN_HEADER, AccessVerifier, AnalysisSlots, LinkSigner, audio_minutes, names,
                     Settings, expire_audio, file_bytes, inline_script_hashes, security_headers, stored_bytes, today)

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger(__name__)
# Types the stdlib doesn't know everywhere; browsers need them to decode the stream.
AUDIO_TYPES = {".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".opus": "audio/ogg",
               ".ogg": "audio/ogg", ".aif": "audio/aiff", ".aiff": "audio/aiff", ".wav": "audio/wav", ".mp3": "audio/mpeg"}
MAX_QUERY_ROWS = 5000
QUERY_SECONDS = 2.0  # hosted: a query that runs longer is stopped
MAX_SEARCHES, MAX_SEARCH_BYTES = 100, 20_000  # saved searches per person, and the size of one
# The Strudel sketch ("Try this in Strudel") loads the original audio and its stems from strudel.cc, so only
# those endpoints answer CORS, and only for that origin.
STRUDEL_ORIGINS = ("https://strudel.cc",)
# Every route about one track; on a hosted server only its owner gets past the login middleware.
TRACK_PATH = re.compile(r"^/api/tracks/(\d+)(?:/|$)")
EXPIRY_EVERY = 6 * 3600  # seconds between sweeps for expired audio on a hosted server
# TASTR_BROWSER_ENGINE=1: the page runs the whole Rust engine (WebAssembly, static/engine.js) and sends the finished
# card with the audio (tastr/engine/browser_card.py); the server stores it when it checks out.
CARD_NAME = "card.json"


def _strudel_cors(origin: str | None, preflight: bool = False) -> dict[str, str]:
    if origin not in STRUDEL_ORIGINS:
        return {"Vary": "Origin"}
    headers = {"Access-Control-Allow-Origin": origin, "Vary": "Origin",
               "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges"}
    if preflight:
        # Chrome's Private Network Access preflight asks before a public page reaches localhost.
        headers |= {"Access-Control-Allow-Methods": "GET, HEAD", "Access-Control-Allow-Headers": "Range",
                    "Access-Control-Allow-Private-Network": "true", "Access-Control-Max-Age": "600"}
    return headers


class UrlBody(BaseModel):
    url: str


class QueryBody(BaseModel):
    sql: str
    mine: bool = False  # hosted admins: only your own library, like everyone else (the Simple search, whose rows open cards)


class SearchBody(BaseModel):
    mode: Literal["simple", "sql"]
    name: str = Field(min_length=1, max_length=40)
    # simple: {groups, join, sort}; sql: {sql}. Stored as given; the page builds the SQL itself.
    data: dict[str, Any]


class LimitsBody(BaseModel):
    daily_tracks: int | None = Field(None, ge=0, le=10_000)  # None = the server default
    daily_stems: int | None = Field(None, ge=0, le=1_000)
    scope: Literal["today", "standing"] = "today"  # today only (ends at 00:00 UTC), or until an admin changes it


EMAIL = re.compile(r"^[^@\s/]{1,64}@[^@\s/]{1,190}$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VISIT_IDLE = 30 * 60  # seconds without a request before the next one counts as a new visit
USAGE_RANGES = {"1": 1, "7": 7, "30": 30, "all": 365}  # the admin chart's ranges in days; "all" stops at a year


class VersionBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    edits: dict[str, Any]


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


def _cross_site(request) -> bool:
    """A browser request sent by a page on another site (Sec-Fetch-Site, else an Origin for another host).
    Requests without either header (curl, scripts, tests) aren't browser cross-site requests."""
    site = request.headers.get("sec-fetch-site")
    if site:
        return site not in ("same-origin", "none")
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return origin == "null"
    return urlparse(origin).netloc != request.headers.get("host", "")


def versioned_index() -> str:
    """index.html with `?v=<content hash>` on each /static/ file it loads.

    Browsers and Cloudflare cache app.js and style.css for hours when no Cache-Control is sent; a new
    hash after a deploy is a new URL, so the new files load right away."""
    def tag(m: re.Match) -> str:
        f = STATIC_DIR / m.group(2)
        if not f.is_file():
            return m.group(0)
        return f'{m.group(1)}/static/{m.group(2)}?v={hashlib.sha256(f.read_bytes()).hexdigest()[:10]}"'
    return re.sub(r'((?:src|href)=")/static/([\w.-]+)"', tag, (STATIC_DIR / "index.html").read_text())


def create_app(db_path: str | Path | None = None, settings: Settings | None = None, verifier=None) -> FastAPI:
    """`settings` default to the environment (TASTR_HOSTED etc.); `verifier` maps an Access token to an email
    (tests pass a stub; hosted mode otherwise checks tokens against the Access team's keys)."""
    settings = settings or Settings.from_env()
    hosted = settings.hosted
    browser_engine = browser_card.enabled()
    if hosted and verifier is None:
        verifier = AccessVerifier(settings.access_team_domain, settings.access_aud)
    slots = AnalysisSlots(settings.max_analyses) if hosted else None
    # Without TASTR_LINK_SECRET the Strudel links last only until the server restarts.
    signer = LinkSigner(settings.link_secret or secrets.token_bytes(32)) if hosted else None
    extra_headers = security_headers(inline_script_hashes((STATIC_DIR / "index.html").read_text())) if hosted else {}

    @asynccontextmanager
    async def lifespan(_app):
        stop = threading.Event()
        jobs.resume()  # stem separations the last run left unfinished (e.g. the machine auto-stopped)
        cut = analyses.resume()  # analyses it left unfinished read as failed
        if hosted:
            log_start(cut)
        drum_runs.resume()  # and drum model runs
        if hosted and settings.audio_days > 0:
            threading.Thread(target=expire_loop, args=(stop,), name="tastr-expiry", daemon=True).start()
        if hosted and settings.watch_minutes > 0 and ((settings.github_repo and settings.github_workflows)
                                                       or os.environ.get("LITESTREAM_BUCKET")):
            threading.Thread(target=watch_loop, args=(stop,), name="tastr-watch", daemon=True).start()
        yield
        stop.set()

    app = FastAPI(title="Tastr", docs_url=None if hosted else "/docs", redoc_url=None if hosted else "/redoc",
                  openapi_url=None if hosted else "/openapi.json", lifespan=lifespan)
    jobs = stems.Jobs()
    analyses = AnalysisJobs(tastr_home() / "analysis-jobs")

    def run_drum_model(track_id: int) -> None:
        """Re-analyze one track with the drum model (a drum_jobs job); raises with a message for the UI."""
        with closing(conn()) as c:
            with analysis_slot():
                result = service.reanalyze_track(c, track_id, drum_model=True)
        if result is None:
            raise LookupError("The track was deleted.")
        if result.error:
            if hosted:
                log.warning("drum model run for track %s failed: %s", track_id, result.error)
                raise RuntimeError("Couldn't analyze the file with the drum model.")
            raise RuntimeError(f"Couldn't analyze the file with the drum model: {result.error}")

    drum_runs = drum_jobs.Jobs(run_drum_model)
    last_seen: dict[str, str] = {}  # email -> time of their last request, since the server started
    snapshot_day = [""]  # the UTC day storage was last recorded for (storage_days)

    @app.middleware("http")
    async def same_origin_writes(request, call_next):
        # Any page open in the browser could otherwise POST to the local server (analyze a path, delete a
        # track, spend lookup quota). This is not authentication: scripts and curl still get through.
        if request.method not in ("GET", "HEAD", "OPTIONS") and _cross_site(request):
            return JSONResponse({"detail": "Cross-site requests are not allowed."}, status_code=403)
        return await call_next(request)

    if hosted:
        # Added last, so it runs first: nothing else sees a request without a valid Cloudflare Access login.
        @app.middleware("http")
        async def access_login(request, call_next):
            # /pub/ links check their own signature instead (strudel.cc can't sign in).
            if request.url.path != HEALTH_PATH and not request.url.path.startswith(PUBLIC_PREFIX):
                token = request.headers.get(TOKEN_HEADER) or request.cookies.get("CF_Authorization")
                user = None
                if token:
                    try:
                        user = await run_in_threadpool(verifier, token)
                    except Exception as e:  # expired, wrong audience, bad signature, keys unreachable
                        log.info("rejected an Access token: %s", type(e).__name__)
                if not user:
                    await run_in_threadpool(count_blocked)
                    return JSONResponse({"detail": "Sign in through the Tastr link you were invited to."},
                                        status_code=401, headers=extra_headers)
                request.state.user = user
                now = datetime.now(timezone.utc)
                before = last_seen.get(user)
                last_seen[user] = now.isoformat(timespec="seconds")
                if before is None or (now - datetime.fromisoformat(before)).total_seconds() > VISIT_IDLE:
                    await run_in_threadpool(count_visit, user)
                if snapshot_day[0] != today():
                    snapshot_day[0] = today()
                    await run_in_threadpool(snapshot_storage)
                m = TRACK_PATH.match(request.url.path)
                if m and not await run_in_threadpool(owns, user, int(m[1])):
                    # 404, not 403: someone else's track looks the same as one that doesn't exist.
                    return JSONResponse({"detail": "no such track"}, status_code=404, headers=extra_headers)
            response = await call_next(request)
            response.headers.update(extra_headers)
            return response

    def conn() -> sqlite3.Connection:
        return db.connect(db_path)

    def expire_loop(stop: threading.Event) -> None:
        while True:
            try:
                with closing(conn()) as c:
                    gone = expire_audio(c, upload_dir(), settings.audio_days, settings.admins)
                snapshot_storage()
                if gone:
                    log.info("Deleted the audio of %d track(s) older than %g days", len(gone), settings.audio_days)
            except Exception:  # noqa: BLE001 - try again next time
                log.exception("Deleting expired audio failed")
            if stop.wait(EXPIRY_EVERY):
                return

    def watch_once(fetch_runs=watch.fetch_runs, snapshots=watch.litestream_snapshots) -> None:
        """Log GitHub Actions runs on main and Litestream snapshots that are new since the last check."""
        now = datetime.now(timezone.utc)
        with closing(conn()) as c:
            if settings.github_repo and settings.github_workflows:
                try:
                    runs = fetch_runs(settings.github_repo, settings.github_token)
                except Exception as e:  # noqa: BLE001 - offline, rate-limited or a bad token: try next time
                    log.warning("Checking GitHub Actions failed: %s", e)
                else:
                    last = db.get_meta(c, watch.LAST_RUN)
                    lines, newest = watch.run_events(runs, int(last) if last else None, now, settings.github_workflows)
                    for e in lines:
                        db.add_event(c, **e)
                    if newest:
                        db.set_meta(c, watch.LAST_RUN, str(newest))
            if os.environ.get("LITESTREAM_BUCKET"):
                snaps, error = None, None
                try:
                    snaps = snapshots(str(db_path or db.default_db_path()))
                except Exception as e:  # noqa: BLE001 - logged as a problem line, once a day
                    error = str(e) or type(e).__name__
                lines, newest, day = watch.backup_events(snaps, error, db.get_meta(c, watch.LAST_SNAPSHOT),
                                                         db.get_meta(c, watch.PROBLEM_DAY), now)
                for e in lines:
                    db.add_event(c, **e)
                db.set_meta(c, watch.LAST_SNAPSHOT, newest)
                db.set_meta(c, watch.PROBLEM_DAY, day)

    def watch_loop(stop: threading.Event) -> None:
        if stop.wait(60):  # let the server finish starting (and Litestream its first sync)
            return
        while True:
            try:
                watch_once()
            except Exception:  # noqa: BLE001 - try again next time
                log.exception("Checking GitHub and the R2 backup failed")
            if stop.wait(settings.watch_minutes * 60):
                return

    app.state.watch_once = watch_once  # for tests

    def count_visit(user: str) -> None:
        """A visit is a person's first request after VISIT_IDLE without one; their very first is a first sign-in."""
        with closing(conn()) as c:
            first = not db.has_usage(c, user)
            db.add_usage(c, user, "visits", today())
        if first:
            event(user, "signin", "Signed in for the first time", source="cloudflare")

    def snapshot_storage(rows: list[dict[str, Any]] | None = None) -> None:
        """Record today's bytes stored per person (storage_days), for the admin view's Storage chart."""
        try:
            with closing(conn()) as c:
                rows = rows if rows is not None else scan_library(c)
                by: dict[str | None, int] = {}
                for r in rows:
                    by[r["owner"]] = by.get(r["owner"], 0) + r["bytes"]
                db.snapshot_storage(c, today(), by)
        except Exception:  # noqa: BLE001 - the chart misses a day; nothing else depends on it
            log.exception("Recording storage per person failed")

    def scan_library(c: sqlite3.Connection) -> list[dict[str, Any]]:
        """Each track's owner, upload time, last analysis and bytes on disk (audio, and stems separately)."""
        rows = []
        for owner, tid, sha, path, at, created in c.execute(
                "SELECT owner, id, sha256, local_path, analyzed_at, created_at FROM tracks"):
            audio, folder = file_bytes(path), stems.stems_dir(sha, tid)
            stem_b = sum(f.stat().st_size for f in folder.iterdir() if f.is_file()) if folder.is_dir() else 0
            rows.append({"owner": owner, "analyzed_at": at, "created_at": created or at, "audio": audio,
                         "stems": stem_b, "bytes": audio + stem_b})
        return rows

    def count_blocked() -> None:
        with closing(conn()) as c:
            db.add_blocked(c, today())

    def event(user: str | None, kind: str, detail: str, ok: bool = True, link: str | None = None,
              source: str = "app") -> None:
        """A line in the admin activity log (hosted only; your own machine keeps no log). `source`: where it
        happened, one of db.EVENT_SOURCES."""
        if hosted:
            with closing(conn()) as c:
                db.add_event(c, user, kind, detail, ok, link, source)

    def runs_on(backend: str) -> str:
        return "modal" if backend == "modal" else "app"

    def analysis_source() -> str:
        return runs_on(os.environ.get("TASTR_ANALYSIS_BACKEND", "local"))

    def cap(c: sqlite3.Connection, user: str, kind: str) -> int:
        """Today's cap on `kind` ("tracks" or "stems") for `user`: an admin's change to it, else the server's."""
        return db.daily_cap(c, user, kind, today(), settings.daily_tracks if kind == "tracks" else settings.daily_stems)

    def require_admin(request: Request) -> str:
        if not hosted or request.state.user not in settings.admins:
            raise HTTPException(404, "Not Found")
        return request.state.user

    def refuse(user: str, status: int, message: str, limit: str, name: str | None = None):
        """Refuse a request over one of the hosted limits, and log which limit it hit (and for which file)."""
        event(user, "limit", f"Hit the {limit}" + (f": {name}" if name else ""), ok=False)
        return HTTPException(status, message)

    def log_start(cut: list[dict[str, Any]]) -> None:
        """Admin log lines when the server starts: a new version deployed, and each analysis a restart cut off.
        A plain wake-up after Fly stopped an idle machine logs nothing."""
        marker, version = tastr_home() / "server-version", settings.fly_image or settings.commit
        host = "flyio" if settings.fly_app else "app"
        try:
            before = marker.read_text().strip()
        except OSError:
            before = None
        deployed = bool(version) and before is not None and before != version
        if version and before != version:
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(version)
            except OSError:
                pass
        if deployed:
            what = f"{settings.commit[:7]}: {settings.commit_subject[:100]}" if settings.commit_subject else settings.commit[:7]
            event(None, "deploy", f"Deployed {what or 'a new version'} (the server restarted)", link=settings.deploy_run or None,
                  source=host)
        elif cut:
            event(None, "restart", "The server restarted in the middle of an analysis (it crashed or its machine was stopped)",
                  ok=False, source=host)
        for job in cut:
            event(job.get("owner"), "failed", f"Cut off by the restart: {names(job.get('files') or ['upload'])}, "
                  f"while at step {job.get('stage') or 'waiting'} (ref {ref(job['id'])})", ok=False)

    def owns(user: str, track_id: int) -> bool:
        with closing(conn()) as c:
            exists, owner = db.track_owner(c, track_id)
        return exists and owner == user

    def owner_of(request: Request) -> str | None:
        """Whose library a request reads and writes: the signed-in email when hosted, else everything."""
        return request.state.user if hosted else None

    def results(rs, user: str | None = None, job: str | None = None, stages: list | None = None) -> dict[str, Any]:
        if hosted:  # error text can name server paths; keep it in the log
            for i, r in enumerate(rs):
                if r.error:
                    tag = f" (ref {ref(job)})" if job else ""
                    log.warning("analysis failed for %s%s: %s", r.source.uri, tag, r.error)
                    at = f" at step {stages[i]}" if stages and i < len(stages) and stages[i] else ""
                    event(user, "failed", f"Analysis failed: {Path(r.source.uri or 'upload').name}{at}, "
                          f"{_failure_reason(r.error)}{tag}", ok=False, source=analysis_source())
                    r.error = _hosted_error(r.error, settings)
        return _results(rs)

    def analysis_slot():
        return slots or nullcontext()

    @app.get("/api/config")
    def config(request: Request):
        """What this server allows, so the UI can hide what's turned off. On a hosted server the daily limits are
        the caller's own (an admin may have changed theirs), and admins also get the Cloudflare dashboard links."""
        user = getattr(request.state, "user", None)
        admin = hosted and user in settings.admins
        out = {"hosted": hosted, "links": not hosted, "query": True, "query_scope": "all" if not hosted or user in settings.admins else "mine", "user": user,
               "admin": admin, "limits": settings.limits() if hosted else None, "browser_engine": browser_engine_cfg()}
        if hosted:
            with closing(conn()) as c:  # this person's own caps, with any change an admin made for them
                out["limits"] |= {"daily_tracks": cap(c, user, "tracks"), "daily_stems": cap(c, user, "stems")}
        if admin:
            out["cf_links"] = settings.cf_links()  # {} without TASTR_CF_ACCOUNT_ID
        return out

    def browser_engine_cfg() -> dict | None:
        """Where the page gets the engine's WebAssembly build and which build it is, when browser cards are on."""
        wid = rust.wasm_id() if browser_engine else None
        return {"wasm": f"/engine/{rust.WASM_NAME}?v={wid}", "engine": wid} if wid else None

    @app.get(f"/engine/{rust.WASM_NAME}")
    def engine_wasm():
        """The Rust engine's WebAssembly build (TASTR_BROWSER_ENGINE). Its URL carries its hash, so it caches."""
        path = rust.wasm_path() if browser_engine else None
        if path is None:
            raise HTTPException(404, "Not Found")
        return FileResponse(path, media_type="application/wasm",
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/api/admin/usage")
    def admin_usage(request: Request, rng: str = Query("30", alias="range")):
        """Usage per person, the server's limits, analyses running or waiting, the last 20 events and today's blocked requests,
        for the admin view, plus `series`: each person's tracks, stem jobs, visits and bytes stored per day of `range`
        (7, 30 or all) or per hour of today (1). Hosted servers only, and only for TASTR_ADMIN_EMAILS; everyone else gets 404."""
        require_admin(request)
        if rng not in USAGE_RANGES:
            raise HTTPException(400, f"range is one of {', '.join(USAGE_RANGES)}.")
        day = today()
        people: dict[str | None, dict[str, Any]] = {}

        def person(email):
            return people.setdefault(email, {"email": email, "tracks": 0, "audio_bytes": 0, "stems_bytes": 0,
                                             "last_upload": None, "last_seen": last_seen.get(email),
                                             "today": {"tracks": 0, "stems": 0, "drums": 0, "visits": 0}})

        with closing(conn()) as c:
            library = scan_library(c)
            for r in library:
                p = person(r["owner"])
                p["tracks"] += 1
                p["audio_bytes"] += r["audio"]
                p["stems_bytes"] += r["stems"]
                p["last_upload"] = max(p["last_upload"] or "", r["analyzed_at"])
            snapshot_storage(library)
            history = [{"day": d, "user": u, "kind": k, "n": n} for d, u, k, n in c.execute(
                "SELECT day, user, kind, n FROM user_usage WHERE day >= date('now', '-29 days') ORDER BY day, user, kind")]
            recent = db.recent_events(c)
            problems = db.recent_events(c, problems=True)
            blocked = db.blocked_on(c, day)
            changed = db.overrides(c, day)
            series = usage_series(c, rng, day, library)
        for h in history:
            p = person(h["user"])
            if h["day"] == day:
                p["today"][h["kind"]] = h["n"]
        for email in [*settings.admins, *changed]:
            person(email)
        for p in people.values():
            if p["email"]:
                o = changed.get(p["email"])
                p["override"] = o
                p["caps"] = {k: settings.daily_tracks if k == "tracks" else settings.daily_stems for k in ("tracks", "stems")}
                for k in p["caps"]:
                    if o and o[f"daily_{k}"] is not None:
                        p["caps"][k] = o[f"daily_{k}"]
        users = sorted(people.values(), key=lambda p: (p["email"] is None, p["email"] or ""))
        return {"today": day, "users": users, "history": history, "admins": list(settings.admins),
                "limits": settings.limits(), "recent": recent, "problems": problems, "blocked_today": blocked,
                "log_links": settings.log_links(os.environ.get("TASTR_MODAL_APP", "tastr-web")),
                "storage": {"used_bytes": stored_bytes(tastr_home()), "cap_bytes": int(settings.storage_gb * 1024 ** 3)},
                "active_stem_jobs": jobs.active(), "active_drum_jobs": drum_runs.active(), "analyses": slots.status(),
                "series": series}

    def usage_series(c: sqlite3.Connection, rng: str, day: str, library: list[dict[str, Any]]) -> dict[str, Any]:
        """Per person: tracks, stems, visits and storage (bytes) for each bucket of the range, oldest first.
        Today: 24 UTC hours; storage there counts each track from the hour it was uploaded, at today's size.
        Days: storage is each day's last snapshot (storage_days), carried forward over days without one."""
        kinds = ("tracks", "stems", "visits")
        out: dict[str, dict[str, list]] = {}
        if rng == "1":
            n, now_hour = 24, datetime.now(timezone.utc).hour

            def row(email):
                return out.setdefault(email, {k: [0] * n for k in (*kinds, "storage")})
            for x in db.usage_hours(c, day):
                if x["kind"] in kinds:
                    row(x["user"])[x["kind"]][x["hour"]] += x["n"]
            for r in library:
                if not r["owner"]:
                    continue
                created = r["created_at"] or ""
                first = int(created[11:13]) if created[:10] == day and created[11:13].isdigit() else 0
                if created[:10] > day:
                    continue
                st = row(r["owner"])["storage"]
                for i in range(first, now_hour + 1):
                    st[i] += r["bytes"]
            return {"range": rng, "hourly": True, "now_hour": now_hour, "labels": list(range(n)), "people": out}
        span = USAGE_RANGES[rng]
        start = (datetime.fromisoformat(day) - timedelta(days=span - 1)).date().isoformat()
        if rng == "all":  # from the first day anything was counted, at most a year back
            first = min(filter(None, [c.execute("SELECT min(day) FROM user_usage").fetchone()[0], db.first_storage_day(c), day]))
            start = max(start, first)
        labels = []
        d = datetime.fromisoformat(start)
        while d.date().isoformat() <= day:
            labels.append(d.date().isoformat())
            d += timedelta(days=1)
        at = {lab: i for i, lab in enumerate(labels)}

        def row(email):
            return out.setdefault(email, {k: [0] * len(labels) for k in (*kinds, "storage")})
        for dd, u, k, nn in c.execute("SELECT day, user, kind, n FROM user_usage WHERE day >= ?", (start,)):
            if k in kinds and dd in at:
                row(u)[k][at[dd]] += nn
        # the last snapshot on or before the range start carries into it
        before = {o or None: b for o, b in c.execute(
            "SELECT owner, bytes FROM storage_days WHERE day = (SELECT max(day) FROM storage_days WHERE day < ?)", (start,))}
        snaps: dict[str | None, dict[str, int]] = {}
        for x in db.storage_history(c, start):
            snaps.setdefault(x["owner"], {})[x["day"]] = x["bytes"]
        for owner in {*before, *snaps}:
            if not owner:
                continue
            st, last = row(owner)["storage"], before.get(owner, 0)
            for i, lab in enumerate(labels):
                last = snaps.get(owner, {}).get(lab, last)
                st[i] = last
        return {"range": rng, "hourly": False, "labels": labels, "people": out}

    @app.get("/api/admin/events")
    def admin_events(request: Request, type: str = "", who: str = "", source: str = "", since: str = Query("", alias="from"),
                     until: str = Query("", alias="to"), problems: bool = False, limit: int = Query(200, ge=1, le=1000)):
        """The admin activity log, filtered on the server: type (db.EVENT_TYPES), who (an email, or "-" for lines
        with no person), source (db.EVENT_SOURCES), from/to (UTC days, both included) and problems (not OK only).
        Blocked requests show as one Cloudflare line per day. Admins only."""
        require_admin(request)
        if type and type not in db.EVENT_TYPES:
            raise HTTPException(400, f"type is one of {', '.join(db.EVENT_TYPES)}.")
        if source and source not in db.EVENT_SOURCES:
            raise HTTPException(400, f"source is one of {', '.join(db.EVENT_SOURCES)}.")
        if any(v and not DAY.match(v) for v in (since, until)):
            raise HTTPException(400, "from and to are days, like 2026-10-31.")
        with closing(conn()) as c:
            return db.find_events(c, type, who, source, since, until, problems, limit)

    @app.put("/api/admin/limits/{email}")
    def set_limits(email: str, body: LimitsBody, request: Request):
        """Change one person's daily track and stem job limits, today only or until changed (None = the default).
        Logged as an admin change. Admins only."""
        admin = require_admin(request)
        email = email.strip().lower()
        if not EMAIL.match(email):
            raise HTTPException(400, "That isn't an email address.")
        day = today()
        with closing(conn()) as c:
            db.set_override(c, email, body.daily_tracks, body.daily_stems, day if body.scope == "today" else None, admin)
            o = db.get_override(c, email, day)
        who = email.split("@")[0]
        if o is None:
            event(admin, "admin", f"Set {who}'s daily limits back to the defaults")
        else:
            t = body.daily_tracks if body.daily_tracks is not None else settings.daily_tracks
            st = body.daily_stems if body.daily_stems is not None else settings.daily_stems
            event(admin, "admin", f"Set {who}'s daily limits to {t} tracks, {st} stem jobs "
                  f"({'today only' if body.scope == 'today' else 'until changed'})")
        return {"email": email, "override": o}

    @app.delete("/api/admin/limits/{email}")
    def reset_limits(email: str, request: Request):
        """Put one person back on the server's daily limits. Admins only."""
        return set_limits(email, LimitsBody(), request)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/bench")
    def bench_scores():
        # Headline benchmark scores the Glossary quotes; `tastr bench ... --record` keeps the file current.
        from ..engine.bench import scores

        return scores.load()

    @app.get("/api/patterns")
    def patterns():
        # The engine's template drum patterns per style, for the Glossary's pattern grids.
        from ..engine.stages.suggestions import STYLE_LABELS, VARIATIONS

        return [{"style": STYLE_LABELS[style], "name": name, "pattern": pat}
                for style, items in VARIATIONS.items() for name, pat in items]

    @app.get("/api/tracks")
    def tracks(request: Request):
        with closing(conn()) as c:
            rows = db.list_tracks(c, owner_of(request))
            uris = dict(c.execute("SELECT id, source_uri FROM tracks").fetchall())
            vibes: dict[int, list[str]] = {}
            for tid, tag in c.execute("SELECT track_id, tag FROM tags ORDER BY score DESC"):
                vibes.setdefault(tid, []).append(tag)
        for r in rows:  # tracks uploaded before the hash moved into a folder still have it in their title
            r["title"] = _unprefixed(r["title"], uris.get(r["id"]))
            r["tags"] = vibes.get(r["id"], [])
        return rows

    def card_body(c: sqlite3.Connection, track_id: int) -> dict[str, Any]:
        card = db.load_card(c, track_id)
        row = c.execute("SELECT analyzed_at FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if card is None:
            raise HTTPException(404, "no such track")
        card.source.title = _unprefixed(card.source.title, card.source.uri)
        body = {"id": track_id, **card.to_dict(), "metadata": describe(card, row[0] if row else None)}
        # Waveform/spectrogram data is large, so the UI fetches it from /views only when a view needs it.
        views = body["extras"].pop("views", None)
        body["has_views"] = views is not None
        body["has_audio"] = _audio_path(card) is not None
        body["stems_available"] = stems.available()
        body["stems_state"] = jobs.status(_stems_dir(card, track_id))["state"]
        body["drum_model_available"] = drum_jobs.available()
        body["drum_model_state"] = drum_runs.status(track_id)["state"]
        body["lookups"] = lookup.describe(card, db.get_lookups(c, track_id))
        body["lookup_providers"] = [{"name": p.name, "label": p.label} for p in lookup.enabled()]
        return body

    def _stems_dir(card, track_id: int) -> Path:
        return stems.stems_dir(card.source.sha256, track_id)

    def _card_or_404(track_id: int):
        with closing(conn()) as c:
            card = db.load_card(c, track_id)
        if card is None:
            raise HTTPException(404, "no such track")
        return card

    def _audio_path(card) -> Path | None:
        p = card.source.local_path
        return Path(p) if p and Path(p).is_file() else None

    @app.get("/api/tracks/{track_id}")
    def track(track_id: int):
        with closing(conn()) as c:
            return card_body(c, track_id)

    @app.get("/api/tracks/{track_id}/audio")
    def track_audio(track_id: int, origin: str | None = Header(None)):
        """The original audio file, with Range support so the browser can seek. strudel.cc may fetch it too."""
        with closing(conn()) as c:
            card = db.load_card(c, track_id)
        if card is None:
            raise HTTPException(404, "no such track")
        path = _audio_path(card)
        if path is None:
            raise HTTPException(404, "The original audio file is not on disk.")
        media = AUDIO_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media, headers=_strudel_cors(origin))

    def _preflight(origin: str | None) -> Response:
        if origin not in STRUDEL_ORIGINS:
            return Response(status_code=403, headers=_strudel_cors(origin))
        return Response(status_code=204, headers=_strudel_cors(origin, preflight=True))

    @app.options("/api/tracks/{track_id}/audio", include_in_schema=False)
    def track_audio_preflight(track_id: int, origin: str | None = Header(None)):
        return _preflight(origin)

    @app.options("/api/tracks/{track_id}/stems/{name}", include_in_schema=False)
    def track_stem_preflight(track_id: int, name: str, origin: str | None = Header(None)):
        return _preflight(origin)

    @app.get("/api/tracks/{track_id}/strudel-link")
    def strudel_link(track_id: int):
        """Where strudel.cc can fetch this track's audio; its stems are next to it (`.../stems/<name>`). On a
        shared server strudel.cc can't pass the login, so this is a signed link that works for LINK_DAYS days."""
        _card_or_404(track_id)
        if signer is None:
            return {"audio": f"/api/tracks/{track_id}/audio", "days": None}
        return {"audio": f"{PUBLIC_PREFIX}{signer.token(track_id)}/tracks/{track_id}/audio", "days": LINK_DAYS}

    def _signed(token: str, track_id: int) -> None:
        if signer is None or not signer.check(token, track_id):
            raise HTTPException(404, "This link has expired. Open the sketch again from Tastr.")

    @app.get(PUBLIC_PREFIX + "{token}/tracks/{track_id}/audio", include_in_schema=False)
    def signed_audio(token: str, track_id: int, origin: str | None = Header(None)):
        _signed(token, track_id)
        return track_audio(track_id, origin)

    @app.get(PUBLIC_PREFIX + "{token}/tracks/{track_id}/stems/{name}", include_in_schema=False)
    def signed_stem(token: str, track_id: int, name: str, origin: str | None = Header(None)):
        _signed(token, track_id)
        return track_stem(track_id, name, origin)

    @app.options(PUBLIC_PREFIX + "{token}/tracks/{track_id}/audio", include_in_schema=False)
    @app.options(PUBLIC_PREFIX + "{token}/tracks/{track_id}/stems/{name}", include_in_schema=False)
    def signed_preflight(token: str, track_id: int, origin: str | None = Header(None), name: str = ""):
        return _preflight(origin)

    def _stems_status(card, track_id: int) -> dict[str, Any]:
        return {**jobs.status(_stems_dir(card, track_id)), "available": stems.available()}

    @app.get("/api/tracks/{track_id}/stems")
    def track_stems(track_id: int):
        """Separation state: none, queued, running (with progress 0-1), ready (with the stem names) or error."""
        return _stems_status(_card_or_404(track_id), track_id)

    @app.post("/api/tracks/{track_id}/stems", status_code=202)
    def separate_stems(track_id: int, request: Request):
        """Start separating one track into stems in the background (one track at a time; others queue)."""
        card = _card_or_404(track_id)
        path = _audio_path(card)
        if path is None:
            raise HTTPException(404, "The original audio file is not on disk, so there is nothing to separate.")
        if not stems.available():
            raise HTTPException(501, stems.INSTALL_HINT)
        folder = _stems_dir(card, track_id)
        if not stems.is_ready(folder) and jobs.status(folder)["state"] not in ("queued", "running"):
            if hosted:
                user = request.state.user
                with closing(conn()) as c:
                    most = cap(c, user, "stems")
                    if db.usage_today(c, user, "stems", today()) >= most:
                        raise refuse(user, 429, f"You've reached today's limit of {most} stem separations.",
                                     f"daily stem limit ({most})")
                    db.add_usage(c, user, "stems", today())
                event(user, "stems", "Started separating stems for 1 track", source=runs_on(stems.backend()))
            jobs.start(path, folder)
        return _stems_status(card, track_id)

    @app.get("/api/tracks/{track_id}/stems/{name}")
    def track_stem(track_id: int, name: str, origin: str | None = Header(None)):
        """One stem as FLAC, like /audio: Range support, and strudel.cc may fetch it."""
        if name not in stems.STEM_NAMES:
            raise HTTPException(404, f"Stems are {', '.join(stems.STEM_NAMES)}.")
        path = stems.stem_path(_stems_dir(_card_or_404(track_id), track_id), name)
        if not path.is_file():
            raise HTTPException(404, "This track has not been separated into stems.")
        return FileResponse(path, media_type="audio/flac", headers=_strudel_cors(origin))

    def _drum_model_status(track_id: int) -> dict[str, Any]:
        return {**drum_runs.status(track_id), "available": drum_jobs.available()}

    @app.get("/api/tracks/{track_id}/drum-model")
    def track_drum_model(track_id: int):
        """The track's drum model run: none, queued, running, done or error (with `error`)."""
        _card_or_404(track_id)
        return _drum_model_status(track_id)

    @app.post("/api/tracks/{track_id}/drum-model", status_code=202)
    def detect_drums(track_id: int, request: Request):
        """Re-analyze the track with the pretrained drum model in the background (one track at a time; others
        queue). Saved edits move to the new sections as with Re-analyze."""
        card = _card_or_404(track_id)
        if _audio_path(card) is None:
            raise HTTPException(410, "The original audio file is no longer on disk.")
        if not drum_jobs.available():
            raise HTTPException(501, drum_jobs.INSTALL_HINT)
        if not drum_runs.busy(track_id):
            user = owner_of(request)
            if hosted:
                with closing(conn()) as c:
                    if db.usage_today(c, user, "drums", today()) >= settings.daily_drums:
                        raise refuse(user, 429, f"You've reached today's limit of {settings.daily_drums} drum model runs.",
                                     f"daily drum model limit ({settings.daily_drums})")
                    db.add_usage(c, user, "drums", today())
            event(user, "drums", "Started detecting drums with the model for 1 track", source=analysis_source())
            drum_runs.start(track_id)
        return _drum_model_status(track_id)

    @app.get("/api/tracks/{track_id}/views")
    def track_views(track_id: int):
        """Waveform and spectrogram data from the `views` stage (404 for cards analyzed before it existed)."""
        with closing(conn()) as c:
            card = db.load_card(c, track_id)
        if card is None:
            raise HTTPException(404, "no such track")
        views = card.extras.get("views")
        if views is None:
            raise HTTPException(404, "This track was analyzed before waveform data was stored.")
        return views

    @app.post("/api/tracks/{track_id}/retag")
    def retag(track_id: int):
        """Re-read one file track's embedded tags and audio info (`tastr retag` for one track)."""
        with closing(conn()) as c:
            result = db.retag_track(c, track_id)
            if result == "missing":
                raise HTTPException(404, "no such track")
            if result == "not_file":
                raise HTTPException(400, "Only uploaded files have tags to reload.")
            if result == "no_file":
                raise HTTPException(410, "The original file is no longer on disk.")
            body = card_body(c, track_id)
        return {"changed": result == "changed", "tags_found": bool(body["source"].get("file_tags")), "card": body}

    @app.post("/api/tracks/{track_id}/reanalyze")
    def reanalyze(track_id: int, request: Request, meter: str | None = None):
        """Run the full engine again on the track's stored audio file (same id, fresh card).

        `?meter=3/4` or `4/4` sets the time signature, `auto` goes back to detecting it; without it a meter
        the user set before is kept.
        """
        if meter not in (None, "3/4", "4/4", "auto"):
            raise HTTPException(400, "meter must be 3/4, 4/4 or auto")
        beats = service.KEEP if meter is None else None if meter == "auto" else int(meter[0])
        with closing(conn()) as c:
            try:
                with analysis_slot():
                    result = service.reanalyze_track(c, track_id, beats)
            except FileNotFoundError:
                raise HTTPException(410, "The original audio file is no longer on disk.")
            except TimeoutError:
                raise HTTPException(503, "The server is busy analyzing other tracks. Try again in a few minutes.")
            if result is None:
                raise HTTPException(404, "no such track")
            if result.error:
                if hosted:
                    log.warning("re-analysis of track %s failed: %s", track_id, result.error)
                    event(request.state.user, "failed", f"Re-analysis failed, {_failure_reason(result.error)}", ok=False)
                    raise HTTPException(422, "Couldn't analyze the file.")
                raise HTTPException(422, f"Couldn't analyze the file: {result.error}")
            event(owner_of(request), "reanalyze", "Re-analyzed 1 track")
            return {"card": card_body(c, track_id), "edits_dropped": result.edits_dropped}

    @app.post("/api/tracks/{track_id}/lookup")
    def lookup_metadata(track_id: int):
        """Ask the enabled catalogs (TASTR_LOOKUP) for this track's BPM and key again."""
        providers = lookup.enabled()
        if not providers:
            raise HTTPException(400, "No lookup providers are set up. See docs/lookup.md.")
        with closing(conn()) as c:
            card = db.load_card(c, track_id)
            if card is None:
                raise HTTPException(404, "no such track")
            if lookup.query_for(card) is None:
                raise HTTPException(422, "This track has no artist and title tags to look up.")
            results = lookup.lookup_track(c, track_id, card, providers)
            body = card_body(c, track_id)
            # Answers a provider's terms don't let us keep are shown this once, not stored.
            body["lookups"] += lookup.describe(card, [r for r in results if r.get("transient")])
            # "limited" (a guardrail held the request back) and "recent" (asked within the refresh window)
            # aren't stored; the UI says why nothing new came back.
            notes = [{"provider": r["provider"], "status": r["status"], "error": r.get("error")}
                     for r in results if r["status"] in ("limited", "recent", "error")]
            return {"card": body, "notes": notes}

    # Edits to the drums, bass and chords: the current state autosaves; versions are named copies.
    def _track_or_404(c: sqlite3.Connection, track_id: int) -> None:
        if c.execute("SELECT 1 FROM tracks WHERE id = ?", (track_id,)).fetchone() is None:
            raise HTTPException(404, "no such track")

    @app.get("/api/tracks/{track_id}/edits")
    def track_edits(track_id: int):
        with closing(conn()) as c:
            _track_or_404(c, track_id)
            return db.get_edits(c, track_id)

    @app.put("/api/tracks/{track_id}/edits", status_code=204)
    def save_track_edits(track_id: int, edits: dict[str, Any] = Body(...)):
        with closing(conn()) as c:
            _track_or_404(c, track_id)
            db.set_edits(c, track_id, edits)
        return Response(status_code=204)

    # Your own labels (tastr/labels.py): BPM, key and section starts set by hand, kept apart from the analysis.
    @app.get("/api/tracks/{track_id}/labels")
    def track_labels(track_id: int):
        with closing(conn()) as c:
            _track_or_404(c, track_id)
            return db.get_labels(c, track_id)

    @app.put("/api/tracks/{track_id}/labels")
    def save_track_labels(track_id: int, body: dict[str, Any] = Body(...)):
        from .. import labels

        try:
            clean = labels.clean(body)
        except labels.LabelError as e:
            raise HTTPException(400, str(e)) from None
        with closing(conn()) as c:
            _track_or_404(c, track_id)
            return db.set_labels(c, track_id, clean)

    @app.get("/api/review")
    def review(request: Request, limit: int = 20):
        from .. import labels

        with closing(conn()) as c:
            return labels.review_queue(c, max(1, min(limit, 1000)), owner_of(request))

    # Put a track in the review list yourself ("flagged"), take it out ("skipped"), or leave it to the queue (null).
    @app.put("/api/tracks/{track_id}/review")
    def set_review(track_id: int, body: dict[str, Any] = Body(...)):
        state = body.get("state")
        if state not in (None, *db.REVIEW_STATES):
            raise HTTPException(400, "state must be \"flagged\", \"skipped\" or null")
        with closing(conn()) as c:
            _track_or_404(c, track_id)
            db.set_review_flag(c, track_id, state)
        return {"state": state}

    # Your labels, newest first, with Tastr's reading when each was saved: the Review tab's log and its CSV.
    @app.get("/api/labels")
    def all_labels(request: Request):
        with closing(conn()) as c:
            rows = db.list_labels(c, owner_of(request))
        for r in rows:
            r.pop("path", None)  # a server path; the UI doesn't need it
        return rows[::-1]

    @app.get("/api/tracks/{track_id}/edit-versions")
    def edit_versions(track_id: int):
        with closing(conn()) as c:
            _track_or_404(c, track_id)
            return db.list_edit_versions(c, track_id)

    @app.post("/api/tracks/{track_id}/edit-versions", status_code=201)
    def add_edit_version(track_id: int, body: VersionBody):
        with closing(conn()) as c:
            _track_or_404(c, track_id)
            return db.add_edit_version(c, track_id, body.name.strip() or "Version", body.edits)

    @app.get("/api/tracks/{track_id}/edit-versions/{version_id}")
    def edit_version(track_id: int, version_id: int):
        with closing(conn()) as c:
            v = db.get_edit_version(c, track_id, version_id)
        if v is None:
            raise HTTPException(404, "no such version")
        return v

    @app.delete("/api/tracks/{track_id}/edit-versions/{version_id}", status_code=204)
    def delete_edit_version(track_id: int, version_id: int):
        with closing(conn()) as c:
            if not db.delete_edit_version(c, track_id, version_id):
                raise HTTPException(404, "no such version")
        return Response(status_code=204)

    @app.delete("/api/tracks/{track_id}", status_code=204)
    def delete_track(track_id: int):
        with closing(conn()) as c:
            card = db.load_card(c, track_id)
            if not db.delete_track(c, track_id):
                raise HTTPException(404, "no such track")
            # Stems are kept per audio file, so another library holding the same file keeps them.
            shared = card is not None and c.execute(
                "SELECT 1 FROM tracks WHERE sha256 = ?", (card.source.sha256,)).fetchone() is not None
        drum_runs.forget(track_id)
        if card is not None and not shared:
            folder = _stems_dir(card, track_id)
            jobs.forget(folder)
            stems.remove(folder)
        if hosted and card is not None:
            remove_upload(card.source.local_path)
        return Response(status_code=204)

    def remove_upload(path: str | None) -> None:
        """A deleted track's uploaded audio goes too (hosted only: on your own machine Tastr leaves files alone).
        Only Tastr's own copies under uploads/, and only when no other track points at the same file."""
        if not path:
            return
        p, root = Path(path).resolve(), upload_dir().resolve()
        if root not in p.parents:
            return
        with closing(conn()) as c:
            if c.execute("SELECT 1 FROM tracks WHERE local_path = ?", (path,)).fetchone():
                return
        p.unlink(missing_ok=True)
        if p.parent != root and not any(p.parent.iterdir()):
            p.parent.rmdir()

    # Plain `def` endpoints below run in FastAPI's threadpool, so slow analysis
    # doesn't block the event loop.
    @app.post("/api/analyze")
    def analyze(request: Request, files: list[UploadFile] = File(...), background: bool = False,
                card: list[UploadFile] | None = File(None)):
        """Analyze uploaded files and answer with the results. With ?background=1, answer at once with a job
        to poll at /api/analyze/jobs/{id}, which says which step each file is on (the page's progress bars).
        `card`: with TASTR_BROWSER_ENGINE on, the card the page made from a single uploaded file; the server
        stores it when it checks out and analyzes the file itself otherwise (or when it's missing or too big)."""
        if hosted:
            check_upload(request.state.user, files)
        paths: list[str] = []
        for f in files:
            name = re.sub(r"[^\w.\- ]", "_", Path(f.filename or "upload").name) or "upload"
            # The unique part is a folder, so the stored file keeps the name the user gave it (that is what the title falls back to).
            dest = upload_dir() / uuid.uuid4().hex[:8] / name
            dest.parent.mkdir()
            size = 0
            with open(dest, "wb") as out:
                while chunk := f.file.read(1 << 20):
                    size += len(chunk)
                    if hosted and size > settings.max_upload_bytes:
                        break
                    out.write(chunk)
            if hosted and size > settings.max_upload_bytes:
                discard([dest, *map(Path, paths)])
                raise refuse(request.state.user, 413, f"{name} is larger than {settings.max_upload_mb:g} MB.",
                             f"upload size limit ({settings.max_upload_mb:g} MB)", name)
            paths.append(str(dest))
            minutes = audio_minutes(dest) if hosted else None
            if minutes is not None and minutes > settings.max_minutes:
                discard(map(Path, paths))
                raise refuse(request.state.user, 413,
                             f"{name} is {minutes:.0f} minutes long. This server analyzes tracks up to "
                             f"{settings.max_minutes:g} minutes, so a long mix needs cutting into shorter files first.",
                             f"track length limit ({settings.max_minutes:g} min)", f"{name}, {minutes:.0f} min")
        sig = save_card(card[0], Path(paths[0])) if browser_engine and card and len(card) == len(paths) == 1 else {}
        given = [Path(p).name for p in paths]
        job_id = new_id()
        if hosted:
            event(request.state.user, "upload", f"Uploaded {names(given)}" + (f" (ref {ref(job_id)})" if background else ""))
        if background:
            user = getattr(request.state, "user", None)
            return JSONResponse(analyses.start(owner_of(request), given,
                                               analysis_work(paths, user, owner_of(request), job_id, cards=sig), job_id),
                                status_code=202)
        with closing(conn()) as c:
            try:
                with analysis_slot():
                    rs = service.analyze_inputs(c, paths, owner=owner_of(request), cards=sig)
            except TimeoutError:
                raise HTTPException(503, "The server is busy analyzing other tracks. Try again in a few minutes.")
            finally:
                drop_cards(sig)
            if hosted:  # only tracks that analyzed count toward the daily limit, so a failed try can be retried
                db.add_usage(c, request.state.user, "tracks", today(), sum(r.track_id is not None for r in rs))
            return results(rs, owner_of(request))

    def discard(files) -> None:
        """Nothing from a refused upload stays on disk."""
        for p in files:
            p.unlink(missing_ok=True)
            p.parent.rmdir()

    def save_card(upload: UploadFile, audio: Path) -> dict[str, str]:
        """Store the card the page made next to its audio until the analysis has read it: {audio: card file}.
        Nothing ({}) when it is too big; the server then analyzes the file itself."""
        dest, size = audio.parent / CARD_NAME, 0
        with open(dest, "wb") as out:
            while chunk := upload.file.read(1 << 20):
                size += len(chunk)
                if size > browser_card.MAX_BYTES:
                    break
                out.write(chunk)
        if size > browser_card.MAX_BYTES:
            dest.unlink(missing_ok=True)
            return {}
        return {str(audio): str(dest)}

    def drop_cards(sig: dict[str, str]) -> None:
        for path in sig.values():
            Path(path).unlink(missing_ok=True)

    def analysis_work(inputs: list[str], user: str | None, owner: str | None, job_id: str, link: bool = False,
                      cards: dict[str, str] | None = None):
        """The work of one background analysis job (see analysis_jobs.py), reporting each step as it starts.
        `cards`: {audio path: card file the page made} for uploads that came with one (deleted once read).
        Anything that stops the whole job goes in the admin log with the job's ref, as failed analyses do."""
        sig = {str(Path(k).resolve()): v for k, v in (cards or {}).items()}

        def work(progress) -> dict[str, Any]:
            try:
                return run(progress)
            except JobError as e:
                event(user, "failed", f"Analysis didn't start: {names([Path(i).name for i in inputs])}, {e} (ref {ref(job_id)})",
                      ok=False)
                raise
            except Exception as e:
                log.exception("analysis job %s failed", ref(job_id))
                event(user, "failed", f"Analysis job failed: {names([Path(i).name for i in inputs])}, "
                      f"{_failure_reason(f'{type(e).__name__}: {e}')} (ref {ref(job_id)})", ok=False)
                raise
            finally:
                drop_cards(sig)

        def run(progress) -> dict[str, Any]:
            with closing(conn()) as c:
                if link:
                    progress.stage("fetch")
                try:
                    sources = resolve_inputs(inputs)
                except IngestError as e:
                    raise JobError(str(e))
                progress.stage("waiting")
                rs, failed_at = [], []
                try:
                    with analysis_slot():
                        progress.running()
                        for i, source in enumerate(sources):
                            progress.stage("decode", i)
                            r = service.analyze_source(c, source, owner=owner, card=sig.get(source.local_path),
                                                       on_stage=lambda name, *_, i=i: progress.stage(name, i))
                            rs.append(r)
                            failed_at.append(progress.last_stage if r.error else None)
                except TimeoutError:
                    raise JobError("The server is busy analyzing other tracks. Try again in a few minutes.")
                if hosted:  # as for a plain upload: only tracks that analyzed count toward the daily limit
                    db.add_usage(c, user, "tracks", today(), sum(r.track_id is not None for r in rs))
            out = results(rs, user, job_id, failed_at)
            for item, stage in zip(out["results"], failed_at):
                if stage:
                    item["stage"] = stage
            return out
        return work

    @app.get("/api/analyze/jobs/{job_id}")
    def analysis_job(job_id: str, request: Request):
        """A background analysis: queued (with `position`), running (with `stage`), done (with `results`) or error."""
        job = analyses.status(job_id, owner_of(request))
        if job is None:
            raise HTTPException(404, "no such job")
        return job

    def check_upload(user: str, files: list[UploadFile]) -> None:
        if len(files) > settings.max_files:
            raise refuse(user, 400, f"Upload at most {settings.max_files} files at a time.",
                         f"files-per-upload limit ({settings.max_files})")
        for f in files:
            if Path(f.filename or "").suffix.lower() not in AUDIO_EXTENSIONS:
                raise HTTPException(415, f"{Path(f.filename or 'upload').name} isn't an audio file Tastr can read.")
        if stored_bytes(tastr_home()) >= settings.storage_gb * 1024 ** 3:
            raise refuse(user, 507, "This server is out of space for new uploads. Ask whoever invited you.",
                         f"storage cap ({settings.storage_gb:g} GB)")
        with closing(conn()) as c:
            used, most = db.usage_today(c, user, "tracks", today()), cap(c, user, "tracks")
        if used + len(files) > most:
            left = max(0, most - used)
            raise refuse(user, 429, f"You can analyze {most} tracks a day; {left} left today.",
                         f"daily track limit ({most})")

    @app.post("/api/analyze-url")
    def analyze_url(body: UrlBody, background: bool = False):
        if hosted:
            raise HTTPException(403, "Link analysis is turned off on this server. Upload the audio file instead.")
        if not is_url(body.url):  # never a path: anyone who can reach the server could read files off its disk
            raise HTTPException(400, "Paste an http(s) link to a track or playlist.")
        if background:
            job_id = new_id()
            return JSONResponse(analyses.start(None, [body.url], analysis_work([body.url], None, None, job_id, link=True), job_id),
                                status_code=202)
        with closing(conn()) as c:
            try:
                results = service.analyze_inputs(c, [body.url])
            except IngestError as e:
                raise HTTPException(400, str(e))
        return _results(results)

    query_slot = threading.Semaphore(1)  # hosted: one SQL query at a time, so nobody's query slows everyone else

    @app.post("/api/query")
    def run_query(body: QueryBody, request: Request):
        # Hosted: admins query the whole database; everyone else a copy of their own library (db.LIBRARY_TABLES).
        owner = request.state.user if hosted and (body.mine or request.state.user not in settings.admins) else None
        if hosted and not query_slot.acquire(blocking=False):
            raise HTTPException(429, "Another SQL query is running. Try again in a few seconds.")
        try:
            columns, rows = db.run_query(db_path or db.default_db_path(), body.sql, MAX_QUERY_ROWS,
                                         QUERY_SECONDS if hosted else 5.0, owner=owner)
        except sqlite3.Error as e:
            msg = "The query took too long and was stopped." if str(e) == "interrupted" else str(e)
            raise HTTPException(400, msg)
        finally:
            if hosted:
                query_slot.release()
        return {"columns": columns, "rows": rows}

    # Saved searches (Explore > Find tracks), each person's own: the signed-in email when hosted, else everyone's.
    @app.get("/api/searches")
    def searches(request: Request):
        with closing(conn()) as c:
            return db.list_searches(c, owner_of(request))

    @app.put("/api/searches")
    def save_search(body: SearchBody, request: Request):
        if len(json.dumps(body.data)) > MAX_SEARCH_BYTES:
            raise HTTPException(413, "That search is too big to save.")
        name, owner = body.name.strip(), owner_of(request)
        if not name:
            raise HTTPException(422, "Give the search a name.")
        with closing(conn()) as c:
            have = db.list_searches(c, owner)
            replaces = any(s["mode"] == body.mode and s["name"].lower() == name.lower() for s in have)
            if len(have) >= MAX_SEARCHES and not replaces:
                raise HTTPException(409, f"You have {MAX_SEARCHES} saved searches. Delete one first.")
            return db.save_search(c, owner, body.mode, name, body.data)

    @app.delete("/api/searches/{search_id}", status_code=204)
    def delete_search(search_id: int, request: Request):
        with closing(conn()) as c:
            if not db.delete_search(c, owner_of(request), search_id):
                raise HTTPException(404, "no such search")
        return Response(status_code=204)

    page = versioned_index()

    @app.get("/", include_in_schema=False)
    def index():
        # The page itself is never cached; its scripts and styles carry a content hash, so a deploy is seen at once.
        return HTMLResponse(page, headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


class _UvicornStyle(logging.Formatter):
    """`INFO:     message`, the same padded prefix uvicorn prints, so the lines line up."""

    def format(self, record: logging.LogRecord) -> str:
        record.levelprefix = f"{record.levelname}:".ljust(9)
        return super().format(record)


def configure_logging(level: str = "info") -> None:
    """Print Tastr's own log lines (failed analyses, stem jobs, and stage tracebacks at debug) next to uvicorn's."""
    handler = logging.StreamHandler()
    handler.setFormatter(_UvicornStyle("%(levelprefix)s %(name)s: %(message)s"))
    logger = logging.getLogger("tastr")
    logger.handlers[:] = [handler]
    logger.setLevel(level.upper())
    logger.propagate = False


def run(host: str = "127.0.0.1", port: int = 8000, db_path: str | None = None, log_level: str = "info") -> None:
    import uvicorn

    from .. import filemeta

    configure_logging(log_level)

    if filemeta.mutagen is None:
        print("Warning: mutagen is not installed, so file tags and audio info can't be read. Run: pip install -e .")
    try:
        settings = Settings.from_env()
    except ValueError as e:
        raise SystemExit(f"Error: {e}")
    if settings.hosted:
        print(f"Hosted mode: logins checked against {settings.access_team_domain}; link analysis and SQL are off.")
    n = db.refresh_file_meta(db.connect(db_path), only_if_stale=True)  # once per tag reader change
    if n:
        print(f"Refreshed file tags for {n} track(s)")
    uvicorn.run(create_app(db_path, settings), host=host, port=port, log_level=log_level)
