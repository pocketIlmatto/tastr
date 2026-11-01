"""Track and playlist links through yt-dlp (YouTube, SoundCloud, Bandcamp, Mixcloud and many more).

Needs the optional extra: pip install 'tastr[links]'.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
from dataclasses import asdict
from pathlib import Path

from ..engine.card import Source
from .base import IngestError, register_url_resolver
from .streaming import streaming_service

logger = logging.getLogger(__name__)

MAX_PLAYLIST = 200
INDEX_NAME = "links.json"  # url -> sources, so a cached link never touches the network again
PARTIAL_SUFFIXES = {".part", ".ytdl", ".temp"}


def _import_yt_dlp():
    try:
        import yt_dlp
    except ImportError as e:
        raise IngestError("Links need yt-dlp. Install it with: pip install 'tastr[links]'") from e
    return yt_dlp


def _safe(text: str) -> str:
    return re.sub(r"[^\w.-]", "_", text)


def _find_download(cache_dir: Path, key: str) -> Path | None:
    for p in sorted(cache_dir.glob(f"{key}.*")):  # key is already _safe(), so no glob metacharacters
        if p.is_file() and p.suffix.lower() not in PARTIAL_SUFFIXES:
            return p
    return None


def _download(yt_dlp, url: str, cache_dir: Path, key: str) -> None:
    template = os.path.join(str(cache_dir).replace("%", "%%"), f"{key}.%(ext)s")
    opts = {"format": "bestaudio/best", "outtmpl": template, "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)


def _load_index(cache_dir: Path) -> dict:
    try:
        return json.loads((cache_dir / INDEX_NAME).read_text())
    except (OSError, ValueError):
        return {}


def _save_index(cache_dir: Path, index: dict) -> None:
    (cache_dir / INDEX_NAME).write_text(json.dumps(index, indent=2))


def _read_info(yt_dlp, url: str) -> tuple[dict, list, bool]:
    """(the link's info, its entries (just the info for a single track), whether it is a playlist)."""
    info_opts = {"quiet": True, "no_warnings": True, "playlistend": MAX_PLAYLIST}
    try:
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            is_playlist = info.get("entries") is not None
            items = list(itertools.islice(info["entries"], MAX_PLAYLIST)) if is_playlist else [info]
    except Exception as e:
        raise IngestError(f"Could not read {url}: {e}") from e
    return info, items, is_playlist


def _entry_source(yt_dlp, entry: dict, info: dict, url: str, cache_dir: Path) -> Source:
    """Download one entry (unless it is cached already) and describe it. Raises on any failure."""
    key = _safe(f"{entry.get('extractor') or info.get('extractor') or 'link'}-{entry['id']}")
    track_url = entry.get("webpage_url") or entry.get("url") or url
    path = _find_download(cache_dir, key)
    if path is None:
        _download(yt_dlp, track_url, cache_dir, key)
        path = _find_download(cache_dir, key)
    if path is None:
        raise IngestError(f"yt-dlp finished but produced no audio file for {track_url}")
    return Source(kind="url", uri=track_url, local_path=str(path), title=entry.get("title"),
                  artist=entry.get("artist") or entry.get("creator") or entry.get("uploader"), album=entry.get("album"))


class YtDlpResolver:
    name = "yt-dlp"

    def handles(self, url: str) -> bool:
        return url.startswith(("http://", "https://")) and streaming_service(url) is None

    def resolve(self, url: str, cache_dir: Path) -> list[Source]:
        cache_dir = Path(cache_dir)
        cached = _load_index(cache_dir).get(url)
        if cached and all(Path(s["local_path"]).exists() for s in cached):
            return [Source(**s) for s in cached]

        yt_dlp = _import_yt_dlp()
        info, items, is_playlist = _read_info(yt_dlp, url)
        sources: list[Source] = []
        for entry in filter(None, items):
            try:
                sources.append(_entry_source(yt_dlp, entry, info, url, cache_dir))
            except Exception as e:
                track_url = entry.get("webpage_url") or entry.get("url") or url
                if not is_playlist:
                    raise IngestError(f"Could not download {track_url}: {e}") from e
                logger.warning("Skipping playlist entry %s: %s", track_url, e)

        if not sources:
            raise IngestError(f"No tracks could be downloaded from {url}")
        index = _load_index(cache_dir)
        index[url] = [asdict(s) for s in sources]
        _save_index(cache_dir, index)
        return sources


register_url_resolver(YtDlpResolver())
