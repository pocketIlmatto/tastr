"""The admin view's API (hosted servers, TASTR_ADMIN_EMAILS only): usage, the activity log, per-person limits."""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ... import db
from ..context import AppContext, tastr_home
from ..hosted import stored_bytes, today

EMAIL = re.compile(r"^[^@\s/]{1,64}@[^@\s/]{1,190}$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
USAGE_RANGES = {"1": 1, "7": 7, "30": 30, "all": 365}  # the admin chart's ranges in days; "all" stops at a year


class LimitsBody(BaseModel):
    daily_tracks: int | None = Field(None, ge=0, le=10_000)  # None = the server default
    daily_stems: int | None = Field(None, ge=0, le=1_000)
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
                                             "today": {"tracks": 0, "stems": 0, "drums": 0, "visits": 0}})

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
                p["caps"] = {k: ctx.settings.daily_tracks if k == "tracks" else ctx.settings.daily_stems for k in ("tracks", "stems")}
                for k in p["caps"]:
                    if o and o[f"daily_{k}"] is not None:
                        p["caps"][k] = o[f"daily_{k}"]
        users = sorted(people.values(), key=lambda p: (p["email"] is None, p["email"] or ""))
        return {"today": day, "users": users, "history": history, "admins": list(ctx.settings.admins),
                "limits": ctx.settings.limits(), "recent": recent, "problems": problems, "blocked_today": blocked,
                "log_links": ctx.settings.log_links(os.environ.get("TASTR_MODAL_APP", "tastr-web")),
                "storage": {"used_bytes": stored_bytes(tastr_home()), "cap_bytes": int(ctx.settings.storage_gb * 1024 ** 3)},
                "active_stem_jobs": ctx.stem_jobs.active(), "active_drum_jobs": ctx.drum_runs.active(), "analyses": ctx.slots.status(),
                "series": series}

    def usage_series(c: sqlite3.Connection, rng: str, day: str, library: list[dict[str, Any]]) -> dict[str, Any]:
        """Per person: tracks, stems, visits and storage (bytes) for each bucket of the range, oldest first.
        Today: 24 UTC hours; storage there counts each track from the hour it was uploaded, at today's size.
        Days: storage is each day's last snapshot (storage_days), carried forward over days without one."""
        kinds = ("tracks", "stems", "visits")
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

    @api.put("/api/admin/limits/{email}")
    def set_limits(email: str, body: LimitsBody, request: Request):
        """Change one person's daily track and stem job limits, today only or until changed (None = the default).
        Logged as an admin change. Admins only."""
        admin = ctx.require_admin(request)
        email = email.strip().lower()
        if not EMAIL.match(email):
            raise HTTPException(400, "That isn't an email address.")
        day = today()
        with closing(ctx.conn()) as c:
            db.set_override(c, email, body.daily_tracks, body.daily_stems, day if body.scope == "today" else None, admin)
            o = db.get_override(c, email, day)
        who = email.split("@")[0]
        if o is None:
            ctx.event(admin, "admin", f"Set {who}'s daily limits back to the defaults")
        else:
            t = body.daily_tracks if body.daily_tracks is not None else ctx.settings.daily_tracks
            st = body.daily_stems if body.daily_stems is not None else ctx.settings.daily_stems
            ctx.event(admin, "admin", f"Set {who}'s daily limits to {t} tracks, {st} stem jobs "
                  f"({'today only' if body.scope == 'today' else 'until changed'})")
        return {"email": email, "override": o}

    @api.delete("/api/admin/limits/{email}")
    def reset_limits(email: str, request: Request):
        """Put one person back on the server's daily limits. Admins only."""
        return set_limits(email, LimitsBody(), request)

    return api
