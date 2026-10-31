"""Look tracks up in online metadata catalogs and compare their BPM / key with ours.

Opt-in: nothing leaves the machine unless TASTR_LOOKUP names providers (comma-separated, e.g.
"getsongbpm,freqblog") and each one is set up (its API key, or AcousticBrainz's index). Lookups send
the track's artist and title from its tags, never audio. Results are stored per provider in the `lookups` table and shown next to the
engine's values; they never replace them. See docs/lookup.md.

Adding a provider: a module with a `base.Provider` subclass that calls `register(...)`, imported at the
bottom of this file.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from typing import Any

from .base import LookupResult, Provider, ProviderError, TrackQuery, parse_listed_key
from .guard import Limited, usage_conn

log = logging.getLogger(__name__)

PROVIDERS: dict[str, Provider] = {}
BPM_TOLERANCE = 0.04  # the benchmark's Acc1 window


def register(provider: Provider) -> Provider:
    PROVIDERS[provider.name] = provider
    return provider


def enabled() -> list[Provider]:
    """Providers named in TASTR_LOOKUP (in that order) that are set up (API key, index...)."""
    names = [n.strip().lower() for n in os.environ.get("TASTR_LOOKUP", "").split(",") if n.strip()]
    out = []
    for n in names:
        p = PROVIDERS.get(n)
        if p is None:
            log.warning("TASTR_LOOKUP names unknown provider %r (known: %s)", n, ", ".join(PROVIDERS))
        elif not p.configured():
            log.warning("lookup provider %s is enabled but %s", n, p.setup_hint())
        else:
            out.append(p)
    return out


def query_for(card) -> TrackQuery | None:
    """Artist and title from the card's tags; None when either is missing (a filename isn't enough)."""
    s = card.source
    tags = s.file_tags or {}
    artist, title = s.artist or tags.get("artist"), tags.get("title") or (s.title if s.kind != "file" else None)
    if not (artist and title):
        return None
    mbid = (tags.get("musicbrainz_recording") or "").strip().lower()
    isrc = re.sub(r"[^A-Z0-9]", "", (tags.get("isrc") or "").upper())
    return TrackQuery(artist=artist, title=title, album=s.album or tags.get("album"), duration=card.duration,
                      mbid=mbid if MBID.fullmatch(mbid) else None, isrc=isrc if len(isrc) == 12 else None)


MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SETTLED = ("found", "not_found")  # answers worth keeping; pending, no_tags and error are asked again
REFRESH_MINUTES = 10.0  # a track isn't asked about again sooner than this (TASTR_LOOKUP_REFRESH_MINUTES)
_asked: dict[tuple[int, str], float] = {}  # (track id, provider) -> last ask, for providers we can't store


def _refresh_seconds() -> float:
    try:
        return 60 * float(os.environ.get("TASTR_LOOKUP_REFRESH_MINUTES", REFRESH_MINUTES))
    except ValueError:
        return 60 * REFRESH_MINUTES


def _recently_asked(stored: dict[str, dict], track_id: int, p: Provider, now: float) -> bool:
    """Whether this provider answered about this track within the refresh window. An error doesn't count,
    so the track can be asked again right away; the guardrails still pace and pause the provider."""
    last = _asked.get((track_id, p.name), 0.0)
    r = stored.get(p.name)
    if r and r.get("fetched_at") and r.get("status") != "error":
        try:
            last = max(last, datetime.fromisoformat(r["fetched_at"]).timestamp())
        except ValueError:
            pass
    return now - last < _refresh_seconds()


def lookup_track(conn: sqlite3.Connection, track_id: int, card, providers: list[Provider] | None = None,
                 only_missing: bool = False) -> list[dict]:
    """Ask each provider about the track and store the answers. Returns what was stored, errors included.

    A provider that fails (network, bad key) is recorded with status "error" and its message, so the UI
    can say why; the others still run. `only_missing` skips providers that already have a settled
    answer for the track (analysis uses it, so re-analyzing doesn't spend API quota again).

    Guardrails (guard.py) can hold a request back: that comes back as status "limited" and is never
    stored. Neither an error nor "limited" replaces an answer already stored. A provider asked about the
    track within the last REFRESH_MINUTES is skipped ("recent"), so repeated clicks cost nothing; one that
    failed isn't, since the guardrails already pause a provider that pushes back.
    """
    from .. import db

    providers = enabled() if providers is None else providers
    stored = {r["provider"]: r for r in db.get_lookups(conn, track_id)}
    if only_missing:
        providers = [p for p in providers if stored.get(p.name, {}).get("status") not in SETTLED and p.may_store()]
    q = query_for(card)
    out = []
    token = usage_conn.set(conn)
    try:
        for p in providers:
            now = time.time()
            if q is None:
                res = {"provider": p.name, "status": "no_tags"}
            elif _recently_asked(stored, track_id, p, now):
                out.append({"provider": p.name, "status": "recent"})
                continue
            else:
                _asked[(track_id, p.name)] = now
                try:
                    res = normalize(p.lookup(q)).to_dict()
                except Limited as e:
                    _asked.pop((track_id, p.name), None)  # nothing was asked
                    out.append({"provider": p.name, "status": "limited", "error": str(e)})
                    continue
                except ProviderError as e:
                    _asked.pop((track_id, p.name), None)
                    res = {"provider": p.name, "status": "error", "error": str(e)}
                except Exception as e:  # a provider bug shouldn't break analysis
                    _asked.pop((track_id, p.name), None)
                    log.warning("lookup %s failed: %s", p.name, e)
                    res = {"provider": p.name, "status": "error", "error": f"{type(e).__name__}: {e}"}
            if not p.may_store():
                res["transient"] = True  # the provider's terms don't allow keeping it
            elif res["status"] in SETTLED or stored.get(p.name, {}).get("status") not in SETTLED:
                db.save_lookup(conn, track_id, res)
            out.append(res)
    finally:
        usage_conn.reset(token)
    return out


def usage(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Requests used and limits per enabled provider (for `tastr lookup --usage` and the docs)."""
    token = usage_conn.set(conn)
    try:
        return [p.guard.status() for p in enabled()]
    finally:
        usage_conn.reset(token)


def normalize(r: LookupResult) -> LookupResult:
    """Fill tonic / mode (and Camelot when missing) from whatever key spelling the provider used."""
    from ..engine.music import camelot

    k = parse_listed_key(r.key, r.camelot, r.extra.get("open_key"))
    if k:
        r.tonic, r.mode = k
        r.camelot = r.camelot or camelot(*k)
    return r


def describe(card, stored: list[dict]) -> list[dict]:
    """Stored lookups ready for the UI: each with the provider's label and homepage and `compare`."""
    out = []
    for r in stored:
        p = PROVIDERS.get(r["provider"])
        out.append(r | {"label": p.label if p else r["provider"], "homepage": p.homepage if p else None,
                        "compare": compare(card, r) if r.get("status") == "found" else None})
    return out


def compare(card, res: dict[str, Any]) -> dict[str, Any]:
    """How a stored lookup compares with the card: bpm "agrees" | "double" | "half" (the listing relative to
    ours) | "differs", and key "agrees" | "relative" | "fifth" | "parallel" | "differs" (None when either
    side has no value)."""
    from ..engine.bench.metrics import key_category

    out: dict[str, Any] = {"bpm": None, "key": None}
    ours, theirs = card.core.bpm, res.get("bpm")
    if ours and theirs:
        r = theirs / ours  # "half": the listing is half our tempo
        out["bpm"] = next((name for name, f in (("agrees", 1), ("double", 2), ("half", 0.5))
                           if abs(r - f) <= BPM_TOLERANCE * f), "differs")
    k = card.core.key
    listed = (res["tonic"], res["mode"]) if res.get("tonic") is not None and res.get("mode") else \
        parse_listed_key(res.get("key"), res.get("camelot"))
    if k and listed:
        cat = key_category(k.tonic, k.mode, *listed)
        out["key"] = {"correct": "agrees", "other": "differs"}.get(cat, cat)
    return out


from . import acousticbrainz, freqblog, getsongbpm  # noqa: E402,F401  registers the built-in providers
