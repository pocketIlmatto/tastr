"""Album art: the cover picture embedded in an uploaded file, and the preview pictures of mash-up share links.

Only a mash-up's short link uses them (tastr/web/routes/strudel.py), and only when an admin makes it: the page draws
the 1200×630 picture from both covers (static/app/share-art.js) and sends it, so the server needs no image library.

- `cover()` reads a track's embedded cover (MP3 APIC, M4A covr, FLAC and Ogg pictures) once and keeps it as
  `covers/<track id>` (an empty object when the file has none, so it isn't read again). Reading it brings the audio
  down from the bucket, so this happens only when a share asks for it, not at every upload.
- `put_art()` / `art()` keep a share's picture as `share-art/<share id>.png`.

Both live in the audio bucket when there is one (TASTR_AUDIO_BUCKET, tastr/blobs.py), else under TASTR_HOME.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from . import blobs

log = logging.getLogger(__name__)

MAX_COVER_BYTES = 4 << 20  # larger embedded pictures are skipped (a phone-photo-sized cover is about 1 MB)
MAX_ART_BYTES = 2 << 20  # a share's 1200×630 PNG is about 100-300 KB
_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"), (b"GIF8", "image/gif"))
FRONT_COVER = 3  # the ID3/FLAC picture type for the front cover


def image_type(data: bytes) -> str | None:
    """The picture's media type from its first bytes (JPEG, PNG, GIF or WebP), else None."""
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _pick(pictures: list[tuple[int, bytes]]) -> bytes | None:
    """The front cover if there is one, else the first picture; only ones we can show and not too big."""
    ok = [(t, d) for t, d in pictures if d and len(d) <= MAX_COVER_BYTES and image_type(d)]
    ok.sort(key=lambda p: p[0] != FRONT_COVER)
    return ok[0][1] if ok else None


def read_cover(path: str | Path) -> tuple[bytes, str] | None:
    """(picture, media type) embedded in the audio file at `path`, or None. Best effort, like filemeta."""
    try:
        import mutagen
        from mutagen.flac import Picture
    except ImportError:
        return None
    try:
        f = mutagen.File(str(path))
    except Exception as e:  # mutagen raises many types for damaged or unknown files
        log.debug("can't read the cover of %s: %s", Path(path).name, e)
        return None
    if f is None:
        return None
    pics: list[tuple[int, bytes]] = []
    tags = f.tags
    if tags is not None and hasattr(tags, "getall"):  # ID3 (MP3, WAV, AIFF)
        pics += [(int(p.type), bytes(p.data)) for p in tags.getall("APIC")]
    elif tags is not None and "covr" in tags:  # MP4/M4A: no picture types, the first is the cover
        pics += [(FRONT_COVER, bytes(c)) for c in tags["covr"]]
    pics += [(int(p.type), bytes(p.data)) for p in getattr(f, "pictures", None) or []]  # FLAC
    if tags is not None and hasattr(tags, "get") and not hasattr(tags, "getall"):  # Vorbis comments (Ogg, Opus)
        for b64 in tags.get("metadata_block_picture", []) or []:
            try:
                p = Picture(base64.b64decode(b64))
                pics.append((int(p.type), bytes(p.data)))
            except Exception as e:  # a damaged block: skip it
                log.debug("bad picture block in %s: %s", Path(path).name, e)
    data = _pick(pics)
    return (data, image_type(data)) if data else None


# ---- storage: the bucket when there is one, else TASTR_HOME ----

def _put(key: str, body: bytes, kind: str) -> None:
    if blobs.enabled():
        blobs.client().put(key, body, kind)
        return
    p = blobs.home() / key
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)


def _get(key: str) -> bytes | None:
    if blobs.enabled():
        if blobs.client().head(key) is None:
            return None
        with blobs.client().get(key) as resp:
            return resp.read()
    p = blobs.home() / key
    return p.read_bytes() if p.is_file() else None


def _has(key: str) -> bool:
    if blobs.enabled():
        return blobs.client().head(key) is not None
    return (blobs.home() / key).is_file()


def _delete(key: str) -> None:
    if blobs.enabled():
        blobs.client().delete(key)
    else:
        (blobs.home() / key).unlink(missing_ok=True)


def cover_key(track_id: int) -> str:
    return f"covers/{track_id}"


def cover(track_id: int, audio: str | Path | None) -> tuple[bytes, str] | None:
    """The track's cover (picture, media type), read from its audio file the first time and kept; None without one.
    `audio` is the track's stored path (tracks.local_path); when the audio is gone and nothing was kept, None."""
    kept = _get(cover_key(track_id))
    if kept is not None:
        return (kept, image_type(kept) or "application/octet-stream") if kept else None
    if not audio:
        return None
    here = Path(audio).is_file()
    with blobs.copy_slot():
        p = blobs.ensure(audio)  # brought down from the bucket if only it has the file
        if p is None:
            return None  # the audio is gone (expired): don't remember "no cover", it may just be unknown
        try:
            got = read_cover(p)
        finally:
            if not here and not blobs.keep_copies:  # the Worker's disk is memory: drop the copy we made
                p.unlink(missing_ok=True)
    _put(cover_key(track_id), got[0] if got else b"", got[1] if got else "application/octet-stream")
    return got


def remove_cover(track_id: int) -> None:
    _delete(cover_key(track_id))


def art_key(share_id: str) -> str:
    return f"share-art/{share_id}.png"


def put_art(share_id: str, png: bytes) -> None:
    _put(art_key(share_id), png, "image/png")


def art(share_id: str) -> bytes | None:
    return _get(art_key(share_id))


def has_art(share_id: str) -> bool:
    return _has(art_key(share_id))
