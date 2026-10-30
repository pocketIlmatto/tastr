"""Look tracks up in online metadata catalogs and compare their BPM / key with ours.

Opt-in: nothing leaves the machine unless TASTR_LOOKUP names providers (comma-separated, e.g.
"getsongbpm,freqblog") and each one's API key is set. Lookups send the track's artist and title from
its tags, never audio. Results are stored per provider in the `lookups` table and shown next to the
engine's values; they never replace them. See docs/lookup.md.

Adding a provider: a module with a `base.Provider` subclass that calls `register(...)`, imported at the
bottom of this file.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

from .base import LookupResult, Provider, ProviderError, TrackQuery, parse_listed_key

log = logging.getLogger(__name__)

PROVIDERS: dict[str, Provider] = {}
BPM_TOLERANCE = 0.04  # the benchmark's Acc1 window


def register(provider: Provider) -> Provider:
    PROVIDERS[provider.name] = provider
    return provider


def enabled() -> list[Provider]:
    """Providers named in TASTR_LOOKUP (in that order) that have their API key set."""
    names = [n.strip().lower() for n in os.environ.get("TASTR_LOOKUP", "").split(",") if n.strip()]
    out = []
    for n in names:
        p = PROVIDERS.get(n)
        if p is None:
            log.warning("TASTR_LOOKUP names unknown provider %r (known: %s)", n, ", ".join(PROVIDERS))
        elif not p.configured():
            log.warning("lookup provider %s is enabled but %s is not set", n, p.key_env)
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
    return TrackQuery(artist=artist, title=title, album=s.album or tags.get("album"), duration=card.duration)


SETTLED = ("found", "not_found")  # answers worth keeping; pending, no_tags and error are asked again


def lookup_track(conn: sqlite3.Connection, track_id: int, card, providers: list[Provider] | None = None,
                 only_missing: bool = False) -> list[dict]:
    """Ask each provider about the track and store the answers. Returns what was stored, errors included.

    A provider that fails (network, bad key) is recorded with status "error" and its message, so the UI
    can say why; the others still run. `only_missing` skips providers that already have a settled
    answer for the track (analysis uses it, so re-analyzing doesn't spend API quota again).
    """
    from .. import db

    providers = enabled() if providers is None else providers
    if only_missing:
        done = {r["provider"] for r in db.get_lookups(conn, track_id) if r.get("status") in SETTLED}
        providers = [p for p in providers if p.name not in done and p.may_store()]
    q = query_for(card)
    out = []
    for p in providers:
        if q is None:
            res = {"provider": p.name, "status": "no_tags"}
        else:
            try:
                res = normalize(p.lookup(q)).to_dict()
            except ProviderError as e:
                res = {"provider": p.name, "status": "error", "error": str(e)}
            except Exception as e:  # a provider bug shouldn't break analysis
                log.warning("lookup %s failed: %s", p.name, e)
                res = {"provider": p.name, "status": "error", "error": f"{type(e).__name__}: {e}"}
        if p.may_store():
            db.save_lookup(conn, track_id, res)
        else:
            res["transient"] = True  # the provider's terms don't allow keeping it
        out.append(res)
    return out


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


from . import freqblog, getsongbpm  # noqa: E402,F401  registers the built-in providers
