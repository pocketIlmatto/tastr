"""Web UI + JSON API. See docs/modules/ui.md."""

from __future__ import annotations

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

    @app.get("/api/tracks/{track_id}")
    def track(track_id: int):
        with closing(conn()) as c:
            card = db.load_card(c, track_id)
            row = c.execute("SELECT analyzed_at FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if card is None:
            raise HTTPException(404, "no such track")
        card.source.title = _unprefixed(card.source.title, card.source.uri)
        return {"id": track_id, **card.to_dict(), "metadata": describe(card, row[0] if row else None)}

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

    uvicorn.run(create_app(db_path), host=host, port=port)
