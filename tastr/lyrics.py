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

import hashlib
import json
import logging
import math
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
    out = describe(db.get_lyrics(conn, track_id) or res, db.get_lyrics_edits(conn, track_id))
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


def describe(stored: dict | None, edits: dict | None = None) -> dict[str, Any]:
    """A stored answer for the page: `state` found | not_found | no_tags | error | none (never asked), `timed`
    (whether the lines have times), `source` lrclib | whisper (heard by listening, tastr/listen.py), `timed_by`
    whisper when LRCLIB's words got their times by listening, and the credit (`label`, `homepage`).

    With the person's own edits (edit_lines), `synced` and `plain` are theirs, with `edited: true`, `edited_at`,
    `base` (lrclib | whisper | none: what they started from) and `newer: true` when LRCLIB or Whisper has given a
    different answer since (keep_edits hides it). `timed_by` is then whisper only when listening timed their words."""
    if not stored and not edits:
        return {"state": "none"}
    out: dict[str, Any] = {"state": "none"}
    if stored:
        out = {k: v for k, v in stored.items() if k not in ("status", "provider")}
        out["state"] = stored.get("status")
        out["source"] = stored.get("provider")
        heard = stored.get("provider") == "whisper"
        out["label"], out["homepage"] = (WHISPER_LABEL, WHISPER_HOME) if heard else (LRCLIB.label, LRCLIB.homepage)
    if edits:
        sig = signature(stored)
        out.pop("timed_by", None)
        out |= {"state": "found", "instrumental": False, "synced": edits["synced"], "plain": edits["plain"] or None,
                "edited": True, "edited_at": edits["edited_at"], "base": edits["base"],
                "newer": bool(sig) and sig != edits["base_sig"]}
        if edits.get("timed_by"):
            out["timed_by"] = edits["timed_by"]
    out["timed"] = bool(out.get("synced"))
    return out


def shown(conn: sqlite3.Connection, track_id: int) -> dict[str, Any]:
    """The track's lyrics as the page shows them: the stored answer, with the person's edits on top."""
    from . import db

    return describe(db.get_lyrics(conn, track_id), db.get_lyrics_edits(conn, track_id))


# ---- the person's own edits ----

MAX_LINES, MAX_LINE, MAX_PLAIN = 1000, 300, 30000  # lines, characters in a line, characters of plain text
MAX_T = 6 * 3600.0


def signature(stored: dict | None) -> str:
    """A short fingerprint of a found answer's words and times ("" for anything else), so edits can tell when
    LRCLIB or Whisper has said something new since they were made."""
    if not stored or stored.get("status") != "found" or stored.get("instrumental"):
        return ""
    lines = [[x.get("t"), x.get("line")] for x in stored.get("synced") or []]
    blob = json.dumps([stored.get("provider"), lines, stored.get("plain") or ""], separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _line(text: Any) -> str:
    if not isinstance(text, str):
        raise ValueError("each line's text must be a string")
    text = " ".join(text.split())  # one line: no newlines or runs of spaces
    if len(text) > MAX_LINE:
        raise ValueError(f"a line can be at most {MAX_LINE} characters")
    return text


def clean_edit(body: Any) -> dict[str, Any]:
    """The person's lines from the page, checked: {"synced": [{t, line}]} (t in seconds, or null for a line not
    timed yet) or {"plain": text}. Timed lines come back in time order (in_time_order); lines with no time get
    `guess: true`, like lines listening couldn't place, and stay right after the line they followed. Raises ValueError."""
    if not isinstance(body, dict):
        raise ValueError("send {synced: [...]} or {plain: text}")
    if body.get("synced") is not None:
        rows = body["synced"]
        if not isinstance(rows, list) or len(rows) > MAX_LINES:
            raise ValueError(f"synced must be a list of at most {MAX_LINES} lines")
        lines = []
        for x in rows:
            if not isinstance(x, dict):
                raise ValueError("each line must be {t, line}")
            t = x.get("t")
            if t is not None:
                if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or not 0 <= t <= MAX_T:
                    raise ValueError("a line's time must be seconds from the start of the track")
                t = round(float(t), 2)
            lines.append({"t": t, "line": _line(x.get("line", ""))})
        if not any(x["line"] for x in lines):
            raise ValueError("there are no lines to keep; undo the edits instead")
        if all(x["t"] is None for x in lines):
            return {"synced": [], "plain": "\n".join(x["line"] for x in lines).strip("\n")}
        lines = in_time_order(lines)
        while not lines[-1]["line"]:
            lines.pop()  # trailing blank lines say nothing
        return {"synced": lines, "plain": "\n".join(x["line"] for x in lines if x["line"])}
    plain = body.get("plain")
    if not isinstance(plain, str):
        raise ValueError("send {synced: [...]} or {plain: text}")
    if len(plain) > MAX_PLAIN:
        raise ValueError(f"lyrics can be at most {MAX_PLAIN} characters")
    rows = [" ".join(x.split()) for x in plain.strip().splitlines()]
    if not any(rows):
        raise ValueError("there are no lines to keep; undo the edits instead")
    if len(rows) > MAX_LINES:
        raise ValueError(f"lyrics can have at most {MAX_LINES} lines")
    return {"synced": [], "plain": "\n".join(rows)}


def in_time_order(lines: list[dict]) -> list[dict]:
    """Timed lines sorted by time, each line without a time (marked `guess: true`) kept right after the line it
    followed, and any before the first timed line kept first. So the timed lines always run in time order, which
    the page's playback relies on."""
    head: list[dict] = []
    groups: list[list[dict]] = []
    for x in lines:
        if x["t"] is None:
            (groups[-1] if groups else head).append({"t": None, "line": x["line"], "guess": True})
        else:
            groups.append([x])
    groups.sort(key=lambda g: g[0]["t"])  # stable: equal times keep their order
    return head + [x for g in groups for x in g]


def edit_lines(conn: sqlite3.Connection, track_id: int, body: Any) -> dict[str, Any]:
    """Store the person's lines (clean_edit) as the track's edited lyrics and return what the page shows."""
    from . import db

    lines = clean_edit(body)
    stored = db.get_lyrics(conn, track_id)
    base = stored.get("provider") if signature(stored) else "none"
    db.save_lyrics_edits(conn, track_id, lines | {"base": base, "base_sig": signature(stored)})
    return shown(conn, track_id)


def keep_edits(conn: sqlite3.Connection, track_id: int) -> dict[str, Any]:
    """The person keeps their edits over a newer LRCLIB or Whisper answer: stop saying it's newer."""
    from . import db

    edits = db.get_lyrics_edits(conn, track_id)
    if edits:
        db.save_lyrics_edits(conn, track_id, edits | {"base_sig": signature(db.get_lyrics(conn, track_id))})
    return shown(conn, track_id)


def undo_edits(conn: sqlite3.Connection, track_id: int) -> dict[str, Any]:
    """Drop the person's edits: the latest LRCLIB or Whisper answer shows again."""
    from . import db

    db.delete_lyrics_edits(conn, track_id)
    return shown(conn, track_id)
