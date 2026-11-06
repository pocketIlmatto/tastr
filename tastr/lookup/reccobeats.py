"""ReccoBeats (https://reccobeats.com): the BPM its catalog lists for a track, found by artist and title.

Free, with no key or account. Its terms (https://reccobeats.com/docs/documentation/terms-of-service) allow
personal and commercial use and don't limit keeping answers, but it may block clients that send too much,
so the guard keeps to one request a second. Its catalog mirrors Spotify's track metadata; the audio
features are its own estimates.

There is no track search, so a lookup is: an artist search for the first-listed artist, that artist's
tracks a page at a time until one matches the title (the same version, so a remix isn't taken for the
original; of several matches, the closest in length), then the audio features of that track. About five requests a track; large discographies cost more, up to MAX_PAGES pages.

Only the tempo is used. In a test on 38 tracks (2026-11) its key matched Rekordbox's on 14, against
30 for Tastr's, so a listed key would mostly be noise. Energy, danceability and valence are kept in
`extra`. RECCOBEATS_API_BASE overrides the API host.
"""

from __future__ import annotations

import os
import re
import unicodedata

from .base import LookupResult, Provider, ProviderError, TrackQuery, artist_names, num, register, same_text

DEFAULT_BASE = "https://api.reccobeats.com/v1"
PAGE_SIZE = 50
MAX_PAGES = 10  # 500 tracks per artist; the rest of a bigger discography isn't searched
FEATURES = ("energy", "danceability", "valence", "loudness", "acousticness", "instrumentalness")
# Versions that can change the tempo or the track: when either title has one, the versions must match word
# for word ("Joel Corry Remix" isn't the original or another remix). Extended, radio, original and
# instrumental cuts keep the tempo, so they are ignored.
STRONG = {"remix", "rmx", "live", "acoustic", "rework", "vip", "bootleg", "flip", "cover", "dub", "mixed", "refix",
          "reprise", "slowed", "sped", "nightcore"}
WEAK = {"extended", "radio", "original", "edit", "mix", "version", "instrumental", "remaster", "remastered",
        "club", "single", "album", "explicit", "clean"}
_FEAT = re.compile(r"[\(\[]\s*(?:feat|ft|featuring|with)\b[^\)\]]*[\)\]]"  # (feat. X)
                   r"|\s+(?:feat|ft|featuring)\.?\s.*?(?=\s+-\s|\s*[\(\[]|$)", re.IGNORECASE)  # feat. X, up to " - " or "("


def _words(text: str) -> list[str]:
    """Lower-case words with accents dropped, any script: "Campeón" -> ["campeon"], "夜に駆ける" stays."""
    t = "".join(ch for ch in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(ch))
    return re.findall(r"[^\W_]+", t.casefold())


def _bare(title: str) -> str:
    """The title without featured artists, brackets or a " - Remix" tail: "Say It (feat. X) - Y Remix" -> "say it"."""
    t = _FEAT.sub("", title or "")
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", "", t)
    t = re.sub(r"\s+-\s+.*$", "", t)
    return " ".join(_words(t))


def _version(title: str) -> set[str]:
    """The version words: what the title adds to its bare form, minus featured artists and same-tempo cuts."""
    bare = set(_bare(title).split())
    return {w for w in _words(_FEAT.sub("", title or "")) if w not in bare and w not in WEAK}


def same_track(ours: str, theirs: str | None) -> bool:
    """Their title is our track: the same bare title and, when either names a remix, live take or other
    re-made version, the same version words."""
    if not theirs or not _bare(ours) or _bare(theirs) != _bare(ours):
        return False
    a, b = _version(ours), _version(theirs)
    return a == b or not (a | b) & STRONG


class ReccoBeats(Provider):
    name = "reccobeats"
    label = "ReccoBeats"
    homepage = "https://reccobeats.com"
    key_env = None
    # No published limit; it blocks IPs that send "excessive traffic". ~5 requests a track, so 1,000 a day is
    # about 200 tracks.
    min_interval = 1.0
    daily_limit = 1000

    def base(self) -> str:
        return os.environ.get("RECCOBEATS_API_BASE", DEFAULT_BASE).rstrip("/")

    def _get(self, path: str, params: dict[str, str]) -> dict:
        _, body = self.get_json(f"{self.base()}{path}", params)
        if not isinstance(body, dict):
            raise ProviderError(f"{self.label} sent an unexpected response")
        return body

    def _artist_ids(self, name: str) -> list[str]:
        body = self._get("/artist/search", {"searchText": name, "page": "0", "size": "10"})
        return [a["id"] for a in body.get("content") or [] if isinstance(a, dict) and a.get("id")
                and same_text(a.get("name"), name) and _bare(a.get("name") or "") == _bare(name)]

    def _find(self, artist_id: str, q: TrackQuery) -> dict | None:
        for page in range(MAX_PAGES):
            body = self._get(f"/artist/{artist_id}/track", {"page": str(page), "size": str(PAGE_SIZE)})
            hits = [t for t in body.get("content") or []
                    if isinstance(t, dict) and t.get("id") and same_track(q.title, t.get("trackTitle"))]
            if hits:  # the closest length first, when we know ours
                return min(hits, key=lambda t: abs((num(t.get("durationMs")) or 0) / 1000 - q.duration) if q.duration else 0)
            if page >= (body.get("totalPages") or 1) - 1:
                return None
        return None

    def lookup(self, q: TrackQuery) -> LookupResult:
        names = artist_names(q.artist)[:1] or [q.artist]
        track = None
        for aid in self._artist_ids(names[0])[:2]:  # a name can belong to more than one artist
            track = self._find(aid, q)
            if track:
                break
        if track is None:
            return LookupResult(self.name, "not_found")
        body = self._get("/audio-features", {"ids": track["id"]})
        feats = next((f for f in body.get("content") or [] if isinstance(f, dict)), None)
        if not feats or num(feats.get("tempo")) is None:
            return LookupResult(self.name, "not_found", extra={"track_id": track["id"], "reason": "no audio features"})
        artists = ", ".join(a.get("name", "") for a in track.get("artists") or [] if isinstance(a, dict)) or None
        return LookupResult(
            self.name, "found", bpm=round(float(feats["tempo"]), 2), matched_artist=artists,
            matched_title=track.get("trackTitle"), url=track.get("href") or self.homepage,
            extra={"track_id": track["id"], "isrc": track.get("isrc"),
                   **{k: feats[k] for k in FEATURES if isinstance(feats.get(k), (int, float))}})


register(ReccoBeats())
