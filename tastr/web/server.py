"""Web UI + JSON API. STUB: only the read endpoints exist. See docs/modules/ui.md."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException

from .. import db


def create_app(db_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="Tastr")

    def conn():
        return db.connect(db_path)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/tracks")
    def tracks():
        return db.list_tracks(conn())

    @app.get("/api/tracks/{track_id}")
    def track(track_id: int):
        card = db.load_card(conn(), track_id)
        if card is None:
            raise HTTPException(404, "no such track")
        return {"id": track_id, **card.to_dict()}

    return app


def run(host: str = "127.0.0.1", port: int = 8000, db_path: str | None = None) -> None:
    import uvicorn

    uvicorn.run(create_app(db_path), host=host, port=port)
