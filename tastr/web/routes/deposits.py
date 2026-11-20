"""The bank's API: deposits (tastr/deposits.py). It sits beside the library's routes and changes none of them."""

from __future__ import annotations

from contextlib import closing
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ... import blobs, deposits, moments, stems
from ..context import AppContext
from .media import _serve

MAX_DEPOSITS = 20000  # moments and collections per person (tracks are counted by the library's own limits)
MAX_JSON_KEYS = 50


class Tag(BaseModel):
    tag: str = Field(min_length=1, max_length=deposits.MAX_TAG)
    origin: str = Field(default="user", min_length=1, max_length=deposits.MAX_TAG)


class NewDeposit(BaseModel):
    size: Literal["moment", "collection"]
    title: str | None = Field(default=None, max_length=deposits.MAX_TITLE)
    note: str | None = Field(default=None, max_length=deposits.MAX_NOTE)
    source_uri: str | None = Field(default=None, max_length=2000)
    start_s: float | None = Field(default=None, ge=0)
    end_s: float | None = Field(default=None, ge=0)
    tags: list[Tag | str] = Field(default_factory=list, max_length=deposits.MAX_TAGS)
    context: dict[str, Any] = Field(default_factory=dict, max_length=MAX_JSON_KEYS)
    held_by: int | None = None
    position: int | None = Field(default=None, ge=0)
    touches: list[int] = Field(default_factory=list, max_length=50)


class NewMoment(BaseModel):
    """A moment from a link: the bank fetches the whole source, cuts the moment out and breaks it down."""
    source_uri: str = Field(min_length=1, max_length=2000)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    title: str | None = Field(default=None, max_length=deposits.MAX_TITLE)
    note: str | None = Field(default=None, max_length=deposits.MAX_NOTE)
    tags: list[Tag | str] = Field(default_factory=list, max_length=deposits.MAX_TAGS)


class DepositEdit(BaseModel):
    title: str | None = Field(default=None, max_length=deposits.MAX_TITLE)
    note: str | None = Field(default=None, max_length=deposits.MAX_NOTE)
    tags: list[Tag | str] | None = Field(default=None, max_length=deposits.MAX_TAGS)
    context: dict[str, Any] | None = Field(default=None, max_length=MAX_JSON_KEYS)


class Hold(BaseModel):
    deposit_ids: list[int] = Field(max_length=5000)


def _tags(tags) -> list:
    return [t.model_dump() if isinstance(t, Tag) else t for t in tags]


