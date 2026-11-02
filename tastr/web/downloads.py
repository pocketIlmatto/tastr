"""Downloads of your own files (the Files view): readable file names, and zips streamed as they are written.

Zips use stored mode: FLAC, MP3 and AAC are already compressed, so deflating them only costs time. The zip is
written straight to the response, with no copy on disk, so the browser's progress comes from the bytes it has
read against `X-Zip-Bytes`, the files' total size.
"""

from __future__ import annotations

import io
import re
import time
import unicodedata
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Iterable, Iterator

from .. import blobs

CHUNK = 1024 * 1024
# A bulk zip holds at most this much, or this many tracks; more answers 413 before anything is sent.
ZIP_MAX_BYTES = 5 * 1024 ** 3
ZIP_MAX_TRACKS = 200

_BAD = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')


def clean_name(text: str, limit: int = 120) -> str:
    """A file name safe on Windows, macOS and Linux: no path separators or reserved characters, no leading dots."""
    text = unicodedata.normalize("NFC", text or "")
    text = _BAD.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip().lstrip(".").strip()
    return text[:limit].rstrip(" .") or "Untitled"


def base_name(title: str | None, artist: str | None, track_id: int) -> str:
    """"Artist - Title", or just the title, as the Files view and the downloads name a track."""
    title = (title or "").strip() or f"Track {track_id}"
    return clean_name(f"{artist.strip()} - {title}" if artist and artist.strip() else title)


def stem_file_name(base: str, stem: str) -> str:
    return f"{base} ({stem}).flac"


def unique(name: str, taken: set[str]) -> str:
    """`name`, or "name (2)" and so on if a zip already holds it (two tracks with the same title)."""
    m = re.fullmatch(r"(.*?)(\.[A-Za-z0-9]{1,5})?", name)
    stem, ext = m[1], m[2] or ""
    out, n = name, 2
    while out.lower() in taken:
        out = f"{stem} ({n}){ext}"
        n += 1
    taken.add(out.lower())
    return out


class _Sink:
    """Where zipfile writes. It has no tell(), so zipfile writes a stream (data descriptors after each file)."""

    def __init__(self) -> None:
        self.parts: list[bytes] = []

    def write(self, b) -> int:
        self.parts.append(bytes(b))
        return len(b)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        out, self.parts = b"".join(self.parts), []
        return out


def stream_zip(entries: Iterable[tuple[str, Path | bytes]]) -> Iterator[bytes]:
    """Yield a stored zip of (name inside the zip, file), a chunk at a time. A file not on disk is read from the
    audio bucket (tastr/blobs.py); bytes are a small file made on the spot (a playlist)."""
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for arcname, path in entries:
            made = isinstance(path, bytes)
            info = zipfile.ZipInfo(arcname, time.localtime(time.time() if made else blobs.mtime(path))[:6])
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_STORED
            with closing(io.BytesIO(path) if made else blobs.open_read(path)) as src, zf.open(info, "w", force_zip64=True) as dest:
                while chunk := src.read(CHUNK):
                    dest.write(chunk)
                    if data := sink.take():
                        yield data
            if data := sink.take():
                yield data
    if data := sink.take():
        yield data


def content_disposition(name: str) -> str:
    """An attachment header with an ASCII fallback and the real name (RFC 6266), as FileResponse writes it."""
    from urllib.parse import quote

    ascii_name = name.encode("ascii", "ignore").decode() or "download"
    ascii_name = ascii_name.replace('"', "")
    return f"attachment; filename=\"{ascii_name}\"; filename*=utf-8''{quote(name)}"
