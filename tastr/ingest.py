"""Turning user input (files, folders, links) into local audio files.

Plug-in point for links: register a UrlResolver (tastr/resolvers/base.py).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .engine.audio import AUDIO_EXTENSIONS, load_audio  # noqa: F401  (re-exported)
from .engine.card import Source
from .filemeta import read_file_meta
from .resolvers.base import URL_RESOLVERS, IngestError, UrlResolver, register_url_resolver  # noqa: F401  (re-exported)


def is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def resolve_inputs(inputs: list[str], cache_dir: Path | None = None) -> list[Source]:
    """Expand files, folders (recursively) and URLs into Sources ready to analyze."""
    out: list[Source] = []
    for item in inputs:
        if is_url(item):
            out.extend(_resolve_url(item, cache_dir or default_cache_dir()))
            continue
        p = Path(item).expanduser()
        if p.is_dir():
            files = sorted(f for f in p.rglob("*") if f.suffix.lower() in AUDIO_EXTENSIONS and f.is_file())
            out.extend(_file_source(f) for f in files)
        elif p.is_file():
            out.append(_file_source(p))
        else:
            raise IngestError(f"No such file or folder: {item}")
    return out


def _file_source(p: Path, fileobj=None) -> Source:
    p = p.resolve()
    tags, info = read_file_meta(p, fileobj)
    return Source(
        kind="file", uri=str(p), local_path=str(p),
        title=tags.get("title") or p.stem, artist=tags.get("artist"), album=tags.get("album"),
        file_tags=tags, audio_info=info,
    )


def bucket_source(path: str | os.PathLike) -> Source:
    """A stored upload as a Source, read from the audio bucket without bringing the file down (tastr/blobs.py):
    its tags from the header, a few blocks at a time, and its hash as it streams past. For the Worker, whose disk
    is memory: a whole copy of a large upload ran it out (T93). Modal reads the bucket's copy to analyze it."""
    from . import blobs

    p = Path(path)
    try:
        with blobs.RemoteFile(p) as f:
            source = _file_source(p, f)
    except FileNotFoundError:
        raise IngestError(f"No such file or folder: {path}") from None
    source.sha256 = blobs.sha256(p)
    return source


def _resolve_url(url: str, cache_dir: Path) -> list[Source]:
    for r in URL_RESOLVERS:  # importing resolvers.base above ran tastr/resolvers/__init__, which registers them
        if r.handles(url):
            return r.resolve(url, cache_dir)
    raise IngestError(f"No resolver for links like {url} yet (track/playlist links are not supported yet).")


def default_cache_dir() -> Path:
    d = Path(os.environ.get("TASTR_HOME", Path.home() / ".tastr")) / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def file_sha256(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
