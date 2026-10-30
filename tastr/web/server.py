"""Web UI + JSON API. See docs/modules/ui.md."""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import sqlite3
import uuid
from contextlib import closing, nullcontext
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Body, FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import db, lookup, service, stems
from ..filemeta import describe, legacy_upload_prefix
from ..ingest import AUDIO_EXTENSIONS, IngestError, is_url
from .hosted import (HEALTH_PATH, TOKEN_HEADER, AccessVerifier, AnalysisSlots, Settings, inline_script_hashes,
                     security_headers, stored_bytes, today)

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger(__name__)
# Types the stdlib doesn't know everywhere; browsers need them to decode the stream.
AUDIO_TYPES = {".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".opus": "audio/ogg",
               ".ogg": "audio/ogg", ".aif": "audio/aiff", ".aiff": "audio/aiff", ".wav": "audio/wav", ".mp3": "audio/mpeg"}
MAX_QUERY_ROWS = 5000
# The Strudel sketch ("Try this in Strudel") loads the original audio and its stems from strudel.cc, so only
# those endpoints answer CORS, and only for that origin.
STRUDEL_ORIGINS = ("https://strudel.cc",)


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


def create_app(db_path: str | Path | None = None, settings: Settings | None = None, verifier=None) -> FastAPI:
    """`settings` default to the environment (TASTR_HOSTED etc.); `verifier` maps an Access token to an email
    (tests pass a stub; hosted mode otherwise checks tokens against the Access team's keys)."""
    settings = settings or Settings.from_env()
    hosted = settings.hosted
    if hosted and verifier is None:
        verifier = AccessVerifier(settings.access_team_domain, settings.access_aud)
    slots = AnalysisSlots(settings.max_analyses) if hosted else None
    extra_headers = security_headers(inline_script_hashes((STATIC_DIR / "index.html").read_text())) if hosted else {}
    app = FastAPI(title="Tastr", docs_url=None if hosted else "/docs", redoc_url=None if hosted else "/redoc",
                  openapi_url=None if hosted else "/openapi.json")
    jobs = stems.Jobs()

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
            if request.url.path != HEALTH_PATH:
                token = request.headers.get(TOKEN_HEADER) or request.cookies.get("CF_Authorization")
                user = None
                if token:
                    try:
                        user = await run_in_threadpool(verifier, token)
                    except Exception as e:  # expired, wrong audience, bad signature, keys unreachable
                        log.info("rejected an Access token: %s", type(e).__name__)
                if not user:
                    return JSONResponse({"detail": "Sign in through the Tastr link you were invited to."},
                                        status_code=401, headers=extra_headers)
                request.state.user = user
            response = await call_next(request)
            response.headers.update(extra_headers)
            return response

    def conn() -> sqlite3.Connection:
        return db.connect(db_path)

    def results(rs) -> dict[str, Any]:
        if hosted:  # error text can name server paths; keep it in the log
            for r in rs:
                if r.error:
                    log.warning("analysis failed for %s: %s", r.source.uri, r.error)
                    r.error = "Couldn't analyze this file."
        return _results(rs)

    def analysis_slot():
        return slots or nullcontext()

    @app.get("/api/config")
    def config(request: Request):
        """What this server allows, so the UI can hide what's turned off."""
        return {"hosted": hosted, "links": not hosted, "query": not hosted,
                "user": getattr(request.state, "user", None), "limits": settings.limits() if hosted else None}

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
    def tracks():
        with closing(conn()) as c:
            rows = db.list_tracks(c)
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
                    if db.usage_today(c, user, "stems", today()) >= settings.daily_stems:
                        raise HTTPException(429, f"You've reached today's limit of {settings.daily_stems} stem separations.")
                    db.add_usage(c, user, "stems", today())
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
    def reanalyze(track_id: int):
        """Run the full engine again on the track's stored audio file (same id, fresh card)."""
        with closing(conn()) as c:
            try:
                with analysis_slot():
                    result = service.reanalyze_track(c, track_id)
            except FileNotFoundError:
                raise HTTPException(410, "The original audio file is no longer on disk.")
            except TimeoutError:
                raise HTTPException(503, "The server is busy analyzing other tracks. Try again in a few minutes.")
            if result is None:
                raise HTTPException(404, "no such track")
            if result.error:
                if hosted:
                    log.warning("re-analysis of track %s failed: %s", track_id, result.error)
                    raise HTTPException(422, "Couldn't analyze the file.")
                raise HTTPException(422, f"Couldn't analyze the file: {result.error}")
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
        if card is not None:
            folder = _stems_dir(card, track_id)
            jobs.forget(folder)
            stems.remove(folder)
        return Response(status_code=204)

    # Plain `def` endpoints below run in FastAPI's threadpool, so slow analysis
    # doesn't block the event loop.
    @app.post("/api/analyze")
    def analyze(request: Request, files: list[UploadFile] = File(...)):
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
                for p in [dest, *map(Path, paths)]:  # nothing from a refused request stays on disk
                    p.unlink(missing_ok=True)
                    p.parent.rmdir()
                raise HTTPException(413, f"{name} is larger than {settings.max_upload_mb:g} MB.")
            paths.append(str(dest))
        with closing(conn()) as c:
            if hosted:
                db.add_usage(c, request.state.user, "tracks", today(), len(paths))
            try:
                with analysis_slot():
                    return results(service.analyze_inputs(c, paths))
            except TimeoutError:
                raise HTTPException(503, "The server is busy analyzing other tracks. Try again in a few minutes.")

    def check_upload(user: str, files: list[UploadFile]) -> None:
        if len(files) > settings.max_files:
            raise HTTPException(400, f"Upload at most {settings.max_files} files at a time.")
        for f in files:
            if Path(f.filename or "").suffix.lower() not in AUDIO_EXTENSIONS:
                raise HTTPException(415, f"{Path(f.filename or 'upload').name} isn't an audio file Tastr can read.")
        if stored_bytes(tastr_home()) >= settings.storage_gb * 1024 ** 3:
            raise HTTPException(507, "This server is out of space for new uploads. Ask whoever invited you.")
        with closing(conn()) as c:
            used = db.usage_today(c, user, "tracks", today())
        if used + len(files) > settings.daily_tracks:
            left = max(0, settings.daily_tracks - used)
            raise HTTPException(429, f"You can analyze {settings.daily_tracks} tracks a day; {left} left today.")

    @app.post("/api/analyze-url")
    def analyze_url(body: UrlBody):
        if hosted:
            raise HTTPException(403, "Link analysis is turned off on this server. Upload the audio file instead.")
        if not is_url(body.url):  # never a path: anyone who can reach the server could read files off its disk
            raise HTTPException(400, "Paste an http(s) link to a track or playlist.")
        with closing(conn()) as c:
            try:
                results = service.analyze_inputs(c, [body.url])
            except IngestError as e:
                raise HTTPException(400, str(e))
        return _results(results)

    @app.post("/api/query")
    def run_query(body: QueryBody):
        if hosted:
            raise HTTPException(403, "The SQL query box is turned off on this server.")
        path = db_path or db.default_db_path()
        try:
            with closing(db.connect(path, readonly=True)) as c:
                cur = c.execute(body.sql)
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = [list(r) for r in cur.fetchmany(MAX_QUERY_ROWS)]
        except sqlite3.Error as e:
            raise HTTPException(400, str(e))
        return {"columns": columns, "rows": rows}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

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
