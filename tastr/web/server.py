"""Web UI + JSON API. See docs/modules/ui.md."""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Body, FastAPI, File, Header, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import db, lookup, service, stems
from ..filemeta import describe, legacy_upload_prefix
from ..ingest import IngestError

STATIC_DIR = Path(__file__).parent / "static"
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


def upload_dir() -> Path:
    home = Path(os.environ.get("TASTR_HOME", Path.home() / ".tastr"))
    d = home / "uploads"
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


def create_app(db_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="Tastr")
    jobs = stems.Jobs()

    @app.middleware("http")
    async def same_origin_writes(request, call_next):
        # Any page open in the browser could otherwise POST to the local server (analyze a path, delete a
        # track, spend lookup quota). This is not authentication: scripts and curl still get through.
        if request.method not in ("GET", "HEAD", "OPTIONS") and _cross_site(request):
            return JSONResponse({"detail": "Cross-site requests are not allowed."}, status_code=403)
        return await call_next(request)

    def conn() -> sqlite3.Connection:
        return db.connect(db_path)

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
    def separate_stems(track_id: int):
        """Start separating one track into stems in the background (one track at a time; others queue)."""
        card = _card_or_404(track_id)
        path = _audio_path(card)
        if path is None:
            raise HTTPException(404, "The original audio file is not on disk, so there is nothing to separate.")
        if not stems.available():
            raise HTTPException(501, stems.INSTALL_HINT)
        folder = _stems_dir(card, track_id)
        if not stems.is_ready(folder):
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
                result = service.reanalyze_track(c, track_id)
            except FileNotFoundError:
                raise HTTPException(410, "The original audio file is no longer on disk.")
            if result is None:
                raise HTTPException(404, "no such track")
            if result.error:
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
    def analyze(files: list[UploadFile] = File(...)):
        paths: list[str] = []
        for f in files:
            name = re.sub(r"[^\w.\- ]", "_", Path(f.filename or "upload").name) or "upload"
            # The unique part is a folder, so the stored file keeps the name the user gave it (that is what the title falls back to).
            dest = upload_dir() / uuid.uuid4().hex[:8] / name
            dest.parent.mkdir()
            with open(dest, "wb") as out:
                while chunk := f.file.read(1 << 20):
                    out.write(chunk)
            paths.append(str(dest))
        with closing(conn()) as c:
            return _results(service.analyze_inputs(c, paths))

    @app.post("/api/analyze-url")
    def analyze_url(body: UrlBody):
        with closing(conn()) as c:
            try:
                results = service.analyze_inputs(c, [body.url])
            except IngestError as e:
                raise HTTPException(400, str(e))
        return _results(results)

    @app.post("/api/query")
    def run_query(body: QueryBody):
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
    n = db.refresh_file_meta(db.connect(db_path), only_if_stale=True)  # once per tag reader change
    if n:
        print(f"Refreshed file tags for {n} track(s)")
    uvicorn.run(create_app(db_path), host=host, port=port, log_level=log_level)
