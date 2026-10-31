"""Web UI + JSON API. See docs/modules/ui.md."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager, closing, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Body, FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import db, lookup, service, stems
from ..filemeta import describe, legacy_upload_prefix
from ..ingest import AUDIO_EXTENSIONS, IngestError, is_url
from .hosted import (HEALTH_PATH, LINK_DAYS, PUBLIC_PREFIX, TOKEN_HEADER, AccessVerifier, AnalysisSlots, LinkSigner,
                     Settings, expire_audio, file_bytes, inline_script_hashes, security_headers, stored_bytes, today)

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger(__name__)
# Types the stdlib doesn't know everywhere; browsers need them to decode the stream.
AUDIO_TYPES = {".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".opus": "audio/ogg",
               ".ogg": "audio/ogg", ".aif": "audio/aiff", ".aiff": "audio/aiff", ".wav": "audio/wav", ".mp3": "audio/mpeg"}
MAX_QUERY_ROWS = 5000
# The Strudel sketch ("Try this in Strudel") loads the original audio and its stems from strudel.cc, so only
# those endpoints answer CORS, and only for that origin.
STRUDEL_ORIGINS = ("https://strudel.cc",)
# Every route about one track; on a hosted server only its owner gets past the login middleware.
TRACK_PATH = re.compile(r"^/api/tracks/(\d+)(?:/|$)")
EXPIRY_EVERY = 6 * 3600  # seconds between sweeps for expired audio on a hosted server


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


class UrlBody(BaseModel):
    url: str


class QueryBody(BaseModel):
    sql: str


class VersionBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    edits: dict[str, Any]


def tastr_home() -> Path:
    return Path(os.environ.get("TASTR_HOME", Path.home() / ".tastr"))


def upload_dir() -> Path:
    d = tastr_home() / "uploads"
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


def _failure_reason(error: str | None) -> str:
    """A short reason for the admin log, without the error text (which can name server paths)."""
    kind = (error or "").split(":", 1)[0]
    return "it couldn't be decoded as audio" if kind == "DecodeError" else f"the analysis hit an error ({kind or 'unknown'})"


def _cross_site(request) -> bool:
    """A browser request sent by a page on another site (Sec-Fetch-Site, else an Origin for another host).
    Requests without either header (curl, scripts, tests) aren't browser cross-site requests."""
    site = request.headers.get("sec-fetch-site")
    if site:
        return site not in ("same-origin", "none")
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return origin == "null"
    return urlparse(origin).netloc != request.headers.get("host", "")


def versioned_index() -> str:
    """index.html with `?v=<content hash>` on each /static/ file it loads.

    Browsers and Cloudflare cache app.js and style.css for hours when no Cache-Control is sent; a new
    hash after a deploy is a new URL, so the new files load right away."""
    def tag(m: re.Match) -> str:
        f = STATIC_DIR / m.group(2)
        if not f.is_file():
            return m.group(0)
        return f'{m.group(1)}/static/{m.group(2)}?v={hashlib.sha256(f.read_bytes()).hexdigest()[:10]}"'
    return re.sub(r'((?:src|href)=")/static/([\w.-]+)"', tag, (STATIC_DIR / "index.html").read_text())


