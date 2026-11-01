"""Spotify and Apple Music links: audio can't be downloaded from these, so say so clearly."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from .base import IngestError, register_url_resolver

STREAMING_SERVICES = {"open.spotify.com": "Spotify", "music.apple.com": "Apple Music"}


def streaming_service(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for domain, service in STREAMING_SERVICES.items():
        if host == domain or host.endswith("." + domain):
            return service
    return None


class StreamingLinkResolver:
    name = "streaming"

    def handles(self, url: str) -> bool:
        return streaming_service(url) is not None

    def resolve(self, url: str, cache_dir: Path) -> list:
        service = streaming_service(url)
        raise IngestError(
            f"{service} links can't be analyzed directly: {service} doesn't let us download audio. "
            "Drop in the audio file itself, or use a link from a site that allows downloads "
            "(YouTube, SoundCloud, Bandcamp, ...)."
        )


register_url_resolver(StreamingLinkResolver())
