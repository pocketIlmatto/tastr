"""The bank's API: deposits (tastr/deposits.py). It sits beside the library's routes and changes none of them."""

from __future__ import annotations

from contextlib import closing
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ... import deposits
from ..context import AppContext

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
        return Response(status_code=204)

    return api
