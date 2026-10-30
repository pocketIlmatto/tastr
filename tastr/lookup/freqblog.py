"""FreqBlog Music Metadata API (https://freqblog.com/music-api.html): BPM and key by artist and title.

Needs a key (FREQBLOG_API_KEY; the free tier allows 1,000 requests a month), sent in an X-API-Key
header. Field names follow the published OpenAPI spec (https://api.freqblog.com/openapi.json): the
match comes back as track_name / artist_name with bpm, bpm_alt, key, camelot and open_key. A track the
catalog hasn't analyzed yet comes back 202 "queued", stored as status "pending" and asked again later.

FreqBlog's terms allow caching results only on paid plans, so answers are kept in the database only
when FREQBLOG_PLAN=paid; otherwise they are shown for the lookup that fetched them and not saved.
FREQBLOG_API_BASE overrides the API host.
"""

from __future__ import annotations

import os

from . import register
from .base import LookupResult, Provider, ProviderError, TrackQuery, num, same_text

DEFAULT_BASE = "https://api.freqblog.com"


def _text(d: dict, *names: str) -> str | None:
    for n in names:
        v = d.get(n)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict) and isinstance(v.get("name"), str):  # {"artist": {"name": ...}}
            return v["name"]
    return None


class FreqBlog(Provider):
    name = "freqblog"
    label = "FreqBlog"
    homepage = "https://freqblog.com"
    key_env = "FREQBLOG_API_KEY"

    def base(self) -> str:
        return os.environ.get("FREQBLOG_API_BASE", DEFAULT_BASE).rstrip("/")

    def may_store(self) -> bool:
        return os.environ.get("FREQBLOG_PLAN", "").strip().lower() == "paid"

    def lookup(self, q: TrackQuery) -> LookupResult:
        try:
            status, body = self.get_json(f"{self.base()}/lookup", {"track": q.title, "artist": q.artist},
                                         headers={"X-API-Key": self.api_key() or ""})
        except ProviderError as e:
            if e.status == 404:
                return LookupResult(self.name, "not_found")
            raise
        body = body if isinstance(body, dict) else {}
        if status == 202:
            return LookupResult(self.name, "pending", extra={"message": body.get("message") or body.get("status")})
        track = body.get("track") if isinstance(body.get("track"), dict) else body  # flat or wrapped
        title, artist = _text(track, "track_name", "title", "track"), _text(track, "artist_name", "artist")
        if num(track.get("bpm")) is None and not track.get("key"):
            return LookupResult(self.name, "not_found")
        # The lookup returns its closest match; don't store a different song as this one.
        if (title and not same_text(title, q.title)) or (artist and not same_text(artist, q.artist)):
            return LookupResult(self.name, "not_found", extra={"closest": f"{artist or '?'} – {title or '?'}"})
        return LookupResult(
            self.name, "found", bpm=num(track.get("bpm")), key=track.get("key") or None,
            camelot=track.get("camelot") or None, matched_artist=artist, matched_title=title,
            url=track.get("url") or self.homepage,
            extra={k: track[k] for k in ("bpm_alt", "open_key", "id") if track.get(k) is not None})


register(FreqBlog())
