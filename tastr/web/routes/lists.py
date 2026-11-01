"""Explore > Blend: saved lists of tracks, and the vibe scores Blend measures tracks by."""

from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ... import db
from ..context import AppContext

MAX_LISTS, MAX_LIST_TRACKS = 200, 5000  # lists per person, and tracks in one


class NewList(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    track_ids: list[int] = Field(default_factory=list, max_length=MAX_LIST_TRACKS)


class ListEdit(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    track_ids: list[int] | None = Field(default=None, max_length=MAX_LIST_TRACKS)


class AddTracks(BaseModel):
    track_ids: list[int] = Field(max_length=MAX_LIST_TRACKS)


def router(ctx: AppContext) -> APIRouter:
    api = APIRouter()

    def mine(c, owner: str | None, ids: list[int]) -> list[int]:
        """The ids that are tracks in this person's library (all tracks on your own machine), in order."""
        if not ids:
            return []
        where, args = ("WHERE owner = ?", (owner,)) if owner else ("", ())
        have = {r[0] for r in c.execute(f"SELECT id FROM tracks {where}", args)}
        return [i for i in dict.fromkeys(ids) if i in have]

    def owned(c, owner: str | None, list_id: int) -> dict:
        got = db.get_list(c, owner, list_id)
        if got is None:
            raise HTTPException(404, "no such list")
        return got

    @api.get("/api/lists")
    def lists(request: Request):
        with closing(ctx.conn()) as c:
            return db.list_lists(c, ctx.owner_of(request))

    @api.post("/api/lists")
    def new_list(body: NewList, request: Request):
        # Saving under a name you already have (any case) adds the tracks to that list instead.
        name, owner = body.name.strip(), ctx.owner_of(request)
        if not name:
            raise HTTPException(422, "Give the list a name.")
        with closing(ctx.conn()) as c:
            ids = mine(c, owner, body.track_ids)
            lid = db.find_list(c, owner, name)
            created = lid is None
            if created:
                if len(db.list_lists(c, owner)) >= MAX_LISTS:
                    raise HTTPException(409, f"You have {MAX_LISTS} lists. Delete one first.")
                lid = db.create_list(c, owner, name)
            added = db.add_to_list(c, lid, ids)
            return {**owned(c, owner, lid), "created": created, "added": added}

    @api.post("/api/lists/{list_id}/tracks")
    def add_tracks(list_id: int, body: AddTracks, request: Request):
        owner = ctx.owner_of(request)
        with closing(ctx.conn()) as c:
            owned(c, owner, list_id)
            added = db.add_to_list(c, list_id, mine(c, owner, body.track_ids))
            return {**owned(c, owner, list_id), "added": added}

    @api.put("/api/lists/{list_id}")
    def edit_list(list_id: int, body: ListEdit, request: Request):
        owner = ctx.owner_of(request)
        name = body.name.strip() if body.name is not None else None
        if name == "":
            raise HTTPException(422, "Give the list a name.")
        with closing(ctx.conn()) as c:
            owned(c, owner, list_id)
            if name is not None and db.find_list(c, owner, name) not in (None, list_id):
                raise HTTPException(409, f"You already have a list called “{name}”.")
            db.set_list(c, list_id, name, None if body.track_ids is None else mine(c, owner, body.track_ids))
            return owned(c, owner, list_id)

    @api.delete("/api/lists/{list_id}", status_code=204)
    def delete_list(list_id: int, request: Request):
        # The tracks stay in the library.
        with closing(ctx.conn()) as c:
            if not db.delete_list(c, ctx.owner_of(request), list_id):
                raise HTTPException(404, "no such list")
        return Response(status_code=204)

    @api.get("/api/vibe-scores")
    def vibe_scores(request: Request):
        # {track id: {vibe: score}}, every vibe the engine scored, below the card's cut-off too.
        with closing(ctx.conn()) as c:
            return db.vibe_scores(c, ctx.owner_of(request))

    return api