def router(ctx: AppContext) -> APIRouter:
    api = APIRouter()

    @api.get("/api/deposits")
    def find(request: Request, size: Literal["moment", "track", "collection"] | None = None, tag: str | None = None,
             q: str | None = None, status: str | None = None, limit: int = 100, offset: int = 0):
        """Deposits, newest first. Tracks added to the library since the last call join the bank here."""
        owner = ctx.owner_of(request)
        with closing(ctx.conn()) as c:
            deposits.sync_tracks(c, owner)
            return deposits.find(c, owner, size=size, tag=tag, q=q, status=status, limit=limit, offset=offset)

    @api.post("/api/deposits", status_code=201)
    def add(body: NewDeposit, request: Request):
        owner = ctx.owner_of(request)
        source = (body.source_uri or "").strip() or None
        if source and not source.startswith(("http://", "https://")):
            raise HTTPException(422, "The source has to be a link.")
        with closing(ctx.conn()) as c:
            n = c.execute("SELECT COUNT(*) FROM deposits WHERE owner IS ? AND size != 'track'", (owner,)).fetchone()[0]
            if n >= MAX_DEPOSITS:
                raise HTTPException(409, f"You have {MAX_DEPOSITS} deposits. Delete some first.")
            try:
                return deposits.add(
                    c, owner, body.size, title=body.title, note=body.note, source_kind="link" if source else None,
                    source_uri=source, start_s=body.start_s, end_s=body.end_s, tags=_tags(body.tags),
                    context=body.context, status="needs-source" if source else "ready", held_by=body.held_by,
                    position=body.position, touches=body.touches)
            except deposits.DepositError as e:
                raise HTTPException(422, str(e)) from None

    def _no_links_here() -> None:
        if ctx.hosted:  # as for /api/analyze-url: the hosted server doesn't fetch from links (docs/features.md)
            raise HTTPException(403, "Saving a moment from a link works on your own install for now.")

    @api.post("/api/deposits/moments", status_code=202)
    def add_moment(body: NewMoment, request: Request):
        """Save a moment from a link and start finishing it in the background; follow its `status` (and
        `extracted.stage`) with GET /api/deposits/{id}."""
        _no_links_here()
        owner = ctx.owner_of(request)
        source = body.source_uri.strip()
        with closing(ctx.conn()) as c:
            n = c.execute("SELECT COUNT(*) FROM deposits WHERE owner IS ? AND size != 'track'", (owner,)).fetchone()[0]
            if n >= MAX_DEPOSITS:
                raise HTTPException(409, f"You have {MAX_DEPOSITS} deposits. Delete some first.")
            try:
                got = moments.add(c, owner, source, body.start_s, body.end_s, note=body.note, title=body.title,
                                  tags=_tags(body.tags))
            except deposits.DepositError as e:
                raise HTTPException(422, str(e)) from None
        ctx.moment_runs.start(got["id"])
        return {**got, "job": ctx.moment_runs.status(got["id"])}

    @api.post("/api/deposits/{deposit_id}/fetch", status_code=202)
    def fetch(deposit_id: int, request: Request):
        """Finish a moment from a link again: one saved as needs-source, or one that failed. What is done is kept."""
        _no_links_here()
        with closing(ctx.conn()) as c:
            got = deposits.get(c, ctx.owner_of(request), deposit_id)
        if got is None:
            raise HTTPException(404, "no such deposit")
        if got["size"] != "moment" or got["source_kind"] != "link" or got["start_s"] is None:
            raise HTTPException(422, "Only a moment with a link, a start and an end can be fetched.")
        ctx.moment_runs.start(deposit_id)
        return {**got, "job": ctx.moment_runs.status(deposit_id)}

    def _mine_or_404(request: Request, deposit_id: int) -> dict[str, Any]:
        with closing(ctx.conn()) as c:
            got = deposits.get(c, ctx.owner_of(request), deposit_id)
        if got is None:
            raise HTTPException(404, "no such deposit")
        return got

    @api.get("/api/deposits/{deposit_id}/audio")
    def audio(deposit_id: int, request: Request, origin: str | None = Header(None)):
        """A moment's slice, or the whole audio of a source the bank fetched, with Range support."""
        got = _mine_or_404(request, deposit_id)
        path = moments.slice_path(deposit_id) if got["size"] == "moment" else moments.source_path(got)
        if path is None or not blobs.exists(path):
            raise HTTPException(404, "The bank has no audio for this deposit.")
        return _serve(path, origin)

    @api.get("/api/deposits/{deposit_id}/stems/{name}")
    def stem(deposit_id: int, name: str, request: Request, origin: str | None = Header(None)):
        """One stem of a moment's slice as FLAC."""
        if name not in stems.STEM_NAMES:
            raise HTTPException(404, f"Stems are {', '.join(stems.STEM_NAMES)}.")
        _mine_or_404(request, deposit_id)
        path = stems.stem_path(moments.stems_folder(deposit_id), name)
        if not blobs.exists(path):
            raise HTTPException(404, "This moment has not been separated into stems.")
        return _serve(path, origin, "audio/flac")

    @api.get("/api/deposits/{deposit_id}/card")
    def card(deposit_id: int, request: Request):
        """The engine's whole card for a moment's slice (its times are in the slice)."""
        _mine_or_404(request, deposit_id)
        path = moments.card_path(deposit_id)
        if not blobs.exists(path):
            raise HTTPException(404, "This moment has not been analyzed yet.")
        return FileResponse(blobs.ensure(path) or path, media_type="application/json")

    @api.get("/api/deposits/{deposit_id}")
    def get(deposit_id: int, request: Request):
        with closing(ctx.conn()) as c:
            got = deposits.get(c, ctx.owner_of(request), deposit_id)
        if got is None:
            raise HTTPException(404, "no such deposit")
        return got

    @api.patch("/api/deposits/{deposit_id}")
    def edit(deposit_id: int, body: DepositEdit, request: Request):
        changes = body.model_dump(exclude_unset=True)
        if "tags" in changes:
            changes["tags"] = _tags(body.tags or [])
        with closing(ctx.conn()) as c:
            try:
                got = deposits.update(c, ctx.owner_of(request), deposit_id, changes)
            except deposits.DepositError as e:
                raise HTTPException(422, str(e)) from None
        if got is None:
            raise HTTPException(404, "no such deposit")
        return got

    @api.post("/api/deposits/{deposit_id}/holds")
    def hold(deposit_id: int, body: Hold, request: Request):
        owner = ctx.owner_of(request)
        with closing(ctx.conn()) as c:
            try:
                added = deposits.hold(c, owner, deposit_id, body.deposit_ids)
            except deposits.DepositError as e:
                raise HTTPException(404, str(e)) from None
            return {**deposits.get(c, owner, deposit_id), "added": added}

    @api.delete("/api/deposits/{deposit_id}", status_code=204)
    def delete(deposit_id: int, request: Request):
        with closing(ctx.conn()) as c:
            try:
                gone = deposits.delete(c, ctx.owner_of(request), deposit_id)
            except deposits.DepositError as e:
                raise HTTPException(409, str(e)) from None
        if not gone:
            raise HTTPException(404, "no such deposit")
        ctx.moment_runs.forget(deposit_id)  # a queued moment from a link is skipped
        return Response(status_code=204)

    return api
