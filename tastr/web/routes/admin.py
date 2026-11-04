"""The admin view's API (hosted servers, TASTR_ADMIN_EMAILS only): usage, the activity log, per-person limits."""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from xml.etree.ElementTree import ParseError
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ... import blobs, db, library_copy, rekordbox_compare, s3
from .. import claude
from ..context import AppContext, tastr_home
from ..hosted import VIEW_AS_COOKIE, VIEW_AS_HOURS, VIEW_AS_PATH, stored_bytes, today

EMAIL = re.compile(r"^[^@\s/]{1,64}@[^@\s/]{1,190}$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REKORDBOX_KEY = "rekordbox/collection.xml"  # where /api/admin/rekordbox-compare looks in the audio bucket by default
REKORDBOX_MAX_MB = 50  # an export is about 1 KB a track; the XML is parsed in memory, so keep well under the Worker's limits
REKORDBOX_LOADED = "Loaded Rekordbox values onto "  # the admin event a load logs; the panel's status line reads it back
USAGE_RANGES = {"1": 1, "7": 7, "30": 30, "all": 365}  # the admin chart's ranges in days; "all" stops at a year


class CopyBody(BaseModel):
    to: str
    track_ids: list[int] = Field(..., min_length=1, max_length=200)


class ViewAsBody(BaseModel):
    email: str


class LimitsBody(BaseModel):
    daily_tracks: int | None = Field(None, ge=0, le=10_000)  # None = the server default
    daily_stems: int | None = Field(None, ge=0, le=1_000)
    daily_asks: int | None = Field(None, ge=0, le=1_000)  # Ask Claude in Strudel
    scope: Literal["today", "standing"] = "today"  # today only (ends at 00:00 UTC), or until an admin changes it


def router(ctx: AppContext) -> APIRouter:
    api = APIRouter()

    @api.get("/api/admin/usage")
    def admin_usage(request: Request, rng: str = Query("30", alias="range")):
        """Usage per person, the server's limits, analyses running or waiting, the last 20 events and today's blocked requests,
        for the admin view, plus `series`: each person's tracks, stem jobs, visits and bytes stored per day of `range`
        (7, 30 or all) or per hour of today (1). Hosted servers only, and only for TASTR_ADMIN_EMAILS; everyone else gets 404."""
        ctx.require_admin(request)
        if rng not in USAGE_RANGES:
            raise HTTPException(400, f"range is one of {', '.join(USAGE_RANGES)}.")
        day = today()
        people: dict[str | None, dict[str, Any]] = {}

        def person(email):
            return people.setdefault(email, {"email": email, "tracks": 0, "audio_bytes": 0, "stems_bytes": 0,
                                             "last_upload": None, "last_seen": ctx.last_seen.get(email),
                                             "today": {"tracks": 0, "stems": 0, "drums": 0, "visits": 0, "asks": 0}})

        with closing(ctx.conn()) as c:
            library = ctx.scan_library(c)
            for r in library:
                p = person(r["owner"])
                p["tracks"] += 1
                p["audio_bytes"] += r["audio"]
                p["stems_bytes"] += r["stems"]
                p["last_upload"] = max(p["last_upload"] or "", r["analyzed_at"])
            ctx.snapshot_storage(library)
            history = [{"day": d, "user": u, "kind": k, "n": n} for d, u, k, n in c.execute(
                "SELECT day, user, kind, n FROM user_usage WHERE day >= date('now', '-29 days') ORDER BY day, user, kind")]
            recent = db.recent_events(c)
            problems = db.recent_events(c, problems=True)
            blocked = db.blocked_on(c, day)
            changed = db.overrides(c, day)
            series = usage_series(c, rng, day, library)
        for h in history:
            p = person(h["user"])
            if h["day"] == day:
                p["today"][h["kind"]] = h["n"]
        for email in [*ctx.settings.admins, *changed]:
            person(email)
        for p in people.values():
            if p["email"]:
                o = changed.get(p["email"])
                p["override"] = o
                s = ctx.settings
                p["caps"] = {"tracks": s.daily_tracks, "stems": s.daily_stems, "asks": s.daily_asks}
                for k in p["caps"]:
                    if o and o[f"daily_{k}"] is not None:
                        p["caps"][k] = o[f"daily_{k}"]
        users = sorted(people.values(), key=lambda p: (p["email"] is None, p["email"] or ""))
        return {"today": day, "users": users, "history": history, "admins": list(ctx.settings.admins),
                "limits": ctx.settings.limits(), "recent": recent, "problems": problems, "blocked_today": blocked,
                "log_links": ctx.settings.log_links(os.environ.get("TASTR_MODAL_APP", "tastr-web")),
                "storage": {"used_bytes": stored_bytes(tastr_home()), "cap_bytes": int(ctx.settings.storage_gb * 1024 ** 3)},
                "active_stem_jobs": ctx.stem_jobs.active(), "active_drum_jobs": ctx.drum_runs.active(), "active_ml_tag_jobs": ctx.ml_runs.active(), "analyses": ctx.slots.status(),
                "series": series,
                # Ask Claude's cost: tokens are counted per person per day (user_usage ask_in/ask_out), priced here.
                "claude": {"model": claude.MODEL, "price_in": claude.PRICE_IN, "price_out": claude.PRICE_OUT}}

    def usage_series(c: sqlite3.Connection, rng: str, day: str, library: list[dict[str, Any]]) -> dict[str, Any]:
        """Per person: tracks, stems, visits and storage (bytes) for each bucket of the range, oldest first.
        Today: 24 UTC hours; storage there counts each track from the hour it was uploaded, at today's size.
        Days: storage is each day's last snapshot (storage_days), carried forward over days without one."""
        kinds = ("tracks", "stems", "visits", "ask_in", "ask_out")
        out: dict[str, dict[str, list]] = {}
        if rng == "1":
            n, now_hour = 24, datetime.now(timezone.utc).hour

            def row(email):
                return out.setdefault(email, {k: [0] * n for k in (*kinds, "storage")})
            for x in db.usage_hours(c, day):
                if x["kind"] in kinds:
                    row(x["user"])[x["kind"]][x["hour"]] += x["n"]
            for r in library:
                if not r["owner"]:
                    continue
                created = r["created_at"] or ""
                first = int(created[11:13]) if created[:10] == day and created[11:13].isdigit() else 0
                if created[:10] > day:
                    continue
                st = row(r["owner"])["storage"]
                for i in range(first, now_hour + 1):
                    st[i] += r["bytes"]
            return {"range": rng, "hourly": True, "now_hour": now_hour, "labels": list(range(n)), "people": out}
        span = USAGE_RANGES[rng]
        start = (datetime.fromisoformat(day) - timedelta(days=span - 1)).date().isoformat()
        if rng == "all":  # from the first day anything was counted, at most a year back
            first = min(filter(None, [c.execute("SELECT min(day) FROM user_usage").fetchone()[0], db.first_storage_day(c), day]))
            start = max(start, first)
        labels = []
        d = datetime.fromisoformat(start)
        while d.date().isoformat() <= day:
            labels.append(d.date().isoformat())
            d += timedelta(days=1)
        at = {lab: i for i, lab in enumerate(labels)}

        def row(email):
            return out.setdefault(email, {k: [0] * len(labels) for k in (*kinds, "storage")})
        for dd, u, k, nn in c.execute("SELECT day, user, kind, n FROM user_usage WHERE day >= ?", (start,)):
            if k in kinds and dd in at:
                row(u)[k][at[dd]] += nn
        # the last snapshot on or before the range start carries into it
        before = {o or None: b for o, b in c.execute(
            "SELECT owner, bytes FROM storage_days WHERE day = (SELECT max(day) FROM storage_days WHERE day < ?)", (start,))}
        snaps: dict[str | None, dict[str, int]] = {}
        for x in db.storage_history(c, start):
            snaps.setdefault(x["owner"], {})[x["day"]] = x["bytes"]
        for owner in {*before, *snaps}:
            if not owner:
                continue
            st, last = row(owner)["storage"], before.get(owner, 0)
            for i, lab in enumerate(labels):
                last = snaps.get(owner, {}).get(lab, last)
                st[i] = last
        return {"range": rng, "hourly": False, "labels": labels, "people": out}

    @api.get("/api/admin/events")
    def admin_events(request: Request, type: str = "", who: str = "", source: str = "", since: str = Query("", alias="from"),
                     until: str = Query("", alias="to"), problems: bool = False, limit: int = Query(200, ge=1, le=1000)):
        """The admin activity log, filtered on the server: type (db.EVENT_TYPES), who (an email, or "-" for lines
        with no person), source (db.EVENT_SOURCES), from/to (UTC days, both included) and problems (not OK only).
        Blocked requests show as one Cloudflare line per day. Admins only."""
        ctx.require_admin(request)
        if type and type not in db.EVENT_TYPES:
            raise HTTPException(400, f"type is one of {', '.join(db.EVENT_TYPES)}.")
        if source and source not in db.EVENT_SOURCES:
            raise HTTPException(400, f"source is one of {', '.join(db.EVENT_SOURCES)}.")
        if any(v and not DAY.match(v) for v in (since, until)):
            raise HTTPException(400, "from and to are days, like 2026-10-31.")
        with closing(ctx.conn()) as c:
            return db.find_events(c, type, who, source, since, until, problems, limit)

    @api.get("/api/admin/copy-tracks")
    def copy_preview(request: Request, to: str, count: int = Query(10, ge=1, le=50), ids: str = ""):
        """What copying your tracks to `to` would do, without doing it: whether `to` has used the server yet, and
        `ids` (comma-separated, up to 200, all yours), each with `they_have` and `has_audio`; without `ids`, your
        `count` most recently added tracks with audio that `to` doesn't have. Admins only."""
        admin = ctx.require_admin(request)
        to = to.strip().lower()
        if not EMAIL.match(to) or to == admin:
            raise HTTPException(400, "Give someone else's email address.")
        wanted = None
        if ids.strip():
            try:
                wanted = sorted({int(x) for x in ids.split(",") if x.strip()})
            except ValueError:
                raise HTTPException(400, "ids is a comma-separated list of track ids.") from None
            if len(wanted) > 200:
                raise HTTPException(400, "Preview at most 200 tracks at a time.")
        with closing(ctx.conn()) as c:
            seen = c.execute("SELECT min(at), max(at) FROM events WHERE user = ?", (to,)).fetchone()
            theirs = c.execute("SELECT count(*) FROM tracks WHERE owner = ?", (to,)).fetchone()[0]
            picks = library_copy.plan(c, admin, to, wanted, count=count)
        if wanted is not None and len(picks) != len(wanted):
            raise HTTPException(404, "no such track")
        return {"to": to, "first_seen": seen[0], "last_seen": seen[1] or ctx.last_seen.get(to), "their_tracks": theirs,
                "tracks": picks}

    @api.post("/api/admin/copy-tracks")
    def copy_tracks(body: CopyBody, request: Request):
        """Copy some of your tracks into someone else's library (tastr/library_copy.py): same analysis, same audio
        file. Tracks they have already, and tracks without audio on the server, are skipped. Admins only; logged as an admin change."""
        admin = ctx.require_admin(request)
        to = body.to.strip().lower()
        if not EMAIL.match(to) or to == admin:
            raise HTTPException(400, "Give someone else's email address.")
        with closing(ctx.conn()) as c:
            made = library_copy.copy_tracks(c, body.track_ids, admin, to)
        if made:
            n = len(made)
            ctx.event(admin, "admin", f"Copied {n} track{'' if n == 1 else 's'} to {to.split('@')[0]}")
        return {"to": to, "copied": [{"from": a, "id": b} for a, b in made.items()],
                "skipped": [t for t in body.track_ids if t not in made]}

    @api.post(VIEW_AS_PATH)
    def view_as(body: ViewAsBody, request: Request):
        """See the app as someone else sees it: their library, limits and screens, read-only. Sets a cookie that the
        login middleware honours for admins only (server.py), for VIEW_AS_HOURS; DELETE stops. Admins only; logged."""
        admin = ctx.require_admin(request)
        email = body.email.strip().lower()
        if not EMAIL.match(email) or email == admin:
            raise HTTPException(400, "Give someone else's email address.")
        with closing(ctx.conn()) as c:
            known = c.execute("SELECT 1 FROM tracks WHERE owner = ? UNION SELECT 1 FROM user_usage WHERE user = ? "
                              "UNION SELECT 1 FROM events WHERE user = ? LIMIT 1", (email, email, email)).fetchone()
        if not known:
            raise HTTPException(404, f"{email} hasn't used Tastr yet.")
        ctx.event(admin, "admin", f"Viewed the app as {email.split('@')[0]}")
        r = JSONResponse({"viewing_as": {"email": email, "by": admin, "read_only": True}})
        r.set_cookie(VIEW_AS_COOKIE, email, max_age=VIEW_AS_HOURS * 3600, path="/", secure=True, httponly=True,
                     samesite="strict")
        return r

    @api.delete(VIEW_AS_PATH)
    def stop_view_as(request: Request):
        """Back to your own view. Works for anyone, so a stale cookie can always be cleared."""
        r = JSONResponse({"viewing_as": None})
        r.delete_cookie(VIEW_AS_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return r

    @api.get("/api/admin/rekordbox-compare")
    def rekordbox(request: Request, key: str = REKORDBOX_KEY, format: Literal["json", "text"] = "json",
                  show: int = Query(25, ge=0, le=500), save: bool = False):
        """Your tracks' stored BPM, key and beat grid next to Rekordbox's, for tracks in both libraries
        (tastr/rekordbox_compare.py, docs/rekordbox.md). Reads the Rekordbox collection XML you put in the audio
        bucket at `key`. `format=text` for the readable summary. `save=1` also stores Rekordbox's BPM and key on
        each of those tracks, so its card's "Listed elsewhere" line and the review list show where Tastr disagrees
        (replacing the last export's). Admins only."""
        admin = ctx.require_admin(request)
        if not key.startswith("rekordbox/") or not key.endswith(".xml") or ".." in key:
            raise HTTPException(400, "key is an .xml file under rekordbox/ in the audio bucket.")
        if not blobs.enabled():
            raise HTTPException(409, "This server has no audio bucket.")
        try:
            data = blobs.client().get(key).read()
        except s3.S3Error as e:
            if e.status == 404:
                raise HTTPException(404, f"Upload your Rekordbox export to the audio bucket as {key} first.") from None
            raise
        try:
            with closing(ctx.conn()) as c:
                rep = rekordbox_compare.compare(c, data, owner=admin)
                if save:
                    rep["saved"] = rekordbox_compare.save_listings(c, data, owner=admin)
        except ParseError as e:
            raise HTTPException(400, f"{key} isn't a Rekordbox XML export: {e}") from None
        if format == "text":
            text = rekordbox_compare.format_report(rep, show)
            if save:
                text += (f"\nSaved Rekordbox's BPM and key on {rep['saved']['saved']} tracks: their cards show it under "
                         "Listed elsewhere, and disagreements are in the review list.")
            return PlainTextResponse(text)
        rep["disagreements"] = rep["disagreements"][:show]
        return rep

    def _rekordbox_status(c: sqlite3.Connection, admin: str) -> dict[str, Any]:
        """The last load from the Admin panel, and how many of your tracks have a Rekordbox listing now."""
        row = c.execute("SELECT at, detail FROM events WHERE user = ? AND kind = 'admin' AND detail LIKE ? "
                        "ORDER BY at DESC, id DESC LIMIT 1", (admin, REKORDBOX_LOADED + "%")).fetchone()
        n = c.execute("SELECT count(*) FROM lookups l JOIN tracks t ON t.id = l.track_id "
                      "WHERE l.provider = ? AND t.owner IS ?", (rekordbox_compare.PROVIDER, admin)).fetchone()[0]
        m = re.search(r" from (.+?)(?: \(\d+ disagree\))?$", row[1]) if row else None
        return {"tracks": n, "loaded_at": row[0] if row else None, "file": m.group(1) if m else None,
                "max_mb": REKORDBOX_MAX_MB}

    @api.get("/api/admin/rekordbox/status")
    def rekordbox_status(request: Request):
        """For Admin › Rekordbox export: when you last loaded an export, from which file, onto how many tracks."""
        admin = ctx.require_admin(request)
        with closing(ctx.conn()) as c:
            return _rekordbox_status(c, admin)

    @api.post("/api/admin/rekordbox/upload")
    async def rekordbox_upload(request: Request, name: str = Query("collection.xml", max_length=200)):
        """Admin › Rekordbox export, step 1: the collection XML as the request body. Stored in the audio bucket as
        rekordbox/collection.xml (replacing the last one) and compared with your library. Nothing is saved on your
        tracks until /api/admin/rekordbox/load. Admins only."""
        admin = ctx.require_admin(request)
        if not blobs.enabled():
            raise HTTPException(409, "This server has no audio bucket.")
        limit = REKORDBOX_MAX_MB * 1024 * 1024
        size = request.headers.get("content-length")
        if size and size.isdigit() and int(size) > limit:
            raise HTTPException(413, f"{name} is {int(size) / 1024 ** 2:.0f} MB. The limit is {REKORDBOX_MAX_MB} MB: "
                                     "export a playlist or a smaller collection.")
        data = await request.body()
        if len(data) > limit:
            raise HTTPException(413, f"{name} is over {REKORDBOX_MAX_MB} MB: export a playlist or a smaller collection.")
        if not rekordbox_compare.is_export(data):
            raise HTTPException(400, f"{name} isn't a Rekordbox collection export. In Rekordbox use File › Export "
                                     "Collection in xml format, then pick that file.")
        with closing(ctx.conn()) as c:
            rep = rekordbox_compare.compare(c, data, owner=admin)
            rep["status"] = _rekordbox_status(c, admin)
        blobs.client().put(REKORDBOX_KEY, data, "application/xml")
        rep["file"] = name
        rep["disagree"] = len(rep["disagreements"])
        rep.pop("disagreements")
        return rep

    @api.post("/api/admin/rekordbox/load")
    def rekordbox_load(request: Request, name: str = Query("collection.xml", max_length=200)):
        """Admin › Rekordbox export, step 2: store the uploaded export's BPM and key on your tracks in both libraries
        (replacing the last load's), so cards show it under Listed elsewhere and disagreements reach the review
        list. Logged as an admin change. Admins only."""
        admin = ctx.require_admin(request)
        if not blobs.enabled():
            raise HTTPException(409, "This server has no audio bucket.")
        try:
            data = blobs.client().get(REKORDBOX_KEY).read()
        except s3.S3Error as e:
            if e.status == 404:
                raise HTTPException(404, "Upload your Rekordbox export first.") from None
            raise
        try:
            with closing(ctx.conn()) as c:
                out = rekordbox_compare.save_listings(c, data, owner=admin)
                out["disagree"] = len(rekordbox_compare.compare(c, data, owner=admin)["disagreements"])
        except ParseError as e:
            raise HTTPException(400, f"The stored export isn't a Rekordbox XML export: {e}") from None
        ctx.event(admin, "admin", f"{REKORDBOX_LOADED}{out['saved']} tracks from {name} ({out['disagree']} disagree)")
        return out

    @api.put("/api/admin/limits/{email}")
    def set_limits(email: str, body: LimitsBody, request: Request):
        """Change one person's daily track, stem job and Claude ask limits, today only or until changed (None = the default).
        Logged as an admin change. Admins only."""
        admin = ctx.require_admin(request)
        email = email.strip().lower()
        if not EMAIL.match(email):
            raise HTTPException(400, "That isn't an email address.")
        day = today()
        with closing(ctx.conn()) as c:
            db.set_override(c, email, body.daily_tracks, body.daily_stems, day if body.scope == "today" else None, admin,
                            body.daily_asks)
            o = db.get_override(c, email, day)
        who = email.split("@")[0]
        if o is None:
            ctx.event(admin, "admin", f"Set {who}'s daily limits back to the defaults")
        else:
            t = body.daily_tracks if body.daily_tracks is not None else ctx.settings.daily_tracks
            st = body.daily_stems if body.daily_stems is not None else ctx.settings.daily_stems
            ak = body.daily_asks if body.daily_asks is not None else ctx.settings.daily_asks
            ctx.event(admin, "admin", f"Set {who}'s daily limits to {t} tracks, {st} stem jobs, {ak} Claude asks "
                  f"({'today only' if body.scope == 'today' else 'until changed'})")
        return {"email": email, "override": o}

    @api.delete("/api/admin/limits/{email}")
    def reset_limits(email: str, request: Request):
        """Put one person back on the server's daily limits. Admins only."""
        return set_limits(email, LimitsBody(), request)

    return api
