"""Explore: SQL queries and saved searches."""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ... import db
from ..context import AppContext

MAX_QUERY_ROWS = 5000
QUERY_SECONDS = 5.0  # on your own machine a query that runs longer is stopped; hosted, searches run in the browser
SQLJS_VERSION = "1.10.3"  # keep in step with the Dockerfile
SQLJS_FILES = {"sql-wasm.js": "text/javascript", "sql-wasm.wasm": "application/wasm"}
MAX_SEARCHES, MAX_SEARCH_BYTES = 100, 20_000  # saved searches per person, and the size of one


class QueryBody(BaseModel):
    sql: str
    mine: bool = False  # ignored on your own machine (every track is yours); kept so older pages still work


class SearchBody(BaseModel):
    mode: Literal["simple", "sql"]
    name: str = Field(min_length=1, max_length=40)
    # simple: {groups, join, sort}; sql: {sql}. Stored as given; the page builds the SQL itself.
    data: dict[str, Any]


def router(ctx: AppContext) -> APIRouter:
    api = APIRouter()

    @api.post("/api/query")
    def run_query(body: QueryBody):
        """One read-only SELECT on your own machine. A hosted server runs searches in the browser instead, on a copy
        of the person's library (/api/query/library), so it has no SQL endpoint."""
        if ctx.hosted:
            raise HTTPException(404, "Searches run in your browser now. Reload the page.")
        try:
            columns, rows = db.run_query(ctx.db_path or db.default_db_path(), body.sql, MAX_QUERY_ROWS, QUERY_SECONDS)
        except sqlite3.Error as e:
            msg = "The query took too long and was stopped." if str(e) == "interrupted" else str(e)
            raise HTTPException(400, msg) from e
        return {"columns": columns, "rows": rows}

    @api.get("/sqljs/{name}", include_in_schema=False)
    def sqljs(name: str):
        """sql.js (SQLite as WebAssembly, MIT) for the browser's searches on a hosted server. The image puts it in
        TASTR_SQLJS_DIR (Dockerfile); the URL carries its version, so it caches."""
        folder = os.environ.get("TASTR_SQLJS_DIR")
        if name not in SQLJS_FILES or not folder or not (Path(folder) / name).is_file():
            raise HTTPException(404, "Not Found")
        return FileResponse(Path(folder) / name, media_type=SQLJS_FILES[name],
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @api.get("/api/query/library")
    def query_library(request: Request, mine: bool = False):
        """Your library as a SQLite file, for the page to run SQL on in the browser with sql.js (TAS-49): on a
        hosted server your own tracks without the columns that name people or server paths, admins everyone's
        (`?mine=1`: only their own); on your own machine every track."""
        owner = request.state.user if ctx.hosted and (mine or request.state.user not in ctx.settings.admins) else None
        data = db.library_snapshot(ctx.db_path or db.default_db_path(), owner)
        headers = {"Cache-Control": "private, no-store", "X-Library-Bytes": str(len(data)),
                   "Access-Control-Expose-Headers": "X-Library-Bytes"}
        if "gzip" in request.headers.get("accept-encoding", ""):  # SQLite pages compress well (3 to 5 times)
            data, headers["Content-Encoding"] = gzip.compress(data, compresslevel=6, mtime=0), "gzip"
        return Response(data, media_type="application/vnd.sqlite3", headers=headers)

    # Saved searches (Explore > Find tracks), each person's own: the signed-in email when hosted, else everyone's.
    @api.get("/api/searches")
    def searches(request: Request):
        with closing(ctx.conn()) as c:
            return db.list_searches(c, ctx.owner_of(request))

    @api.put("/api/searches")
    def save_search(body: SearchBody, request: Request):
        if len(json.dumps(body.data)) > MAX_SEARCH_BYTES:
            raise HTTPException(413, "That search is too big to save.")
        name, owner = body.name.strip(), ctx.owner_of(request)
        if not name:
            raise HTTPException(422, "Give the search a name.")
        with closing(ctx.conn()) as c:
            have = db.list_searches(c, owner)
            replaces = any(s["mode"] == body.mode and s["name"].lower() == name.lower() for s in have)
            if len(have) >= MAX_SEARCHES and not replaces:
                raise HTTPException(409, f"You have {MAX_SEARCHES} saved searches. Delete one first.")
            return db.save_search(c, owner, body.mode, name, body.data)

    @api.delete("/api/searches/{search_id}", status_code=204)
    def delete_search(search_id: int, request: Request):
        with closing(ctx.conn()) as c:
            if not db.delete_search(c, ctx.owner_of(request), search_id):
                raise HTTPException(404, "no such search")
        return Response(status_code=204)

    return api
