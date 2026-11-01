"""Your edits to a track (drums, bass, chords) and their named versions, your cue points, labels, pattern reviews and
the review list."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ... import db
from ..context import AppContext


MAX_CUES, MAX_CUE_NAME, CUE_COLORS = 9, 30, 9  # cues per person per track, a name's length, the page's colors


class Cue(BaseModel):
    t: float = Field(ge=0, allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=MAX_CUE_NAME)
    color: int = Field(ge=0, lt=CUE_COLORS)


class VersionBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    edits: dict[str, Any]


def router(ctx: AppContext) -> APIRouter:
    api = APIRouter()

    # Edits to the drums, bass and chords: the current state autosaves; versions are named copies.
    @api.get("/api/tracks/{track_id}/edits")
    def track_edits(track_id: int):
        with closing(ctx.conn()) as c:
            ctx.track_or_404(c, track_id)
            return db.get_edits(c, track_id)

    @api.put("/api/tracks/{track_id}/edits", status_code=204)
    def save_track_edits(track_id: int, edits: dict[str, Any] = Body(...)):
        with closing(ctx.conn()) as c:
            ctx.track_or_404(c, track_id)
            db.set_edits(c, track_id, edits)
        return Response(status_code=204)

    # Cue points (GitHub #3): spots on the track to jump back to, per person. PUT replaces the whole list.
    @api.get("/api/tracks/{track_id}/cues")
    def track_cues(track_id: int, request: Request):
        with closing(ctx.conn()) as c:
            ctx.track_or_404(c, track_id)
            return db.get_cues(c, ctx.owner_of(request), track_id)

    @api.put("/api/tracks/{track_id}/cues")
    def save_track_cues(track_id: int, request: Request, cues: list[Cue] = Body(...)):
        if len(cues) > MAX_CUES:
            raise HTTPException(422, f"A track holds at most {MAX_CUES} cues.")
        clean = [{"t": round(q.t, 2), "name": q.name.strip() or "Cue", "color": q.color} for q in cues]
        with closing(ctx.conn()) as c:
            ctx.track_or_404(c, track_id)
            return db.set_cues(c, ctx.owner_of(request), track_id, clean)

    # Your own labels (tastr/labels.py): BPM, key and section starts set by hand, kept apart from the analysis.
    @api.get("/api/tracks/{track_id}/labels")
    def track_labels(track_id: int):
        with closing(ctx.conn()) as c:
            ctx.track_or_404(c, track_id)
            return db.get_labels(c, track_id)

    @api.put("/api/tracks/{track_id}/labels")
    def save_track_labels(track_id: int, body: dict[str, Any] = Body(...)):
        from ... import labels

        try:
            clean = labels.clean(body)
        except labels.LabelError as e:
            raise HTTPException(400, str(e)) from None
        with closing(ctx.conn()) as c:
            ctx.track_or_404(c, track_id)
            return db.set_labels(c, track_id, clean)

    # Your verdicts on the generated drums, bass and chords, per section (tastr/labels.py, "pattern reviews").
    @api.get("/api/tracks/{track_id}/pattern-reviews")
    def track_pattern_reviews(track_id: int):
        from ... import labels

        with closing(ctx.conn()) as c:
            ctx.track_or_404(c, track_id)
            return labels.pattern_reviews(c, track_id)

    @api.put("/api/tracks/{track_id}/pattern-reviews")
    def save_pattern_review(track_id: int, body: dict[str, Any] = Body(...)):
        from ... import labels

        with closing(ctx.conn()) as c:
            ctx.track_or_404(c, track_id)
            try:
                return labels.save_pattern_review(c, track_id, body)
            except labels.LabelError as e:
                raise HTTPException(400, str(e)) from None

    # Every pattern review, newest first: the Review tab's log shows them beside your labels.
    @api.get("/api/pattern-reviews")
    def all_pattern_reviews(request: Request):
        from ... import labels

        with closing(ctx.conn()) as c:
            rows = labels.all_pattern_reviews(c, ctx.owner_of(request))
        for r in rows:
            r.pop("path", None)
        return rows[::-1]

    @api.get("/api/review")
    def review(request: Request, limit: int = 20):
        from ... import labels

        with closing(ctx.conn()) as c:
            return labels.review_queue(c, max(1, min(limit, 1000)), ctx.owner_of(request))

    # Put a track in the review list yourself ("flagged"), take it out ("skipped"), or leave it to the queue (null).
    @api.put("/api/tracks/{track_id}/review")
    def set_review(track_id: int, body: dict[str, Any] = Body(...)):
        state = body.get("state")
        if state not in (None, *db.REVIEW_STATES):
            raise HTTPException(400, "state must be \"flagged\", \"skipped\" or null")
        with closing(ctx.conn()) as c:
            ctx.track_or_404(c, track_id)
            db.set_review_flag(c, track_id, state)
        return {"state": state}

    # Your labels, newest first, with Tastr's reading when each was saved: the Review tab's log and its CSV.
    @api.get("/api/labels")
    def all_labels(request: Request):
        with closing(ctx.conn()) as c:
            rows = db.list_labels(c, ctx.owner_of(request))
        for r in rows:
            r.pop("path", None)  # a server path; the UI doesn't need it
        return rows[::-1]

    @api.get("/api/tracks/{track_id}/edit-versions")
    def edit_versions(track_id: int):
        with closing(ctx.conn()) as c:
            ctx.track_or_404(c, track_id)
            return db.list_edit_versions(c, track_id)

    @api.post("/api/tracks/{track_id}/edit-versions", status_code=201)
    def add_edit_version(track_id: int, body: VersionBody):
        with closing(ctx.conn()) as c:
            ctx.track_or_404(c, track_id)
            return db.add_edit_version(c, track_id, body.name.strip() or "Version", body.edits)

    @api.get("/api/tracks/{track_id}/edit-versions/{version_id}")
    def edit_version(track_id: int, version_id: int):
        with closing(ctx.conn()) as c:
            v = db.get_edit_version(c, track_id, version_id)
        if v is None:
            raise HTTPException(404, "no such version")
        return v

    @api.delete("/api/tracks/{track_id}/edit-versions/{version_id}", status_code=204)
    def delete_edit_version(track_id: int, version_id: int):
        with closing(ctx.conn()) as c:
            if not db.delete_edit_version(c, track_id, version_id):
                raise HTTPException(404, "no such version")
        return Response(status_code=204)

    return api
