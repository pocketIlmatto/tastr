"""Web UI + JSON API. See docs/modules/ui.md.

`create_app` builds the shared state (context.py), the login and same-origin middleware, and mounts the API
from routes/, one module per area: tracks, media (audio, stems, drum model), files (sizes, zips, deleting stems),
edits and labels, analyze, search and admin."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .. import blobs, db
from ..engine import rust
from . import background
from .context import STATIC_DIR, AppContext, _failure_reason, _hosted_error, log  # noqa: F401 - tests import these
from .hosted import HEALTH_PATH, PUBLIC_PREFIX, TOKEN_HEADER, Settings, today
from .routes import admin, analyze, edits, files, lists, media, search, strudel, tracks

# Every route about one track; on a hosted server only its owner gets past the login middleware.
TRACK_PATH = re.compile(r"^/api/tracks/(\d+)(?:/|$)")
VISIT_IDLE = 30 * 60  # seconds without a request before the next one counts as a new visit


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

    Browsers and Cloudflare cache scripts and style.css for hours when no Cache-Control is sent; a new
    hash after a deploy is a new URL, so the new files load right away. The UI's modules (static/app/) are
    loaded by main.js, not the page, so they carry no hash: ModuleFiles serves them with no-cache instead."""
    def tag(m: re.Match) -> str:
        f = STATIC_DIR / m.group(2)
        if not f.is_file():
            return m.group(0)
        return f'{m.group(1)}/static/{m.group(2)}?v={hashlib.sha256(f.read_bytes()).hexdigest()[:10]}"'
    return re.sub(r'((?:src|href)=")/static/([\w./-]+)"', tag, (STATIC_DIR / "index.html").read_text())


