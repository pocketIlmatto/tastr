"""The provider interface: what a metadata source gets asked and what it answers.

A provider turns a track's artist and title into the BPM and key a catalog lists for it. Providers are
looked up by name in the registry (`register`, here, so provider modules don't import the package), so adding a source is one module with a
`Provider` subclass and a `register(...)` call. See docs/lookup.md.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any

from .. import __version__

USER_AGENT = f"tastr/{__version__} (+https://github.com/pocketIlmatto/tastr)"
TIMEOUT = 10.0


@dataclass
class TrackQuery:
    """What we know about a track from its tags."""

    artist: str
    title: str
    album: str | None = None
    duration: float | None = None  # seconds
    mbid: str | None = None  # MusicBrainz recording id, when the file is tagged with one
    isrc: str | None = None


@dataclass
class LookupResult:
    """One provider's listing for a track. `status` is "found", "not_found" or "pending" (queued upstream)."""

    provider: str
    status: str
    bpm: float | None = None
    key: str | None = None  # as listed, e.g. "Am", "F-Minor", "D♭"
    camelot: str | None = None  # computed from the parsed key when the provider gives none
    tonic: int | None = None  # parsed from `key` / camelot / open key by lookup_track (0 = C)
    mode: str | None = None  # "major" | "minor"
    matched_artist: str | None = None
    matched_title: str | None = None
    url: str | None = None  # the provider's page for the match, for attribution
    extra: dict[str, Any] = field(default_factory=dict)  # provider-specific fields worth keeping (time_sig, bpm_alt...)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProviderError(Exception):
    """The provider couldn't answer: bad key, rate limit, network or an unexpected response."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # HTTP status when the provider answered with an error


class Provider:
    """Subclass, set the class attributes and implement `lookup`.

    name        registry name and the value in TASTR_LOOKUP
    label       shown in the UI next to its numbers
    homepage    linked from the UI (some providers require a visible link back)
    key_env     environment variable holding the API key (None if the provider needs none)

    Every request goes through `get_json`, which applies the guardrails in guard.py.
    """

    name: str = ""
    label: str = ""
    homepage: str = ""
    key_env: str | None = None
    # Guardrails (guard.py), each overridable with TASTR_LOOKUP_<NAME>_INTERVAL / _DAILY / _MONTHLY.
    min_interval: float = 1.0  # seconds between requests
    daily_limit: int | None = 200
    monthly_limit: int | None = None
    # Whether the provider's quota headers count our own usage. MusicBrainz's x-ratelimit-* count its global
    # load (x-ratelimit-zone: global), so pausing on them would stop lookups whenever it's busy.
    quota_headers: bool = True
    _guard = None

    @property
    def guard(self):
        from .guard import Guard

        if self._guard is None:
            self._guard = Guard(self)
        return self._guard

    def api_key(self) -> str | None:
        return os.environ.get(self.key_env) if self.key_env else None

    def configured(self) -> bool:
        return self.key_env is None or bool(self.api_key())

    def setup_hint(self) -> str:
        """Why `configured()` is False, for the warning when the provider is enabled anyway."""
        return f"{self.key_env} is not set"

    def may_store(self) -> bool:
        """Whether the provider's terms allow keeping its answers. When False, results are shown for the
        lookup that fetched them and not saved, and analysis doesn't look the provider up."""
        return True

    def lookup(self, q: TrackQuery) -> LookupResult:
        raise NotImplementedError

    def get_json(self, url: str, params: dict[str, str] | None = None, headers: dict[str, str] | None = None,
                 source: str | None = None) -> tuple[int, Any]:
        """(HTTP status, parsed JSON body) for a GET. 4xx/5xx raise ProviderError with the body's message.
        `source` names the service in error messages when it isn't the provider itself (AcousticBrainz asks
        MusicBrainz)."""
        who = source or self.label
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
        self.guard.before_request()  # raises guard.Limited instead of sending
        status, resp_headers = None, None
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                status, resp_headers = r.status, r.headers
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            status, resp_headers = e.code, e.headers
            detail = e.read()[:300].decode("utf-8", "replace").strip()
            raise ProviderError(f"{who} answered HTTP {e.code}" + (f": {detail}" if detail else ""), e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderError(f"Couldn't reach {who}: {getattr(e, 'reason', e)}") from None
        except http.client.HTTPException as e:  # e.g. IncompleteRead: the response was cut off
            raise ProviderError(f"{who}'s response was cut off ({type(e).__name__})") from None
        except json.JSONDecodeError:
            raise ProviderError(f"{who} sent a response that isn't JSON") from None
        finally:
            self.guard.after_response(status, dict(resp_headers.items()) if resp_headers else None)


def num(v: Any) -> float | None:
    """A positive number from a provider field ("128", 128, "128.0"), else None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def parse_listed_key(*texts: str | None) -> tuple[int, str] | None:
    """(tonic, mode) from the first parseable of a provider's key spellings: "Am", "F-Minor", "D♭ major",
    "10B" (Camelot) or "1m" / "1d" (Open Key)."""
    from ..engine.bench.datasets import parse_key

    for t in texts:
        if not t:
            continue
        s = str(t).strip().replace("-", " ").replace("_", " ")
        m = re.fullmatch(r"(1[0-2]|[1-9])\s*([mdMD])", s)
        if m:  # Open Key: 1d = C major = Camelot 8B
            s = f"{(int(m.group(1)) + 6) % 12 + 1}{'A' if m.group(2).lower() == 'm' else 'B'}"
        k = parse_key(s)
        if k:
            return k
    return None


def same_text(a: str | None, b: str | None) -> bool:
    """Loose match for artist / title: case, punctuation and "the" don't matter, one may contain the other."""
    def norm(s):
        s = "".join(ch for ch in (s or "").lower() if ch.isalnum() or ch == " ")
        return " ".join(w for w in s.split() if w != "the")

    x, y = norm(a), norm(b)
    return bool(x and y) and (x == y or x in y or y in x)


_ARTIST_SEP = re.compile(r"\s*(?:,|&|\band\b|\bx\b|\bvs\.?|\bfeat\.?|\bft\.?|\bfeaturing\b)\s*", re.IGNORECASE)


def artist_names(credit: str | None) -> list[str]:
    """The artists in a credit, in order: "A, B & C feat. D" -> [A, B, C, D]."""
    return [n.strip() for n in _ARTIST_SEP.split(credit or "") if n.strip()]


def same_artists(a: str | None, b: str | None) -> bool:
    """Artist credits match: same_text, or every artist in the shorter credit is in the longer one, in any
    order and however joined ("A, B & C" vs "A, C, B"; "A feat. B" vs "A")."""
    if same_text(a, b):
        return True
    xs, ys = artist_names(a), artist_names(b)
    if not xs or not ys:
        return False
    short, long = sorted((xs, ys), key=len)
    return all(any(same_text(s, t) for t in long) for s in short)


# The providers by name. Each provider module registers itself; tastr.lookup imports them all.
PROVIDERS: dict[str, Provider] = {}


def register(provider: Provider) -> Provider:
    PROVIDERS[provider.name] = provider
    return provider
