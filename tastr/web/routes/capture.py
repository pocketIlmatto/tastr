"""The capture address: an iPhone shortcut (or anything else holding a capture key) adds a moment to one person's
bank without signing in. See docs/capture.md for the request format.

It lives under /pub/, which Cloudflare Access lets through (docs/hosting-map.md), so this checks its own key:
`Authorization: Bearer <key>`, against TASTR_CAPTURE_KEYS (hosted.capture_keys). A key can only add: these two
routes are all it opens, and neither reads, changes or deletes anything already in the bank. The key is never
logged or stored; Tastr keeps only its hash.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from ... import blobs, db, deposits
from ..context import AppContext, tastr_home
from ..hosted import PUBLIC_PREFIX, audio_minutes, capture_owner, stored_bytes, today
from .deposits import MAX_DEPOSITS

log = logging.getLogger(__name__)

CAPTURE_PATH = PUBLIC_PREFIX + "capture"
HEARD_ON = ("spotify", "shazam", "recording")
FIELDS = ("marked_at", "heard_on", "title", "artist", "position_s", "link", "note", "client")
MAX_FIELD, MAX_LINK, MAX_CLIENT = 300, 2000, 100
FORM_SLACK = 64 * 1024  # the form's text fields and multipart framing, on top of the recording
AUDIO_SUFFIXES = {".m4a": ".m4a", ".mp4": ".m4a", ".aac": ".aac", ".mp3": ".mp3", ".wav": ".wav"}
AUDIO_TYPES = {"audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/m4a": ".m4a", "audio/aac": ".aac",
               "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/vnd.wave": ".wav"}
BEFORE_S, AFTER_S = 20.0, 10.0  # a moment marked at position_s spans from this much before it to this much after
USAGE = "captures"  # the daily count's kind in user_usage


class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _answer(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def _text(form, name: str, limit: int) -> str | None:
    value = form.get(name)
    if value is None or isinstance(value, UploadFile):
        return None
    value = str(value).strip()
    if len(value) > limit:
        raise Refused(422, f"{name} can be at most {limit} characters.")
    return value or None


def read_fields(form, now: datetime) -> dict:
    """The capture's fields from the form, checked; empty fields count as missing. Raises Refused (422)."""
    heard_on = (_text(form, "heard_on", 20) or "").lower()
    if heard_on not in HEARD_ON:
        raise Refused(422, f"heard_on is one of: {', '.join(HEARD_ON)}.")
    out = {"heard_on": heard_on, "title": _text(form, "title", MAX_FIELD), "artist": _text(form, "artist", MAX_FIELD),
           "note": _text(form, "note", deposits.MAX_NOTE), "client": _text(form, "client", MAX_CLIENT)}
    marked = _text(form, "marked_at", 64)
    if marked:
        try:
            when = datetime.fromisoformat(marked.replace(" ", "T"))
        except ValueError:
            raise Refused(422, "marked_at has to be a date and time like 2026-11-20T14:03:00-08:00.") from None
        out["marked_at"] = (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).isoformat(timespec="seconds")
    else:
        out["marked_at"] = now.isoformat(timespec="seconds")
    pos = _text(form, "position_s", 32)
    if pos:
        try:
            out["position_s"] = round(float(pos.replace(",", ".")), 3)
        except ValueError:
            out["position_s"] = -1.0
        if not 0 <= out["position_s"] < 24 * 3600:
            raise Refused(422, "position_s has to be seconds into the track, like 95.4.")
    else:
        out["position_s"] = None
    link = _text(form, "link", MAX_LINK)
    if link and urlparse(link).scheme not in ("http", "https"):
        raise Refused(422, "link has to be a web link.")
    out["link"] = link
    return out


def _date(marked_at: str) -> str:
    when = datetime.fromisoformat(marked_at)
    return f"{when.day} {when:%b %Y, %H:%M}"


def title_of(f: dict, has_audio: bool) -> str:
    """"Artist - Title" for a match, else "Recording, 20 Nov 2026, 14:03" (or "Capture, ..." without audio)."""
    if f["title"] or f["artist"]:
        return " - ".join(x for x in (f["artist"], f["title"]) if x)[:deposits.MAX_TITLE]
    return f"{'Recording' if has_audio else 'Capture'}, {_date(f['marked_at'])}"


def _suffix(upload: UploadFile) -> str:
    s = AUDIO_SUFFIXES.get(Path(upload.filename or "").suffix.lower())
    s = s or AUDIO_TYPES.get((upload.content_type or "").split(";")[0].strip().lower())
    if not s:
        raise Refused(422, "The recording has to be m4a, AAC, MP3 or WAV.")
    return s


