"""Guardrails so nothing (a bulk run, a stuck client, a hostile one once Tastr is shared) hammers a provider.

Every HTTP request a provider makes goes through `before_request` / `after_response`, so the limits hold
however a lookup was started (after analysis, the ⋯ menu, bulk actions, `tastr lookup`):

- spacing: at most one request per `min_interval` seconds per provider, one at a time;
- budgets: requests per UTC day and per calendar month, counted in the `lookup_usage` table so they
  survive restarts (in memory when no database is attached, e.g. in tests);
- the provider's own quota headers (`x-quota-used` / `x-quota-limit`, `x-ratelimit-remaining`): stop
  `QUOTA_MARGIN` requests short of its limit until its reset time;
- backoff: HTTP 429 / 5xx pause the provider (Retry-After when given, else doubling from a minute up to
  an hour); 401 / 403 pause it for an hour, since a bad key won't fix itself.

Limits are per-provider class attributes (`min_interval`, `daily_limit`, `monthly_limit`) with env overrides
TASTR_LOOKUP_<NAME>_DAILY / _MONTHLY / _INTERVAL. A request over a limit raises `Limited` instead of going
out. See docs/lookup.md.
"""

from __future__ import annotations

import contextvars
import os
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

QUOTA_MARGIN = 20
BACKOFF_START, BACKOFF_MAX, AUTH_PAUSE = 60.0, 3600.0, 3600.0

# The database connection usage is counted in, set by lookup_track for the duration of its calls.
usage_conn: contextvars.ContextVar = contextvars.ContextVar("lookup_usage_conn", default=None)


class Limited(Exception):
    """A request was held back by a guardrail; nothing was sent."""


class Guard:
    def __init__(self, provider):
        self.p = provider
        self.lock = threading.Lock()
        self.last = 0.0
        self.paused_until = 0.0
        self.backoff = 0.0
        self.pause_reason = ""
        self.memory: dict[str, int] = {}  # usage counts when no database is attached

    # --- limits -----------------------------------------------------------------------------------
    def _limit(self, kind: str, default):
        v = os.environ.get(f"TASTR_LOOKUP_{self.p.name.upper()}_{kind}")
        try:
            return type(default)(v) if v not in (None, "") and default is not None else (float(v) if v else default)
        except ValueError:
            return default

    def limits(self) -> dict:
        return {"interval": self._limit("INTERVAL", float(self.p.min_interval)),
                "daily": self._limit("DAILY", self.p.daily_limit),
                "monthly": self._limit("MONTHLY", self.p.monthly_limit)}

    # --- usage --------------------------------------------------------------------------------------
    @staticmethod
    def _periods(now: float) -> tuple[str, str]:
        d = datetime.fromtimestamp(now, timezone.utc)
        return d.strftime("%Y-%m-%d"), d.strftime("%Y-%m")

    def used(self, now: float | None = None) -> tuple[int, int]:
        """(requests today, requests this month), UTC."""
        day, month = self._periods(now or time.time())
        conn = usage_conn.get()
        if conn is None:
            return self.memory.get(day, 0), sum(n for d, n in self.memory.items() if d.startswith(month))
        today = conn.execute("SELECT requests FROM lookup_usage WHERE provider = ? AND day = ?", (self.p.name, day)).fetchone()
        mon = conn.execute("SELECT COALESCE(SUM(requests), 0) FROM lookup_usage WHERE provider = ? AND day LIKE ?",
                           (self.p.name, month + "-%")).fetchone()
        return (today[0] if today else 0), mon[0]

    def _count(self, now: float) -> None:
        day, _ = self._periods(now)
        conn = usage_conn.get()
        if conn is None:
            self.memory[day] = self.memory.get(day, 0) + 1
            return
        with conn:
            conn.execute("INSERT INTO lookup_usage VALUES (?, ?, 1) ON CONFLICT(provider, day) "
                         "DO UPDATE SET requests = requests + 1", (self.p.name, day))

    # --- the gate -------------------------------------------------------------------------------------
    def before_request(self) -> None:
        """Wait out the spacing, then count the request, or raise Limited if a budget or pause forbids it.
        Holds the provider's lock until after_response, so requests to one provider never overlap."""
        self.lock.acquire()
        try:
            now = time.time()
            if now < self.paused_until:
                raise Limited(f"{self.p.label} is paused for {int(self.paused_until - now) + 1} s: {self.pause_reason}")
            lim = self.limits()
            today, month = self.used(now)
            if lim["daily"] is not None and today >= lim["daily"]:
                raise Limited(f"{self.p.label}: daily limit of {lim['daily']} requests reached")
            if lim["monthly"] is not None and month >= lim["monthly"]:
                raise Limited(f"{self.p.label}: monthly limit of {lim['monthly']} requests reached")
            wait = self.last + lim["interval"] - now
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self._count(self.last)
        except BaseException:
            self.lock.release()
            raise

    def after_response(self, status: int | None, headers=None) -> None:
        """Read quota headers and back off on 429 / 5xx / auth errors; releases the lock."""
        try:
            h = {k.lower(): v for k, v in (headers or {}).items()}
            now = time.time()
            if status in (429, 502, 503, 504):
                retry = _seconds(h.get("retry-after"), now)
                self.backoff = min(BACKOFF_MAX, max(BACKOFF_START, self.backoff * 2))
                self._pause(now + (retry if retry is not None else self.backoff), f"HTTP {status}")
            elif status in (401, 403):
                self._pause(now + AUTH_PAUSE, f"HTTP {status}" + (f", check {self.p.key_env}" if self.p.key_env else ""))
            elif status is not None and status < 400:
                self.backoff = 0.0
            used, limit = _int(h.get("x-quota-used")), _int(h.get("x-quota-limit"))
            remaining = _int(h.get("x-ratelimit-remaining") or h.get("x-quota-remaining"))
            if remaining is None and used is not None and limit is not None:
                remaining = limit - used
            if remaining is not None and remaining <= QUOTA_MARGIN:
                reset = _seconds(h.get("x-quota-reset") or h.get("x-ratelimit-reset"), now)
                self._pause(now + (reset if reset is not None else 24 * 3600),
                            f"its quota is nearly used up ({remaining} left)")
        finally:
            self.lock.release()

    def _pause(self, until: float, reason: str) -> None:
        if until > self.paused_until:
            self.paused_until, self.pause_reason = until, reason

    def status(self) -> dict:
        today, month = self.used()
        return {"provider": self.p.name, "today": today, "month": month, **self.limits(),
                "paused_for": max(0, int(self.paused_until - time.time())), "pause_reason": self.pause_reason}


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _seconds(v, now: float) -> float | None:
    """Seconds until a Retry-After / reset value: delta seconds, a Unix time, an HTTP date or an ISO date."""
    if v in (None, ""):
        return None
    try:
        x = float(v)
        return max(0.0, x - now) if x > 1e9 else max(0.0, x)
    except ValueError:
        pass
    for parse in (parsedate_to_datetime, lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))):
        try:
            d = parse(v)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return max(0.0, d.timestamp() - now)
        except (TypeError, ValueError):
            continue
    return None
