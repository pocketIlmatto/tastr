"""Web UI + JSON API. See docs/modules/ui.md."""

from __future__ import annotations

import mimetypes
import os
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import db, service
from ..filemeta import describe, legacy_upload_prefix
from ..ingest import IngestError

STATIC_DIR = Path(__file__).parent / "static"
# Types the stdlib doesn't know everywhere; browsers need them to decode the stream.
AUDIO_TYPES = {".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".opus": "audio/ogg",
               ".ogg": "audio/ogg", ".aif": "audio/aiff", ".aiff": "audio/aiff", ".wav": "audio/wav", ".mp3": "audio/mpeg"}
MAX_QUERY_ROWS = 5000


class UrlBody(BaseModel):
    url: str


class QueryBody(BaseModel):
    sql: str


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


def create_app(db_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="Tastr")

    def conn() -> sqlite3.Connection:
        return db.connect(db_path)

    @app.get("/api/health")
    def health():
        return {"ok": True}

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
        return body

    def _audio_path(card) -> Path | None:
        p = card.source.local_path
        return Path(p) if p and Path(p).is_file() else None

    @app.get("/api/tracks/{track_id}")
    def track(track_id: int):
        with closing(conn()) as c:
            return card_body(c, track_id)

    @app.get("/api/tracks/{track_id}/audio")
    def track_audio(track_id: int):
        """The original audio file, with Range support so the browser can seek."""
        with closing(conn()) as c:
            card = db.load_card(c, track_id)
        if card is None:
            raise HTTPException(404, "no such track")
        path = _audio_path(card)
        if path is None:
            raise HTTPException(404, "The original audio file is not on disk.")
        media = AUDIO_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media)

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

    @app.delete("/api/tracks/{track_id}", status_code=204)
    def delete_track(track_id: int):
        with closing(conn()) as c:
            if not db.delete_track(c, track_id):
                raise HTTPException(404, "no such track")
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


def run(host: str = "127.0.0.1", port: int = 8000, db_path: str | None = None) -> None:
    import uvicorn

    from .. import filemeta

    if filemeta.mutagen is None:
        print("Warning: mutagen is not installed, so file tags and audio info can't be read. Run: pip install -e .")
    n = db.refresh_file_meta(db.connect(db_path), only_if_stale=True)  # once per tag reader change
    if n:
        print(f"Refreshed file tags for {n} track(s)")
    uvicorn.run(create_app(db_path), host=host, port=port)