def router(ctx: AppContext) -> APIRouter:
    api = APIRouter()
    s = ctx.settings
    max_audio = int(s.capture_mb * 1024 * 1024)

    def who(request: Request) -> tuple[bool, str | None]:
        auth = request.headers.get("authorization") or ""
        scheme, _, key = auth.partition(" ")
        if scheme.lower() != "bearer" or not key.strip() or not s.capture_keys:
            return False, None
        return capture_owner(s.capture_keys, key.strip())

    def refused_key() -> JSONResponse:
        return _answer(401, "This capture key isn't one Tastr knows. Check the key in the shortcut.")

    @api.get(CAPTURE_PATH + "/now")
    def now_playing(request: Request):
        """What the key owner is playing on Spotify, for the shortcut's first step. Spotify isn't connected yet,
        so this always answers {"playing": false}: the shortcut then asks Shazam or records."""
        ok, _ = who(request)
        if not ok:
            return refused_key()
        return {"playing": False}

    def save(owner: str | None, f: dict, audio: tuple[bytes, str] | None) -> JSONResponse:
        user = owner or ""
        with closing(ctx.conn()) as c:
            if db.usage_today(c, user, USAGE, today()) >= s.daily_captures:
                raise Refused(429, f"That's {s.daily_captures} captures today, the most for one day. "
                                   "Try again tomorrow (UTC).")
            n = c.execute("SELECT COUNT(*) FROM deposits WHERE owner IS ? AND size != 'track'", (owner,)).fetchone()[0]
            if n >= MAX_DEPOSITS:
                raise Refused(409, f"The bank holds {MAX_DEPOSITS} deposits. Delete some first.")
            if audio and ctx.hosted and stored_bytes(tastr_home()) >= s.storage_gb * 1024 ** 3:
                raise Refused(507, f"The server is at its storage cap ({s.storage_gb:g} GB), so it can't keep "
                                   "a recording now. Send the capture without one.")
            recording, created = store(audio) if audio else (None, False)
            try:
                matched = bool(f["title"] or f["artist"] or f["link"])
                pos = f["position_s"]
                context = {"via": f["heard_on"], "title": f["title"], "artist": f["artist"], "at_s": pos,
                           "site": urlparse(f["link"]).netloc.lower() if f["link"] else None,
                           "recording": recording,
                           "capture": {k: f[k] for k in FIELDS if k != "note" and f.get(k) is not None}}
                d = deposits.add(
                    c, owner, "moment", title=title_of(f, recording is not None), note=f["note"],
                    source_kind="link" if f["link"] else ("recording" if recording else None),
                    source_uri=f["link"], start_s=max(0.0, pos - BEFORE_S) if pos is not None else None,
                    end_s=pos + AFTER_S if pos is not None else None,
                    context={k: v for k, v in context.items() if v is not None},
                    # 'ready' only when the recording is the whole source; a match names a track not here yet
                    status="ready" if recording and not matched else "needs-source")
            except Exception:
                if created:  # never a file an earlier capture of the same audio still points at
                    blobs.remove(tastr_home() / recording)
                raise
            db.add_usage(c, user, USAGE, today())
        log.info("capture: deposit %s added (%s%s)", d["id"], f["heard_on"], ", with a recording" if recording else "")
        return JSONResponse({"id": d["id"], "title": d["title"], "status": d["status"]}, status_code=201)

    def store(audio: tuple[bytes, str]) -> tuple[str, bool]:
        """Keep the recording as captures/<hash>/<hash>.<ext> (in the audio bucket when that is on) after checking
        its length; returns that name, and whether this call made the file (False: the same audio was kept
        before, for an earlier capture). Raises Refused for one too long or unreadable."""
        data, suffix = audio
        h = hashlib.sha256(data).hexdigest()
        path = tastr_home() / "captures" / h[:12] / f"{h[:16]}{suffix}"
        name = path.relative_to(tastr_home()).as_posix()
        if blobs.exists(path):
            return name, False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        try:
            minutes = audio_minutes(path)
            if minutes is None:
                raise Refused(422, "Tastr can't read this recording's length.")
            if minutes * 60 > s.capture_seconds + 1:
                raise Refused(413, f"The recording is longer than {s.capture_seconds:g} seconds.")
            blobs.push(path)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        if blobs.enabled() and not blobs.keep_copies:
            path.unlink(missing_ok=True)
        return name, True

    @api.post(CAPTURE_PATH, status_code=201)
    async def capture(request: Request):
        """Add one moment to the key owner's bank (docs/capture.md)."""
        ok, owner = who(request)
        if not ok:
            return refused_key()
        try:
            size = request.headers.get("content-length")
            if size and size.isdigit() and int(size) > max_audio + FORM_SLACK:
                raise Refused(413, f"The recording is larger than {s.capture_mb:g} MB.")
            try:
                form = await request.form(max_files=1, max_fields=len(FIELDS) + 4)
            except Exception:  # noqa: BLE001 - malformed body, too many parts
                raise Refused(422, "Tastr can't read this request; send a form.") from None
            try:
                f = read_fields(form, datetime.now(timezone.utc))
                audio = None
                upload = form.get("audio")
                if isinstance(upload, UploadFile):
                    data = await upload.read(max_audio + 1)
                    if len(data) > max_audio:
                        raise Refused(413, f"The recording is larger than {s.capture_mb:g} MB.")
                    if data:
                        audio = (data, _suffix(upload))
            finally:
                await form.close()
            return await run_in_threadpool(save, owner, f, audio)
        except Refused as e:
            return _answer(e.status, e.detail)
        except deposits.DepositError as e:
            return _answer(422, str(e))

    return api
