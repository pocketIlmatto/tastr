"""A track's audio, stems and drum model runs, and the signed links strudel.cc fetches them through."""

from __future__ import annotations

import mimetypes
from contextlib import closing
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse

from ... import db, drum_jobs, stems
from ..context import AppContext, analysis_source, runs_on
from ..hosted import LINK_DAYS, PUBLIC_PREFIX, today

# Types the stdlib doesn't know everywhere; browsers need them to decode the stream.
AUDIO_TYPES = {".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".opus": "audio/ogg",
               ".ogg": "audio/ogg", ".aif": "audio/aiff", ".aiff": "audio/aiff", ".wav": "audio/wav", ".mp3": "audio/mpeg"}
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


def router(ctx: AppContext) -> APIRouter:
    api = APIRouter()

    @api.get("/api/tracks/{track_id}/audio")
    def track_audio(track_id: int, origin: str | None = Header(None)):
        """The original audio file, with Range support so the browser can seek. strudel.cc may fetch it too."""
        with closing(ctx.conn()) as c:
            card = db.load_card(c, track_id)
        if card is None:
            raise HTTPException(404, "no such track")
        path = ctx.audio_path(card)
        if path is None:
            raise HTTPException(404, "The original audio file is not on disk.")
        media = AUDIO_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media, headers=_strudel_cors(origin))

    def _preflight(origin: str | None) -> Response:
        if origin not in STRUDEL_ORIGINS:
            return Response(status_code=403, headers=_strudel_cors(origin))
        return Response(status_code=204, headers=_strudel_cors(origin, preflight=True))

    @api.options("/api/tracks/{track_id}/audio", include_in_schema=False)
    def track_audio_preflight(track_id: int, origin: str | None = Header(None)):
        return _preflight(origin)

    @api.options("/api/tracks/{track_id}/stems/{name}", include_in_schema=False)
    def track_stem_preflight(track_id: int, name: str, origin: str | None = Header(None)):
        return _preflight(origin)

    @api.get("/api/tracks/{track_id}/strudel-link")
    def strudel_link(track_id: int):
        """Where strudel.cc can fetch this track's audio; its stems are next to it (`.../stems/<name>`). On a
        shared server strudel.cc can't pass the login, so this is a signed link that works for LINK_DAYS days."""
        ctx.card_or_404(track_id)
        if ctx.signer is None:
            return {"audio": f"/api/tracks/{track_id}/audio", "days": None}
        return {"audio": f"{PUBLIC_PREFIX}{ctx.signer.token(track_id)}/tracks/{track_id}/audio", "days": LINK_DAYS}

    def _signed(token: str, track_id: int) -> None:
        if ctx.signer is None or not ctx.signer.check(token, track_id):
            raise HTTPException(404, "This link has expired. Open the sketch again from Tastr.")

    @api.get(PUBLIC_PREFIX + "{token}/tracks/{track_id}/audio", include_in_schema=False)
    def signed_audio(token: str, track_id: int, origin: str | None = Header(None)):
        _signed(token, track_id)
        return track_audio(track_id, origin)

    @api.get(PUBLIC_PREFIX + "{token}/tracks/{track_id}/stems/{name}", include_in_schema=False)
    def signed_stem(token: str, track_id: int, name: str, origin: str | None = Header(None)):
        _signed(token, track_id)
        return track_stem(track_id, name, origin)

    @api.options(PUBLIC_PREFIX + "{token}/tracks/{track_id}/audio", include_in_schema=False)
    @api.options(PUBLIC_PREFIX + "{token}/tracks/{track_id}/stems/{name}", include_in_schema=False)
    def signed_preflight(token: str, track_id: int, origin: str | None = Header(None), name: str = ""):
        return _preflight(origin)

    def _stems_status(card, track_id: int) -> dict[str, Any]:
        return {**ctx.stem_jobs.status(ctx.stems_dir(card, track_id)), "available": stems.available()}

    @api.get("/api/tracks/{track_id}/stems")
    def track_stems(track_id: int):
        """Separation state: none, queued, running (with progress 0-1), ready (with the stem names) or error."""
        return _stems_status(ctx.card_or_404(track_id), track_id)

    @api.post("/api/tracks/{track_id}/stems", status_code=202)
    def separate_stems(track_id: int, request: Request):
        """Start separating one track into stems in the background (one track at a time; others queue)."""
        card = ctx.card_or_404(track_id)
        path = ctx.audio_path(card)
        if path is None:
            raise HTTPException(404, "The original audio file is not on disk, so there is nothing to separate.")
        if not stems.available():
            raise HTTPException(501, stems.INSTALL_HINT)
        folder = ctx.stems_dir(card, track_id)
        if not stems.is_ready(folder) and ctx.stem_jobs.status(folder)["state"] not in ("queued", "running"):
            if ctx.hosted:
                user = request.state.user
                with closing(ctx.conn()) as c:
                    most = ctx.cap(c, user, "stems")
                    if db.usage_today(c, user, "stems", today()) >= most:
                        raise ctx.refuse(user, 429, f"You've reached today's limit of {most} stem separations.",
                                     f"daily stem limit ({most})")
                    db.add_usage(c, user, "stems", today())
                ctx.event(user, "stems", "Started separating stems for 1 track", source=runs_on(stems.backend()))
            ctx.stem_jobs.start(path, folder)
        return _stems_status(card, track_id)

    @api.get("/api/tracks/{track_id}/stems/{name}")
    def track_stem(track_id: int, name: str, origin: str | None = Header(None)):
        """One stem as FLAC, like /audio: Range support, and strudel.cc may fetch it."""
        if name not in stems.STEM_NAMES:
            raise HTTPException(404, f"Stems are {', '.join(stems.STEM_NAMES)}.")
        path = stems.stem_path(ctx.stems_dir(ctx.card_or_404(track_id), track_id), name)
        if not path.is_file():
            raise HTTPException(404, "This track has not been separated into stems.")
        return FileResponse(path, media_type="audio/flac", headers=_strudel_cors(origin))

    def _drum_model_status(track_id: int) -> dict[str, Any]:
        return {**ctx.drum_runs.status(track_id), "available": drum_jobs.available()}

    @api.get("/api/tracks/{track_id}/drum-model")
    def track_drum_model(track_id: int):
        """The track's drum model run: none, queued, running, done or error (with `error`)."""
        ctx.card_or_404(track_id)
        return _drum_model_status(track_id)

    @api.post("/api/tracks/{track_id}/drum-model", status_code=202)
    def detect_drums(track_id: int, request: Request):
        """Re-analyze the track with the pretrained drum model in the background (one track at a time; others
        queue). Saved edits move to the new sections as with Re-analyze."""
        card = ctx.card_or_404(track_id)
        if ctx.audio_path(card) is None:
            raise HTTPException(410, "The original audio file is no longer on disk.")
        if not drum_jobs.available():
            raise HTTPException(501, drum_jobs.INSTALL_HINT)
        if not ctx.drum_runs.busy(track_id):
            user = ctx.owner_of(request)
            if ctx.hosted:
                with closing(ctx.conn()) as c:
                    if db.usage_today(c, user, "drums", today()) >= ctx.settings.daily_drums:
                        raise ctx.refuse(user, 429, f"You've reached today's limit of {ctx.settings.daily_drums} drum model runs.",
                                     f"daily drum model limit ({ctx.settings.daily_drums})")
                    db.add_usage(c, user, "drums", today())
            ctx.event(user, "drums", "Started detecting drums with the model for 1 track", source=analysis_source())
            ctx.drum_runs.start(track_id)
        return _drum_model_status(track_id)

    return api