def create_app(db_path: str | Path | None = None, settings: Settings | None = None, verifier=None) -> FastAPI:
    """`settings` default to the environment (TASTR_HOSTED etc.); `verifier` maps an Access token to an email
    (tests pass a stub; hosted mode otherwise checks tokens against the Access team's keys)."""
    settings = settings or Settings.from_env()
    hosted = settings.hosted
    if hosted and verifier is None:
        verifier = AccessVerifier(settings.access_team_domain, settings.access_aud)
    slots = AnalysisSlots(settings.max_analyses) if hosted else None
    # Without TASTR_LINK_SECRET the Strudel links last only until the server restarts.
    signer = LinkSigner(settings.link_secret or secrets.token_bytes(32)) if hosted else None
    extra_headers = security_headers(inline_script_hashes((STATIC_DIR / "index.html").read_text())) if hosted else {}

    @asynccontextmanager
    async def lifespan(_app):
        stop = threading.Event()
        jobs.resume()  # stem separations the last run left unfinished (e.g. the machine auto-stopped)
        if hosted and settings.audio_days > 0:
            threading.Thread(target=expire_loop, args=(stop,), name="tastr-expiry", daemon=True).start()
        yield
        stop.set()

    app = FastAPI(title="Tastr", docs_url=None if hosted else "/docs", redoc_url=None if hosted else "/redoc",
                  openapi_url=None if hosted else "/openapi.json", lifespan=lifespan)
    jobs = stems.Jobs()
    last_seen: dict[str, str] = {}  # email -> time of their last request, since the server started

    @app.middleware("http")
    async def same_origin_writes(request, call_next):
        # Any page open in the browser could otherwise POST to the local server (analyze a path, delete a
        # track, spend lookup quota). This is not authentication: scripts and curl still get through.
        if request.method not in ("GET", "HEAD", "OPTIONS") and _cross_site(request):
            return JSONResponse({"detail": "Cross-site requests are not allowed."}, status_code=403)
        return await call_next(request)

    if hosted:
        # Added last, so it runs first: nothing else sees a request without a valid Cloudflare Access login.
        @app.middleware("http")
        async def access_login(request, call_next):
            # /pub/ links check their own signature instead (strudel.cc can't sign in).
            if request.url.path != HEALTH_PATH and not request.url.path.startswith(PUBLIC_PREFIX):
                token = request.headers.get(TOKEN_HEADER) or request.cookies.get("CF_Authorization")
                user = None
                if token:
                    try:
                        user = await run_in_threadpool(verifier, token)
                    except Exception as e:  # expired, wrong audience, bad signature, keys unreachable
                        log.info("rejected an Access token: %s", type(e).__name__)
                if not user:
                    await run_in_threadpool(count_blocked)
                    return JSONResponse({"detail": "Sign in through the Tastr link you were invited to."},
                                        status_code=401, headers=extra_headers)
                request.state.user = user
                last_seen[user] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                m = TRACK_PATH.match(request.url.path)
                if m and not await run_in_threadpool(owns, user, int(m[1])):
                    # 404, not 403: someone else's track looks the same as one that doesn't exist.
                    return JSONResponse({"detail": "no such track"}, status_code=404, headers=extra_headers)
            response = await call_next(request)
            response.headers.update(extra_headers)
            return response

    def conn() -> sqlite3.Connection:
        return db.connect(db_path)

    def expire_loop(stop: threading.Event) -> None:
        while True:
            try:
                with closing(conn()) as c:
                    gone = expire_audio(c, upload_dir(), settings.audio_days, settings.admins)
                if gone:
                    log.info("Deleted the audio of %d track(s) older than %g days", len(gone), settings.audio_days)
            except Exception:  # noqa: BLE001 - try again next time
                log.exception("Deleting expired audio failed")
            if stop.wait(EXPIRY_EVERY):
                return

    def count_blocked() -> None:
        with closing(conn()) as c:
            db.add_blocked(c, today())

    def event(user: str | None, kind: str, detail: str, ok: bool = True) -> None:
        """A line in the admin activity log (hosted only; your own machine keeps no log)."""
        if hosted:
            with closing(conn()) as c:
                db.add_event(c, user, kind, detail, ok)

    def refuse(user: str, status: int, message: str, limit: str):
        """Refuse a request over one of the hosted limits, and log which limit it hit."""
        event(user, "limit", f"Hit the {limit}", ok=False)
        return HTTPException(status, message)

    def owns(user: str, track_id: int) -> bool:
        with closing(conn()) as c:
            exists, owner = db.track_owner(c, track_id)
        return exists and owner == user

    def owner_of(request: Request) -> str | None:
        """Whose library a request reads and writes: the signed-in email when hosted, else everything."""
        return request.state.user if hosted else None

    def results(rs, user: str | None = None) -> dict[str, Any]:
        if hosted:  # error text can name server paths; keep it in the log
            for r in rs:
                if r.error:
                    log.warning("analysis failed for %s: %s", r.source.uri, r.error)
                    event(user, "failed", f"Analysis failed: {Path(r.source.uri or 'upload').name}, {_failure_reason(r.error)}",
                          ok=False)
                    r.error = "Couldn't analyze this file."
        return _results(rs)

    def analysis_slot():
        return slots or nullcontext()

    @app.get("/api/config")
    def config(request: Request):
        """What this server allows, so the UI can hide what's turned off. The UI asks once per page load,
        so on a hosted server this also counts each person's visits for the admin view."""
        user = getattr(request.state, "user", None)
        if hosted:
            with closing(conn()) as c:
                first = not db.has_usage(c, user)
                db.add_usage(c, user, "visits", today())
            if first:
                event(user, "signin", "Signed in for the first time")
        return {"hosted": hosted, "links": not hosted, "query": not hosted, "user": user,
                "admin": hosted and user in settings.admins, "limits": settings.limits() if hosted else None}

    @app.get("/api/admin/usage")
    def admin_usage(request: Request):
        """Usage per person, the server's limits, analyses running or waiting, the last 20 events and today's blocked requests,
        for the admin view. Hosted servers only, and only for TASTR_ADMIN_EMAILS; everyone else gets 404."""
        if not hosted or request.state.user not in settings.admins:
            raise HTTPException(404, "Not Found")
        day = today()
        people: dict[str | None, dict[str, Any]] = {}

        def person(email):
            return people.setdefault(email, {"email": email, "tracks": 0, "audio_bytes": 0, "stems_bytes": 0,
                                             "last_upload": None, "last_seen": last_seen.get(email),
                                             "today": {"tracks": 0, "stems": 0, "visits": 0}})

        with closing(conn()) as c:
            for owner, tid, sha, path, at in c.execute(
                    "SELECT owner, id, sha256, local_path, analyzed_at FROM tracks"):
                p = person(owner)
                p["tracks"] += 1
                p["audio_bytes"] += file_bytes(path)
                folder = stems.stems_dir(sha, tid)
                if folder.is_dir():
                    p["stems_bytes"] += sum(f.stat().st_size for f in folder.iterdir() if f.is_file())
                p["last_upload"] = max(p["last_upload"] or "", at)
            history = [{"day": d, "user": u, "kind": k, "n": n} for d, u, k, n in c.execute(
                "SELECT day, user, kind, n FROM user_usage WHERE day >= date('now', '-29 days') ORDER BY day, user, kind")]
            recent = db.recent_events(c)
            blocked = db.blocked_on(c, day)
        for h in history:
            p = person(h["user"])
            if h["day"] == day:
                p["today"][h["kind"]] = h["n"]
        for email in settings.admins:
            person(email)
        users = sorted(people.values(), key=lambda p: (p["email"] is None, p["email"] or ""))
        return {"today": day, "users": users, "history": history, "admins": list(settings.admins),
                "limits": settings.limits(), "recent": recent, "blocked_today": blocked,
                "storage": {"used_bytes": stored_bytes(tastr_home()), "cap_bytes": int(settings.storage_gb * 1024 ** 3)},
                "active_stem_jobs": jobs.active(), "analyses": slots.status()}

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/bench")
    def bench_scores():
        # Headline benchmark scores the Glossary quotes; `tastr bench ... --record` keeps the file current.
        from ..engine.bench import scores

        return scores.load()

    @app.get("/api/patterns")
    def patterns():
        # The engine's template drum patterns per style, for the Glossary's pattern grids.
        from ..engine.stages.suggestions import STYLE_LABELS, VARIATIONS

        return [{"style": STYLE_LABELS[style], "name": name, "pattern": pat}
                for style, items in VARIATIONS.items() for name, pat in items]

    @app.get("/api/tracks")
    def tracks(request: Request):
        with closing(conn()) as c:
            rows = db.list_tracks(c, owner_of(request))
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
        body["stems_available"] = stems.available()
        body["stems_state"] = jobs.status(_stems_dir(card, track_id))["state"]
        body["lookups"] = lookup.describe(card, db.get_lookups(c, track_id))
        body["lookup_providers"] = [{"name": p.name, "label": p.label} for p in lookup.enabled()]
        return body

    def _stems_dir(card, track_id: int) -> Path:
        return stems.stems_dir(card.source.sha256, track_id)

    def _card_or_404(track_id: int):
        with closing(conn()) as c:
            card = db.load_card(c, track_id)
        if card is None:
            raise HTTPException(404, "no such track")
        return card

    def _audio_path(card) -> Path | None:
        p = card.source.local_path
        return Path(p) if p and Path(p).is_file() else None

    @app.get("/api/tracks/{track_id}")
    def track(track_id: int):
        with closing(conn()) as c:
            return card_body(c, track_id)

    @app.get("/api/tracks/{track_id}/audio")
    def track_audio(track_id: int, origin: str | None = Header(None)):
        """The original audio file, with Range support so the browser can seek. strudel.cc may fetch it too."""
        with closing(conn()) as c:
            card = db.load_card(c, track_id)
        if card is None:
            raise HTTPException(404, "no such track")
        path = _audio_path(card)
        if path is None:
            raise HTTPException(404, "The original audio file is not on disk.")
        media = AUDIO_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media, headers=_strudel_cors(origin))

    def _preflight(origin: str | None) -> Response:
        if origin not in STRUDEL_ORIGINS:
            return Response(status_code=403, headers=_strudel_cors(origin))
        return Response(status_code=204, headers=_strudel_cors(origin, preflight=True))

    @app.options("/api/tracks/{track_id}/audio", include_in_schema=False)
    def track_audio_preflight(track_id: int, origin: str | None = Header(None)):
        return _preflight(origin)

    @app.options("/api/tracks/{track_id}/stems/{name}", include_in_schema=False)
    def track_stem_preflight(track_id: int, name: str, origin: str | None = Header(None)):
        return _preflight(origin)

    @app.get("/api/tracks/{track_id}/strudel-link")
    def strudel_link(track_id: int):
        """Where strudel.cc can fetch this track's audio; its stems are next to it (`.../stems/<name>`). On a
        shared server strudel.cc can't pass the login, so this is a signed link that works for LINK_DAYS days."""
        _card_or_404(track_id)
        if signer is None:
            return {"audio": f"/api/tracks/{track_id}/audio", "days": None}
        return {"audio": f"{PUBLIC_PREFIX}{signer.token(track_id)}/tracks/{track_id}/audio", "days": LINK_DAYS}

    def _signed(token: str, track_id: int) -> None:
        if signer is None or not signer.check(token, track_id):
            raise HTTPException(404, "This link has expired. Open the sketch again from Tastr.")

    @app.get(PUBLIC_PREFIX + "{token}/tracks/{track_id}/audio", include_in_schema=False)
    def signed_audio(token: str, track_id: int, origin: str | None = Header(None)):
        _signed(token, track_id)
        return track_audio(track_id, origin)

    @app.get(PUBLIC_PREFIX + "{token}/tracks/{track_id}/stems/{name}", include_in_schema=False)
    def signed_stem(token: str, track_id: int, name: str, origin: str | None = Header(None)):
        _signed(token, track_id)
        return track_stem(track_id, name, origin)

    @app.options(PUBLIC_PREFIX + "{token}/tracks/{track_id}/audio", include_in_schema=False)
    @app.options(PUBLIC_PREFIX + "{token}/tracks/{track_id}/stems/{name}", include_in_schema=False)
    def signed_preflight(token: str, track_id: int, origin: str | None = Header(None), name: str = ""):
        return _preflight(origin)

    def _stems_status(card, track_id: int) -> dict[str, Any]:
        return {**jobs.status(_stems_dir(card, track_id)), "available": stems.available()}

    @app.get("/api/tracks/{track_id}/stems")
    def track_stems(track_id: int):
        """Separation state: none, queued, running (with progress 0-1), ready (with the stem names) or error."""
        return _stems_status(_card_or_404(track_id), track_id)

    @app.post("/api/tracks/{track_id}/stems", status_code=202)
    def separate_stems(track_id: int, request: Request):
        """Start separating one track into stems in the background (one track at a time; others queue)."""
        card = _card_or_404(track_id)
        path = _audio_path(card)
        if path is None:
            raise HTTPException(404, "The original audio file is not on disk, so there is nothing to separate.")
        if not stems.available():
            raise HTTPException(501, stems.INSTALL_HINT)
        folder = _stems_dir(card, track_id)
        if not stems.is_ready(folder) and jobs.status(folder)["state"] not in ("queued", "running"):
            if hosted:
                user = request.state.user
                with closing(conn()) as c:
                    if db.usage_today(c, user, "stems", today()) >= settings.daily_stems:
                        raise refuse(user, 429, f"You've reached today's limit of {settings.daily_stems} stem separations.",
                                     f"daily stem limit ({settings.daily_stems})")
                    db.add_usage(c, user, "stems", today())
                event(user, "stems", "Started separating stems for 1 track")
            jobs.start(path, folder)
        return _stems_status(card, track_id)

    @app.get("/api/tracks/{track_id}/stems/{name}")
    def track_stem(track_id: int, name: str, origin: str | None = Header(None)):
        """One stem as FLAC, like /audio: Range support, and strudel.cc may fetch it."""
        if name not in stems.STEM_NAMES:
            raise HTTPException(404, f"Stems are {', '.join(stems.STEM_NAMES)}.")
        path = stems.stem_path(_stems_dir(_card_or_404(track_id), track_id), name)
        if not path.is_file():
            raise HTTPException(404, "This track has not been separated into stems.")
        return FileResponse(path, media_type="audio/flac", headers=_strudel_cors(origin))

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

    @app.post("/api/tracks/{track_id}/reanalyze")
    def reanalyze(track_id: int, request: Request, meter: str | None = None):
        """Run the full engine again on the track's stored audio file (same id, fresh card).

        `?meter=3/4` or `4/4` sets the time signature, `auto` goes back to detecting it; without it a meter
        the user set before is kept.
        """
        if meter not in (None, "3/4", "4/4", "auto"):
            raise HTTPException(400, "meter must be 3/4, 4/4 or auto")
        beats = service.KEEP if meter is None else None if meter == "auto" else int(meter[0])
        with closing(conn()) as c:
            try:
                with analysis_slot():
                    result = service.reanalyze_track(c, track_id, beats)
            except FileNotFoundError:
                raise HTTPException(410, "The original audio file is no longer on disk.")
            except TimeoutError:
                raise HTTPException(503, "The server is busy analyzing other tracks. Try again in a few minutes.")
            if result is None:
                raise HTTPException(404, "no such track")
            if result.error:
                if hosted:
                    log.warning("re-analysis of track %s failed: %s", track_id, result.error)
                    event(request.state.user, "failed", f"Re-analysis failed, {_failure_reason(result.error)}", ok=False)
                    raise HTTPException(422, "Couldn't analyze the file.")
                raise HTTPException(422, f"Couldn't analyze the file: {result.error}")
            event(owner_of(request), "reanalyze", "Re-analyzed 1 track")
            return {"card": card_body(c, track_id), "edits_dropped": result.edits_dropped}

    @app.post("/api/tracks/{track_id}/lookup")
    def lookup_metadata(track_id: int):
        """Ask the enabled catalogs (TASTR_LOOKUP) for this track's BPM and key again."""
        providers = lookup.enabled()
        if not providers:
            raise HTTPException(400, "No lookup providers are set up. See docs/lookup.md.")
        with closing(conn()) as c:
            card = db.load_card(c, track_id)
            if card is None:
                raise HTTPException(404, "no such track")
            if lookup.query_for(card) is None:
                raise HTTPException(422, "This track has no artist and title tags to look up.")
            results = lookup.lookup_track(c, track_id, card, providers)
            body = card_body(c, track_id)
            # Answers a provider's terms don't let us keep are shown this once, not stored.
            body["lookups"] += lookup.describe(card, [r for r in results if r.get("transient")])
            # "limited" (a guardrail held the request back) and "recent" (asked within the refresh window)
            # aren't stored; the UI says why nothing new came back.
            notes = [{"provider": r["provider"], "status": r["status"], "error": r.get("error")}
                     for r in results if r["status"] in ("limited", "recent", "error")]
            return {"card": body, "notes": notes}

    # Edits to the drums, bass and chords: the current state autosaves; versions are named copies.
    def _track_or_404(c: sqlite3.Connection, track_id: int) -> None:
        if c.execute("SELECT 1 FROM tracks WHERE id = ?", (track_id,)).fetchone() is None:
            raise HTTPException(404, "no such track")

    @app.get("/api/tracks/{track_id}/edits")
    def track_edits(track_id: int):
        with closing(conn()) as c:
            _track_or_404(c, track_id)
            return db.get_edits(c, track_id)

    @app.put("/api/tracks/{track_id}/edits", status_code=204)
    def save_track_edits(track_id: int, edits: dict[str, Any] = Body(...)):
        with closing(conn()) as c:
            _track_or_404(c, track_id)
            db.set_edits(c, track_id, edits)
        return Response(status_code=204)

    @app.get("/api/tracks/{track_id}/edit-versions")
    def edit_versions(track_id: int):
        with closing(conn()) as c:
            _track_or_404(c, track_id)
            return db.list_edit_versions(c, track_id)

    @app.post("/api/tracks/{track_id}/edit-versions", status_code=201)
    def add_edit_version(track_id: int, body: VersionBody):
        with closing(conn()) as c:
            _track_or_404(c, track_id)
            return db.add_edit_version(c, track_id, body.name.strip() or "Version", body.edits)

    @app.get("/api/tracks/{track_id}/edit-versions/{version_id}")
    def edit_version(track_id: int, version_id: int):
        with closing(conn()) as c:
            v = db.get_edit_version(c, track_id, version_id)
        if v is None:
            raise HTTPException(404, "no such version")
        return v

    @app.delete("/api/tracks/{track_id}/edit-versions/{version_id}", status_code=204)
    def delete_edit_version(track_id: int, version_id: int):
        with closing(conn()) as c:
            if not db.delete_edit_version(c, track_id, version_id):
                raise HTTPException(404, "no such version")
        return Response(status_code=204)

    @app.delete("/api/tracks/{track_id}", status_code=204)
    def delete_track(track_id: int):
        with closing(conn()) as c:
            card = db.load_card(c, track_id)
            if not db.delete_track(c, track_id):
                raise HTTPException(404, "no such track")
            # Stems are kept per audio file, so another library holding the same file keeps them.
            shared = card is not None and c.execute(
                "SELECT 1 FROM tracks WHERE sha256 = ?", (card.source.sha256,)).fetchone() is not None
        if card is not None and not shared:
            folder = _stems_dir(card, track_id)
            jobs.forget(folder)
            stems.remove(folder)
        if hosted and card is not None:
            remove_upload(card.source.local_path)
        return Response(status_code=204)

    def remove_upload(path: str | None) -> None:
        """A deleted track's uploaded audio goes too (hosted only: on your own machine Tastr leaves files alone).
        Only Tastr's own copies under uploads/, and only when no other track points at the same file."""
        if not path:
            return
        p, root = Path(path).resolve(), upload_dir().resolve()
        if root not in p.parents:
            return
        with closing(conn()) as c:
            if c.execute("SELECT 1 FROM tracks WHERE local_path = ?", (path,)).fetchone():
                return
        p.unlink(missing_ok=True)
        if p.parent != root and not any(p.parent.iterdir()):
            p.parent.rmdir()

    # Plain `def` endpoints below run in FastAPI's threadpool, so slow analysis
    # doesn't block the event loop.
    @app.post("/api/analyze")
    def analyze(request: Request, files: list[UploadFile] = File(...)):
        if hosted:
            check_upload(request.state.user, files)
        paths: list[str] = []
        for f in files:
            name = re.sub(r"[^\w.\- ]", "_", Path(f.filename or "upload").name) or "upload"
            # The unique part is a folder, so the stored file keeps the name the user gave it (that is what the title falls back to).
            dest = upload_dir() / uuid.uuid4().hex[:8] / name
            dest.parent.mkdir()
            size = 0
            with open(dest, "wb") as out:
                while chunk := f.file.read(1 << 20):
                    size += len(chunk)
                    if hosted and size > settings.max_upload_bytes:
                        break
                    out.write(chunk)
            if hosted and size > settings.max_upload_bytes:
                for p in [dest, *map(Path, paths)]:  # nothing from a refused request stays on disk
                    p.unlink(missing_ok=True)
                    p.parent.rmdir()
                raise refuse(request.state.user, 413, f"{name} is larger than {settings.max_upload_mb:g} MB.",
                             f"upload size limit ({settings.max_upload_mb:g} MB)")
            paths.append(str(dest))
        with closing(conn()) as c:
            if hosted:
                event(request.state.user, "upload", f"Uploaded {len(paths)} track{'s' if len(paths) != 1 else ''}")
            try:
                with analysis_slot():
                    rs = service.analyze_inputs(c, paths, owner=owner_of(request))
            except TimeoutError:
                raise HTTPException(503, "The server is busy analyzing other tracks. Try again in a few minutes.")
            if hosted:  # only tracks that analyzed count toward the daily limit, so a failed try can be retried
                db.add_usage(c, request.state.user, "tracks", today(), sum(r.track_id is not None for r in rs))
            return results(rs, owner_of(request))

    def check_upload(user: str, files: list[UploadFile]) -> None:
        if len(files) > settings.max_files:
            raise refuse(user, 400, f"Upload at most {settings.max_files} files at a time.",
                         f"files-per-upload limit ({settings.max_files})")
        for f in files:
            if Path(f.filename or "").suffix.lower() not in AUDIO_EXTENSIONS:
                raise HTTPException(415, f"{Path(f.filename or 'upload').name} isn't an audio file Tastr can read.")
        if stored_bytes(tastr_home()) >= settings.storage_gb * 1024 ** 3:
            raise refuse(user, 507, "This server is out of space for new uploads. Ask whoever invited you.",
                         f"storage cap ({settings.storage_gb:g} GB)")
        with closing(conn()) as c:
            used = db.usage_today(c, user, "tracks", today())
        if used + len(files) > settings.daily_tracks:
            left = max(0, settings.daily_tracks - used)
            raise refuse(user, 429, f"You can analyze {settings.daily_tracks} tracks a day; {left} left today.",
                         f"daily track limit ({settings.daily_tracks})")

    @app.post("/api/analyze-url")
    def analyze_url(body: UrlBody):
        if hosted:
            raise HTTPException(403, "Link analysis is turned off on this server. Upload the audio file instead.")
        if not is_url(body.url):  # never a path: anyone who can reach the server could read files off its disk
            raise HTTPException(400, "Paste an http(s) link to a track or playlist.")
        with closing(conn()) as c:
            try:
                results = service.analyze_inputs(c, [body.url])
            except IngestError as e:
                raise HTTPException(400, str(e))
        return _results(results)

    @app.post("/api/query")
    def run_query(body: QueryBody):
        if hosted:
            raise HTTPException(403, "The SQL query box is turned off on this server.")
        path = db_path or db.default_db_path()
        try:
            with closing(db.connect(path, readonly=True)) as c:
                cur = c.execute(body.sql)
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = [list(r) for r in cur.fetchmany(MAX_QUERY_ROWS)]
        except sqlite3.Error as e:
            raise HTTPException(400, str(e))
        return {"columns": columns, "rows": rows}

    page = versioned_index()

    @app.get("/", include_in_schema=False)
    def index():
        # The page itself is never cached; its scripts and styles carry a content hash, so a deploy is seen at once.
        return HTMLResponse(page, headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


class _UvicornStyle(logging.Formatter):
    """`INFO:     message`, the same padded prefix uvicorn prints, so the lines line up."""

    def format(self, record: logging.LogRecord) -> str:
        record.levelprefix = f"{record.levelname}:".ljust(9)
        return super().format(record)


def configure_logging(level: str = "info") -> None:
    """Print Tastr's own log lines (failed analyses, stem jobs, and stage tracebacks at debug) next to uvicorn's."""
    handler = logging.StreamHandler()
    handler.setFormatter(_UvicornStyle("%(levelprefix)s %(name)s: %(message)s"))
    logger = logging.getLogger("tastr")
    logger.handlers[:] = [handler]
    logger.setLevel(level.upper())
    logger.propagate = False


def run(host: str = "127.0.0.1", port: int = 8000, db_path: str | None = None, log_level: str = "info") -> None:
    import uvicorn

    from .. import filemeta

    configure_logging(log_level)

    if filemeta.mutagen is None:
        print("Warning: mutagen is not installed, so file tags and audio info can't be read. Run: pip install -e .")
    try:
        settings = Settings.from_env()
    except ValueError as e:
        raise SystemExit(f"Error: {e}")
    if settings.hosted:
        print(f"Hosted mode: logins checked against {settings.access_team_domain}; link analysis and SQL are off.")
    n = db.refresh_file_meta(db.connect(db_path), only_if_stale=True)  # once per tag reader change
    if n:
        print(f"Refreshed file tags for {n} track(s)")
    uvicorn.run(create_app(db_path, settings), host=host, port=port, log_level=log_level)
