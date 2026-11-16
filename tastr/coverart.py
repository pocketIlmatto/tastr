"""Album art fetched from the Cover Art Archive for a track whose file has no embedded cover.

Only a mash-up share preview asks for it (`/api/tracks/{id}/cover`, routes/media.py), and only for the people whose
links may show album art (admins on a shared server, anyone on their own machine). The images belong to their
copyright owners, usually the labels; the archive grants no licence for them, so they go no further than the
embedded covers do: into the preview picture of an admin's link, never into the repository or other people's links.

How a cover is found:
1. The track's MusicBrainz recordings: the one in its tags (or under its ISRC), the ones a stored AcousticBrainz
   lookup matched, else a MusicBrainz search by artist, title and length (the AcousticBrainz provider's matching).
2. The recordings' releases, official ones first, then the oldest; one recording lookup when the search didn't
   list them.
3. The release group's front cover (`/release-group/<id>/front-500`, 500 px), else the release's own. The archive
   redirects to archive.org; a 404 means it has none.

Every MusicBrainz request goes through the AcousticBrainz provider's guardrails (one a second, its daily budget,
backoff on 503), so cover lookups and catalog lookups share one MusicBrainz rate.

Off unless outside requests are allowed: TASTR_COVER_ART=1 turns it on, 0 off; unset, it follows TASTR_LOOKUP
(on when that names any provider), since nothing should leave the machine without one of them.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from . import http
from .covers import MAX_COVER_BYTES, FetchFailed, image_type

CAA_BASE = "https://coverartarchive.org"
SIZE = 500  # the archive's thumbnails are 250, 500 and 1200 px; the preview draws each cover 340 px wide
TIMEOUT = 15.0
MAX_RECORDINGS = 3  # recordings looked at
MAX_TRIES = 4  # cover requests per track


class Unavailable(FetchFailed):
    """Couldn't ask (MusicBrainz busy, a budget used up, the network): try again another time."""


def enabled() -> bool:
    v = os.environ.get("TASTR_COVER_ART", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    return bool(os.environ.get("TASTR_LOOKUP", "").strip())


def _musicbrainz():
    from .lookup import PROVIDERS

    return PROVIDERS["acousticbrainz"]


def _mb_base() -> str:
    from .lookup.acousticbrainz import MUSICBRAINZ_BASE

    return os.environ.get("MUSICBRAINZ_API_BASE", MUSICBRAINZ_BASE).rstrip("/")


def recordings(q, stored: list[dict] | None = None) -> list[dict]:
    """The track's MusicBrainz recordings ({mbid, releases?}), best first, at most MAX_RECORDINGS. `q`: its
    TrackQuery (lookup.query_for); `stored`: its stored lookups (db.get_lookups)."""
    mb = _musicbrainz()
    out: list[dict] = []

    def add(mbid: str | None, releases: list | None = None) -> None:
        if mbid and all(r["mbid"] != mbid for r in out):
            out.append({"mbid": mbid} | ({"releases": releases} if releases is not None else {}))

    for h in mb.tagged(q):
        add(h["mbid"])
    for r in stored or []:
        if r.get("provider") == "acousticbrainz":
            extra = r.get("extra") or {}
            for m in [extra.get("mbid")] + list(extra.get("mbids") or []):
                add(m)
    if not out:
        for h in mb.candidates(q):  # the search lists each match's releases, so no lookup is needed for them
            add(h["mbid"], h.get("releases"))
    return out[:MAX_RECORDINGS]


def releases_of(mbid: str) -> list[dict]:
    """A recording's releases, each with its release group, from one MusicBrainz lookup."""
    from .lookup import ProviderError

    mb, url = _musicbrainz(), f"{_mb_base()}/recording/{mbid}"
    try:
        _, body = mb.get_json(url, {"inc": "releases+release-groups", "fmt": "json"}, source="MusicBrainz")
    except ProviderError as e:
        if e.status == 404:  # merged away or deleted
            return []
        if e.status != 400:
            raise
        # Without the groups: each release's own cover is still asked.
        _, body = mb.get_json(url, {"inc": "releases", "fmt": "json"}, source="MusicBrainz")
    rels = (body or {}).get("releases") if isinstance(body, dict) else None
    return rels if isinstance(rels, list) else []


def _order(releases: list[dict], album: str | None = None) -> list[dict]:
    """Releases best first: the album the tags name, official ones, then the oldest (the original release)."""
    from .lookup.base import same_text

    def key(r: dict):
        return (not (album and same_text(r.get("title"), album)), r.get("status") not in (None, "Official"),
                not r.get("date"), r.get("date") or "")
    return sorted((r for r in releases if isinstance(r, dict) and r.get("id")), key=key)


def targets(recs: list[dict], album: str | None = None) -> Iterator[str]:
    """The archive paths to ask, best first: each release's group, then the release itself. Lazy, so a recording's
    releases are looked up only when the ones before it had no cover."""
    seen: set[str] = set()
    for rec in recs:
        rels = rec.get("releases")
        if rels is None:
            rels = releases_of(rec["mbid"])
        for r in _order(rels, album):
            rg = (r.get("release-group") or {}).get("id")
            for path in ([f"release-group/{rg}"] if rg else []) + [f"release/{r['id']}"]:
                if path not in seen:
                    seen.add(path)
                    yield path


def front(path: str) -> tuple[bytes, str] | None:
    """The front cover at the archive's `path` ("release-group/<id>" or "release/<id>"), None when it has none."""
    from .lookup.base import USER_AGENT

    url = f"{os.environ.get('COVER_ART_ARCHIVE_BASE', CAA_BASE).rstrip('/')}/{path}/front-{SIZE}"
    try:
        with http.request("GET", url, {"User-Agent": USER_AGENT}, timeout=TIMEOUT) as r:
            if r.status in (400, 404):
                return None
            if r.status != 200:
                raise Unavailable(f"the Cover Art Archive answered HTTP {r.status}")
            data = r.read(MAX_COVER_BYTES + 1)
    except OSError as e:
        raise Unavailable(f"couldn't reach the Cover Art Archive: {e}") from None
    kind = image_type(data)
    return (data, kind) if kind and len(data) <= MAX_COVER_BYTES else None


def fetch(q, stored: list[dict] | None = None) -> tuple[bytes, str] | None:
    """The track's front cover from the Cover Art Archive, or None when MusicBrainz or the archive has none.
    Raises Unavailable when it couldn't ask, so the caller doesn't remember a miss."""
    from .lookup import Limited, ProviderError

    if q is None:
        return None
    try:
        for n, path in enumerate(targets(recordings(q, stored), q.album)):
            if n >= MAX_TRIES:
                break
            got = front(path)
            if got:
                return got
    except (Limited, ProviderError) as e:
        raise Unavailable(str(e)) from None
    return None