class ModuleFiles(StaticFiles):
    """/static, with the UI's ES modules (static/app/*.js) revalidated on every load (no-cache: a 304 when
    unchanged), so a deploy never mixes old and new modules."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        if Path(full_path).parent == STATIC_DIR / "app":
            response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(db_path: str | Path | None = None, settings: Settings | None = None, verifier=None) -> FastAPI:
    """`settings` default to the environment (TASTR_HOSTED etc.); `verifier` maps an Access token to an email
    (tests pass a stub; hosted mode otherwise checks tokens against the Access team's keys)."""
    settings = settings or Settings.from_env()
    ctx = AppContext(db_path, settings, verifier)
    hosted = ctx.hosted

    @asynccontextmanager
    async def lifespan(_app):
        stop = threading.Event()
        ctx.stem_jobs.resume()  # stem separations the last run left unfinished (e.g. the machine auto-stopped)
        cut = ctx.analyses.resume()  # analyses it left unfinished read as failed
        if hosted:
            background.log_start(ctx, cut)
        ctx.drum_runs.resume()  # and drum model runs
        if hosted and settings.audio_days > 0:
            threading.Thread(target=background.expire_loop, args=(ctx, stop), name="tastr-expiry", daemon=True).start()
        if hosted and settings.watch_minutes > 0 and ((settings.github_repo and settings.github_workflows)
                                                       or os.environ.get("LITESTREAM_BUCKET")):
            threading.Thread(target=background.watch_loop, args=(ctx, stop), name="tastr-watch", daemon=True).start()
        yield
        stop.set()

    app = FastAPI(title="Tastr", docs_url=None if hosted else "/docs", redoc_url=None if hosted else "/redoc",
                  openapi_url=None if hosted else "/openapi.json", lifespan=lifespan)
    app.state.watch_once = partial(background.watch_once, ctx)  # for tests

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
                        user = await run_in_threadpool(ctx.verifier, token)
                    except Exception as e:  # expired, wrong audience, bad signature, keys unreachable
                        log.info("rejected an Access token: %s", type(e).__name__)
                if not user:
                    await run_in_threadpool(ctx.count_blocked)
                    return JSONResponse({"detail": "Sign in through the Tastr link you were invited to."},
                                        status_code=401, headers=ctx.extra_headers)
                request.state.user = user
                now = datetime.now(timezone.utc)
                before = ctx.last_seen.get(user)
                ctx.last_seen[user] = now.isoformat(timespec="seconds")
                if before is None or (now - datetime.fromisoformat(before)).total_seconds() > VISIT_IDLE:
                    await run_in_threadpool(ctx.count_visit, user)
                if ctx.snapshot_day != today():
                    ctx.snapshot_day = today()
                    await run_in_threadpool(ctx.snapshot_storage)
                m = TRACK_PATH.match(request.url.path)
                if m and not await run_in_threadpool(ctx.owns, user, int(m[1])):
                    # 404, not 403: someone else's track looks the same as one that doesn't exist.
                    return JSONResponse({"detail": "no such track"}, status_code=404, headers=ctx.extra_headers)
            response = await call_next(request)
            response.headers.update(ctx.extra_headers)
            return response

    @app.get("/api/config")
    def config(request: Request):
        """What this server allows, so the UI can hide what's turned off. On a hosted server the daily limits are
        the caller's own (an admin may have changed theirs), and admins also get the Cloudflare dashboard links."""
        user = getattr(request.state, "user", None)
        admin = hosted and user in settings.admins
        out = {"hosted": hosted, "links": not hosted, "query": True, "query_scope": "all" if not hosted or user in settings.admins else "mine", "user": user,
               "admin": admin, "limits": settings.limits() if hosted else None, "browser_engine": browser_engine_cfg(),
               "bulk_at_once": settings.bulk_lanes(),
               # Hosted, searches run in the browser on a copy of the library, with sql.js from here (TAS-49).
               "query_browser": {"base": "/sqljs/", "version": search.SQLJS_VERSION} if hosted else None,
               # Hosted with TASTR_DIRECT_UPLOAD: the page uploads straight to the audio bucket (TAS-66).
               "direct_upload": hosted and settings.direct_upload and blobs.enabled()}
        if hosted:
            with closing(ctx.conn()) as c:  # this person's own caps, with any change an admin made for them
                out["limits"] |= {"daily_tracks": ctx.cap(c, user, "tracks"), "daily_stems": ctx.cap(c, user, "stems"),
                                  "daily_asks": ctx.cap(c, user, "asks")}
        if admin:
            out["cf_links"] = settings.cf_links()  # {} without TASTR_CF_ACCOUNT_ID
        return out

    def browser_engine_cfg() -> dict | None:
        """Where the page gets the engine's WebAssembly build and which build it is, when browser cards are on."""
        wid = rust.wasm_id() if ctx.browser_engine else None
        return {"wasm": f"/engine/{rust.WASM_NAME}?v={wid}", "engine": wid} if wid else None

    @app.get(f"/engine/{rust.WASM_NAME}")
    def engine_wasm():
        """The Rust engine's WebAssembly build (TASTR_BROWSER_ENGINE). Its URL carries its hash, so it caches."""
        path = rust.wasm_path() if ctx.browser_engine else None
        if path is None:
            raise HTTPException(404, "Not Found")
        return FileResponse(path, media_type="application/wasm",
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/api/health")
    def health():
        return {"ok": True}

    for area in (admin, tracks, media, files, edits, analyze, search, lists, strudel):
        app.include_router(area.router(ctx))

    page = versioned_index()

    @app.get("/", include_in_schema=False)
    def index():
        # The page itself is never cached; its scripts and styles carry a content hash, so a deploy is seen at once.
        return HTMLResponse(page, headers={"Cache-Control": "no-cache"})

    app.mount("/static", ModuleFiles(directory=STATIC_DIR), name="static")
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
        raise SystemExit(f"Error: {e}") from e
    if settings.hosted:
        print(f"Hosted mode: logins checked against {settings.access_team_domain}; link analysis and SQL are off.")
    n = db.refresh_file_meta(db.connect(db_path), only_if_stale=True)  # once per tag reader change
    if n:
        print(f"Refreshed file tags for {n} track(s)")
    uvicorn.run(create_app(db_path, settings), host=host, port=port, log_level=log_level)
