"""GetSongBPM (https://getsongbpm.com/api): tempo, time signature and key by artist and title.

Free with a registered key (GETSONGBPM_API_KEY), sent in the X-API-KEY header. The terms require a
visible link back to getsongbpm.com, which the card shows next to its numbers. Search hits name the
song as song_title / song_id in some responses and title / id in others, so both are read. The API base
moved hosts once, so GETSONGBPM_API_BASE overrides it.
"""

from __future__ import annotations

import os

from .base import LookupResult, Provider, ProviderError, TrackQuery, register, num, same_artists, same_text

DEFAULT_BASE = "https://api.getsong.co"


def _title(h: dict) -> str | None:
    return h.get("song_title") or h.get("title")


def _artist(h: dict) -> str | None:
    a = h.get("artist")
    return (a.get("name") if isinstance(a, dict) else a) or h.get("artist_name")


class GetSongBPM(Provider):
    name = "getsongbpm"
    label = "GetSongBPM"
    homepage = "https://getsongbpm.com"
    key_env = "GETSONGBPM_API_KEY"
    # Its limit is 3,000 requests an hour (over that the key is blocked for an hour); a search can take
    # two requests (search, then the song). These stay far below it.
    min_interval = 1.0
    daily_limit = 1000

    def base(self) -> str:
        return os.environ.get("GETSONGBPM_API_BASE", DEFAULT_BASE).rstrip("/")

    def _search(self, q: TrackQuery, auth: dict[str, str]) -> list | None:
        """The search hits; None when it found nothing."""
        _, body = self.get_json(f"{self.base()}/search/", {"type": "both", "lookup": f"song:{q.title} artist:{q.artist}"}, auth)
        hits = (body or {}).get("search") if isinstance(body, dict) else None
        if isinstance(hits, dict) and hits.get("error"):  # {"search": {"error": "no result"}}
            if "no result" in str(hits["error"]).lower():
                return None
            raise ProviderError(f"{self.label}: {hits['error']}")
        if not isinstance(hits, list):
            raise ProviderError(f"{self.label} sent an unexpected search response")
        return hits

    def _full(self, song: dict, auth: dict[str, str]) -> dict:
        """Some search hits are thin; the song endpoint has the full record."""
        sid = song.get("song_id") or song.get("id")
        if num(song.get("tempo")) is not None or song.get("key_of") or not sid:
            return song
        _, full = self.get_json(f"{self.base()}/song/", {"id": str(sid)}, auth)
        return (full or {}).get("song") or song

    def lookup(self, q: TrackQuery) -> LookupResult:
        auth = {"X-API-KEY": self.api_key() or ""}
        hits = self._search(q, auth) or []
        song = next((h for h in hits if same_artists(_artist(h), q.artist) and same_text(_title(h), q.title)), None)
        if song is None:
            return LookupResult(self.name, "not_found")
        song = self._full(song, auth)
        return LookupResult(
            self.name, "found", bpm=num(song.get("tempo")), key=song.get("key_of") or None,
            matched_artist=_artist(song), matched_title=_title(song),
            url=song.get("song_uri") or song.get("uri") or self.homepage,
            extra={k: song[k] for k in ("time_sig", "open_key", "song_id", "id") if song.get(k)})


register(GetSongBPM())
