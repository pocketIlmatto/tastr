"""Lyrics from LRCLIB (https://lrclib.net), timed line by line when it has them, for the card and karaoke.

Off by default: nothing is asked until TASTR_LYRICS=lrclib. Like the catalog lookups (tastr/lookup), the request
sends the track's artist, title, album and length from its tags, never audio, goes through the same guardrails
(one request at a time, a daily cap; TASTR_LOOKUP_LRCLIB_INTERVAL / _DAILY) and the answer is stored per track in the
`lyrics` table, kept across re-analysis.

LRCLIB needs no key. Its lyrics are contributed by its users; the words still belong to their writers and
publishers, and LRCLIB doesn't license them. So Tastr shows them to the person whose library the track is in and
keeps them out of everything shared or downloaded (the library snapshot, mash-up links), and the tests use made-up
lines.

`lookup_track` first asks /api/get, which matches artist, title, album and length (within about 2 s), then falls
back to /api/search and picks a hit whose artist and title match and whose length is within MAX_LENGTH_GAP.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from typing import Any

from .lookup.base import Provider, ProviderError, TrackQuery, same_artists, same_text

log = logging.getLogger(__name__)

MAX_LENGTH_GAP = 4.0  # seconds: a search hit further from the track's length is another version
SETTLED = ("found", "not_found")


class LRCLIB(Provider):
    name = "lrclib"
    label = "LRCLIB"
    homepage = "https://lrclib.net"
    key_env = None
    # LRCLIB asks clients not to send requests in parallel; the guard sends one at a time at least this far apart.
    min_interval = 1.0
    daily_limit = 1000
    base = "https://lrclib.net/api"

    def lookup(self, q: TrackQuery) -> dict[str, Any]:
        hit = None
        if q.duration:
            params = {"track_name": q.title, "artist_name": q.artist, "duration": str(round(q.duration))}
            if q.album:
                params["album_name"] = q.album
            try:
                _, hit = self.get_json(f"{self.base}/get", params)
            except ProviderError as e:
                if e.status != 404:  # 404: no record with those details; the search below is looser
                    raise
                hit = None
        if not isinstance(hit, dict):
            _, hits = self.get_json(f"{self.base}/search", {"track_name": q.title, "artist_name": q.artist})
            hit = pick(hits if isinstance(hits, list) else [], q)
        if not hit:
            return {"provider": self.name, "status": "not_found"}
        return result(hit, self)


LRCLIB_PROVIDER = LRCLIB()


def enabled() -> LRCLIB | None:
    """The lyrics source, when TASTR_LYRICS names it."""
    names = {n.strip().lower() for n in os.environ.get("TASTR_LYRICS", "").split(",") if n.strip()}
    return LRCLIB_PROVIDER if "lrclib" in names else None


def pick(hits: list[dict], q: TrackQuery) -> dict | None:
    """The search hit for this track: artist and title match, length within MAX_LENGTH_GAP when both are known.
    Of those, timed lyrics first, then the closest length."""
    def gap(h):
        d = h.get("duration")
        return abs(float(d) - q.duration) if isinstance(d, (int, float)) and q.duration else 0.0

    ok = [h for h in hits if isinstance(h, dict) and same_artists(h.get("artistName"), q.artist)
          and same_text(h.get("trackName"), q.title) and gap(h) <= MAX_LENGTH_GAP]
    ok.sort(key=lambda h: (not h.get("syncedLyrics"), gap(h)))
    return ok[0] if ok else None


def result(hit: dict, p: Provider) -> dict[str, Any]:
    synced = parse_lrc(hit.get("syncedLyrics") or "")
    plain = (hit.get("plainLyrics") or "").strip() or "\n".join(x["line"] for x in synced)
    return {"provider": p.name, "status": "found", "id": hit.get("id"),
            "matched_artist": hit.get("artistName"), "matched_title": hit.get("trackName"),
            "duration": hit.get("duration"), "instrumental": bool(hit.get("instrumental")),
            "synced": synced, "plain": plain or None,
            "url": f"{p.homepage}/lyrics/{hit['id']}" if hit.get("id") else p.homepage}


_STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")


def parse_lrc(text: str) -> list[dict[str, Any]]:
    """[{t: seconds, line}] from LRC text, in time order. A line may carry several stamps ("[00:12.00][01:30.50]
    chorus"); tags like [ar: ...] and lines without a stamp are skipped. Empty lines are kept: they mark a gap."""
    out = []
    for raw in text.splitlines():
        stamps, rest = [], raw.strip()
        while (m := _STAMP.match(rest)):
            stamps.append(int(m.group(1)) * 60 + float(m.group(2).replace(":", ".")))
            rest = rest[m.end():]
        out += [{"t": round(t, 2), "line": rest.strip()} for t in stamps]
    out.sort(key=lambda x: x["t"])
    return out


def lookup_track(conn: sqlite3.Connection, track_id: int, card) -> dict[str, Any]:
    """Ask LRCLIB for the track's lyrics and store the answer. An error doesn't replace lyrics already found.
    Raises guard.Limited when the guardrails hold the request back (nothing is stored)."""
    from . import db, lookup
    from .lookup.guard import usage_conn

    p = enabled()
    if p is None:
        raise RuntimeError("Lyrics aren't set up on this server (TASTR_LYRICS).")
    q = lookup.query_for(card)
    if q is None:
        res: dict[str, Any] = {"provider": p.name, "status": "no_tags"}
    else:
        token = usage_conn.set(conn)
        try:
            res = p.lookup(q)
        except ProviderError as e:
            res = {"provider": p.name, "status": "error", "error": str(e)}
        finally:
            usage_conn.reset(token)
    stored = db.get_lyrics(conn, track_id)
    if keep_stored(stored, res):
        pass
    elif res["status"] in SETTLED or (stored or {}).get("status") not in SETTLED:
        db.save_lyrics(conn, track_id, res)
    out = describe(db.get_lyrics(conn, track_id) or res)
    if res["status"] == "error" and out["state"] != "error":
        out["last_error"] = res["error"]  # lyrics found earlier stay; the page can still say this ask failed
    return out


def keep_stored(stored: dict | None, res: dict) -> bool:
    """Whether LRCLIB's new answer leaves the stored lyrics as they are, because they came from listening
    (tastr/listen.py): heard lyrics stay unless LRCLIB now has the words, and LRCLIB's plain words timed by
    listening stay while LRCLIB still has the same words and no times of its own."""
    if not stored or stored.get("status") != "found":
        return False
    if stored.get("provider") == "whisper":
        return res["status"] != "found"
    if stored.get("timed_by") == "whisper":
        return res["status"] != "found" or (not res.get("synced") and res.get("plain") == stored.get("plain"))
    return False


WHISPER_LABEL, WHISPER_HOME = "Whisper", "https://github.com/openai/whisper"


def describe(stored: dict | None) -> dict[str, Any]:
    """A stored answer for the page: `state` found | not_found | no_tags | error | none (never asked), `timed`
    (whether the lines have times), `source` lrclib | whisper (heard by listening, tastr/listen.py), `timed_by`
    whisper when LRCLIB's words got their times by listening, and the credit (`label`, `homepage`)."""
    if not stored:
        return {"state": "none"}
    out = {k: v for k, v in stored.items() if k not in ("status", "provider")}
    out["state"] = stored.get("status")
    out["source"] = stored.get("provider")
    heard = stored.get("provider") == "whisper"
    out["label"], out["homepage"] = (WHISPER_LABEL, WHISPER_HOME) if heard else (LRCLIB.label, LRCLIB.homepage)
    out["timed"] = bool(stored.get("synced"))
    return out
