"""Explore > Blend: saved lists of tracks, and the vibe scores Blend measures tracks by."""

from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ... import db
from .. import playlists
from ..context import AppContext
from ..downloads import ZIP_MAX_TRACKS, content_disposition, unique
from ..hosted import LINK_DAYS, PUBLIC_PREFIX
from .files import track_rows, zip_response, zip_too_big

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

    def playlist_tracks(request: Request, list_id: int) -> tuple[dict, list[dict]]:
        """The list (yours, or 404) and its tracks that have audio on the server, in list order."""
        owner = ctx.owner_of(request)
        with closing(ctx.conn()) as c:
            lst = owned(c, owner, list_id)
        ids = lst["track_ids"]
        if not ids:
            return lst, []
        by_id = {r["id"]: r for r in track_rows(ctx, owner, ids)}
        return lst, [by_id[i] for i in ids if i in by_id and by_id[i]["audio_path"]]

    @api.get("/api/lists/{list_id}/playlist.zip")
    def playlist_zip(list_id: int, request: Request):
        """The list's audio in audio/, numbered in list order, plus an .m3u8 that points at those files, so it plays
        anywhere offline. Tracks without audio on the server are left out of both."""
        with closing(ctx.conn()) as c:
            lst = owned(c, ctx.owner_of(request), list_id)
        if len(lst["track_ids"]) > ZIP_MAX_TRACKS:
            raise HTTPException(413, f"A zip can hold up to {ZIP_MAX_TRACKS} tracks; this list has {len(lst['track_ids'])}. "
                                     "Download the tracks in smaller groups.")
        lst, rows = playlist_tracks(request, list_id)
        if not rows:
            raise HTTPException(404, "None of this list's tracks has its original audio on the server.")
        entries, items, taken = [], [], set()
        for pos, r in enumerate(rows, 1):
            arc = unique(playlists.zip_audio_name(pos, len(rows), r["base"], r["audio_path"].suffix.lower()), taken)
            entries.append((arc, r["audio_path"]))
            items.append({"location": arc, "title": r["title"], "artist": r["artist"], "duration": r["duration"]})
        zip_too_big(entries)
        name = playlists.file_name(lst["name"])
        text = playlists.m3u8(lst["name"], items).encode()
        return zip_response([*entries, (f"{name}.m3u8", text)], f"{name}.zip")

    @api.get("/api/lists/{list_id}/playlist.{fmt}")
    def playlist_file(list_id: int, fmt: str, request: Request):
        """The list as an M3U8, XSPF or PLS file. On your own machine each entry is this server's address for the
        track's audio, so Tastr has to keep running. On a shared server those addresses sit behind the login, so
        each entry is a signed link instead, which works for LINK_DAYS days (like the Strudel links)."""
        if fmt not in playlists.FORMATS:
            raise HTTPException(404, "Not a playlist format")
        lst, rows = playlist_tracks(request, list_id)
        base = str(request.base_url).rstrip("/")
        if ctx.hosted and base.startswith("http://"):  # the TLS ends at the proxy in front of the server
            base = "https://" + base.removeprefix("http://")

        def where(tid: int) -> str:
            if ctx.signer is not None:
                return f"{base}{PUBLIC_PREFIX}{ctx.signer.token(tid)}/tracks/{tid}/audio"
            return f"{base}/api/tracks/{tid}/audio"
        items = [{"location": where(r["id"]), "title": r["title"], "artist": r["artist"], "duration": r["duration"]}
                 for r in rows]
        text = playlists.BUILD[fmt](lst["name"], items)
        return Response(text, media_type=f"{playlists.FORMATS[fmt]}; charset=utf-8",
                        headers={"Content-Disposition": content_disposition(f"{playlists.file_name(lst['name'])}.{fmt}"),
                                 "Cache-Control": "no-store", **({"X-Link-Days": str(LINK_DAYS)} if ctx.signer else {})})

    @api.get("/api/vibe-scores")
    def vibe_scores(request: Request):
        # {track id: {vibe: score}}, every vibe the engine scored, below the card's cut-off too.
        with closing(ctx.conn()) as c:
            return db.vibe_scores(c, ctx.owner_of(request))

    return api
