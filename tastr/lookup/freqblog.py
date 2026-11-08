"""FreqBlog Music Metadata API (https://freqblog.com/music-api.html): BPM and key by artist and title.

Needs a key (FREQBLOG_API_KEY; the free tier allows 1,000 requests a month), sent in an X-API-Key
header. Field names follow the published OpenAPI spec (https://api.freqblog.com/openapi.json): the
match comes back as track_name / artist_name with bpm, bpm_alt, key, camelot and open_key. A track the
catalog hasn't analyzed yet comes back 202 "queued", stored as status "pending" and asked again later.

FreqBlog's terms (3 November 2026, section 6.2) let a free key used for a non-commercial project store
the values it returns, inside the project, so answers are kept like any other provider's. Two of the
terms' rules show up here: a "not found" may be cached only for a while (MISS_DAYS, or the
retry_after_days FreqBlog sends with it), and stored values must not be published as a dataset.
FREQBLOG_STORE=0 turns storing off (answers are then shown for the lookup that fetched them only);
`tastr lookup --forget freqblog` deletes what is stored. FREQBLOG_API_BASE overrides the API host.
"""

from __future__ import annotations

import os

from .base import LookupResult, Provider, ProviderError, TrackQuery, register, num, same_artists, same_text

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
    # Free tier: 1,000 requests a month. Stay under it with room for manual checks.
    min_interval = 1.0
    daily_limit = 60
    monthly_limit = 900
    # Terms: a miss may be cached "as a temporary negative result, but not a permanent one". The card says
    # Tastr "asks again after a few days".
    miss_days = 7.0
    # Terms: stored values must not be offered "as an API, feed, export or dataset", so they stay out of the
    # library snapshot the hosted search downloads.
    in_downloads = False

    def base(self) -> str:
        return os.environ.get("FREQBLOG_API_BASE", DEFAULT_BASE).rstrip("/")

    def may_store(self) -> bool:
        return os.environ.get("FREQBLOG_STORE", "1").strip().lower() not in ("0", "false", "no", "off")

    def _fetch(self, q: TrackQuery) -> tuple[int | None, dict]:
        """(HTTP status, body); status None when the API has no such track (a 404)."""
        try:
            status, body = self.get_json(f"{self.base()}/lookup", {"track": q.title, "artist": q.artist},
                                         headers={"X-API-Key": self.api_key() or ""})
        except ProviderError as e:
            if e.status == 404:
                return None, e.body if isinstance(e.body, dict) else {}
            raise
        return status, body if isinstance(body, dict) else {}

    def _match(self, track: dict, q: TrackQuery) -> LookupResult:
        title, artist = _text(track, "track_name", "title", "track"), _text(track, "artist_name", "artist")
        if num(track.get("bpm")) is None and not track.get("key"):
            return LookupResult(self.name, "not_found")
        # The lookup returns its closest match; don't store a different song as this one.
        if (title and not same_text(title, q.title)) or (artist and not same_artists(artist, q.artist)):
            return LookupResult(self.name, "not_found", extra={"closest": f"{artist or '?'} – {title or '?'}"})
        return LookupResult(
            self.name, "found", bpm=num(track.get("bpm")), key=track.get("key") or None,
            camelot=track.get("camelot") or None, matched_artist=artist, matched_title=title,
            url=track.get("url") or self.homepage,
            extra={k: track[k] for k in ("bpm_alt", "open_key", "id") if track.get(k) is not None})

    def lookup(self, q: TrackQuery) -> LookupResult:
        status, body = self._fetch(q)
        if status is None:
            retry = num(body.get("retry_after_days"))
            return LookupResult(self.name, "not_found", extra={"retry_after_days": retry} if retry else {})
        if status == 202:
            return LookupResult(self.name, "pending", extra={"message": body.get("message") or body.get("status")})
        track = body.get("track") if isinstance(body.get("track"), dict) else body  # flat or wrapped
        return self._match(track, q)


register(FreqBlog())
