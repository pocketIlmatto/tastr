"""Your files: the audio you uploaded and the stems made from it, their sizes, zips of them, and deleting stems.

On a hosted server every route here sees only the caller's own tracks: /api/tracks/{id}/... goes through the
login middleware's owner check, and the bulk routes check each id themselves.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ... import blobs, stems
from ..context import AppContext, _unprefixed, tastr_home
from ..downloads import (ZIP_MAX_BYTES, ZIP_MAX_TRACKS, base_name, content_disposition, stem_file_name,
                         stream_zip, unique)
from ..hosted import stored_bytes

FORMATS = {".mp3": "MP3", ".wav": "WAV", ".flac": "FLAC", ".m4a": "M4A", ".aac": "AAC", ".ogg": "OGG",
           ".opus": "OPUS", ".aif": "AIFF", ".aiff": "AIFF"}


def _size(p: Path | bytes) -> int:
    return len(p) if isinstance(p, bytes) else blobs.size(p)  # here, or in the audio bucket


def _fmt_bytes(b: int) -> str:
    return f"{b / 1024 ** 3:.1f} GB" if b >= 1024 ** 3 else f"{b / 1024 ** 2:.1f} MB"


class ZipRequest(BaseModel):
    ids: list[int]
    kind: Literal["audio", "stems"]


def track_rows(ctx: AppContext, owner: str | None, ids: list[int] | None = None) -> list[dict[str, Any]]:
    """The caller's tracks (all of them on your own machine): what a download needs, from the tracks table."""
    where, args = (["owner = ?"], [owner]) if owner else ([], [])
    if ids is not None:
        where.append(f"id IN ({','.join('?' * len(ids))})")
        args += ids
    sql = ("SELECT id, title, artist, duration, created_at, analyzed_at, sha256, local_path, source_uri FROM tracks"
           + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY id")
    with closing(ctx.conn()) as c:
        out = [dict(r) for r in c.execute(sql, args).fetchall()]
    for r in out:
        r["title"] = _unprefixed(r["title"], r["source_uri"]) or Path(r["source_uri"] or "").stem or None
        r["base"] = base_name(r["title"], r["artist"], r["id"])
        r["folder"] = stems.stems_dir(r["sha256"], r["id"])
        p = Path(r["local_path"]) if r["local_path"] else None
        r["audio_path"] = p if p and blobs.exists(p) else None
    return out


def zip_response(entries: list[tuple[str, Path | bytes]], name: str) -> StreamingResponse:
    total = sum(_size(p) for _, p in entries)
    return StreamingResponse(stream_zip(entries), media_type="application/zip",
                             headers={"Content-Disposition": content_disposition(name), "X-Zip-Bytes": str(total),
                                      "Access-Control-Expose-Headers": "X-Zip-Bytes", "Cache-Control": "no-store"})


def zip_too_big(entries: list[tuple[str, Path]]) -> None:
    """413 when the files add up to more than one zip may hold."""
    total = sum(_size(p) for _, p in entries)
    if total > ZIP_MAX_BYTES:
        raise HTTPException(413, f"That's {_fmt_bytes(total)}, more than one zip can hold ({_fmt_bytes(ZIP_MAX_BYTES)}). "
                                 "Download the tracks in smaller groups.")


def router(ctx: AppContext) -> APIRouter:
    api = APIRouter()

    def rows(owner: str | None, ids: list[int] | None = None) -> list[dict[str, Any]]:
        return track_rows(ctx, owner, ids)

    def stem_info(r: dict[str, Any]) -> dict[str, Any] | None:
        st = ctx.stem_jobs.status(r["folder"])
        if st["state"] == "none":
            return None
        out: dict[str, Any] = {"state": st["state"]}
        for k in ("progress", "position", "elapsed", "error"):
            if k in st:
                out[k] = st[k]
        if st["state"] == "ready":
            out["bytes"] = {n: _size(stems.stem_path(r["folder"], n)) for n in stems.STEM_NAMES}
        return out

    @api.get("/api/files")
    def files(request: Request):
        """Your tracks with the size of their audio and stems (from the file system or the bucket's listing), plus
        the storage used, and on a hosted server the cap and the server's free space."""
        tracks, audio_b, stem_b = [], 0, 0
        for r in rows(ctx.owner_of(request)):
            p = r["audio_path"]
            audio = {"name": r["base"] + p.suffix.lower(), "format": FORMATS.get(p.suffix.lower(), p.suffix.lstrip(".").upper()),
                     "bytes": _size(p)} if p else None
            st = stem_info(r)
            audio_b += audio["bytes"] if audio else 0
            stem_b += sum(st["bytes"].values()) if st and "bytes" in st else 0
            tracks.append({"id": r["id"], "title": r["title"], "artist": r["artist"], "base": r["base"],
                           "created_at": r["created_at"] or r["analyzed_at"], "duration": r["duration"],
                           "audio": audio, "stems": st})
        cap = free = None
        if ctx.hosted:  # the cap is the whole server's (TASTR_STORAGE_GB), so free space is the server's too
            cap = int(ctx.settings.storage_gb * 1024 ** 3)
            free = max(0, cap - stored_bytes(tastr_home()))
        return {"used": {"audio_bytes": audio_b, "stem_bytes": stem_b}, "cap_bytes": cap, "free_bytes": free,
                "stems_available": stems.available(), "tracks": tracks}

    @api.get("/api/tracks/{track_id}/stems.zip")
    def stems_zip(track_id: int):
        """All four stems of one track in one zip."""
        found = rows(None, [track_id])
        if not found:
            raise HTTPException(404, "no such track")
        r = found[0]
        if not stems.is_ready(r["folder"]):
            raise HTTPException(404, "This track has not been separated into stems.")
        entries = [(stem_file_name(r["base"], n), stems.stem_path(r["folder"], n)) for n in stems.STEM_NAMES]
        return zip_response(entries, f"{r['base']} stems.zip")

    @api.post("/api/files/zip")
    def bulk_zip(body: ZipRequest, request: Request):
        """One zip of several tracks' original audio, or of their stems in a folder per track. Tracks without
        that file are left out; someone else's track counts as one that doesn't exist."""
        ids = list(dict.fromkeys(body.ids))
        if not ids:
            raise HTTPException(400, "Pick at least one track.")
        if len(ids) > ZIP_MAX_TRACKS:
            raise HTTPException(413, f"A zip can hold up to {ZIP_MAX_TRACKS} tracks; you picked {len(ids)}. "
                                     "Download them in smaller groups.")
        found = rows(ctx.owner_of(request), ids)
        if len(found) != len(ids):
            raise HTTPException(404, "no such track")
        entries: list[tuple[str, Path]] = []
        taken: set[str] = set()
        n = 0
        for r in found:
            if body.kind == "audio":
                if r["audio_path"]:
                    entries.append((unique(r["base"] + r["audio_path"].suffix.lower(), taken), r["audio_path"]))
                    n += 1
            elif stems.is_ready(r["folder"]):
                folder = unique(r["base"], taken)
                entries += [(f"{folder}/{stem_file_name(r['base'], s)}", stems.stem_path(r["folder"], s)) for s in stems.STEM_NAMES]
                n += 1
        if not entries:
            raise HTTPException(404, "None of these tracks has its original audio on the server." if body.kind == "audio"
                                else "None of these tracks has stems yet.")
        zip_too_big(entries)
        what = "audio" if body.kind == "audio" else "stems"
        return zip_response(entries, f"Tastr {what}, {n} track{'' if n == 1 else 's'}.zip")

    @api.delete("/api/tracks/{track_id}/stems")
    def delete_stems(track_id: int, request: Request):
        """Delete a track's stems to free space; they can be made again later. Stems are kept per audio file, so
        while another track (yours or someone else's) has the same audio, they stay."""
        found = rows(None, [track_id])
        if not found:
            raise HTTPException(404, "no such track")
        r, folder = found[0], found[0]["folder"]
        state = ctx.stem_jobs.status(folder)["state"]
        if state in ("queued", "running"):
            raise HTTPException(409, "Stems are being made for this track. Delete them once that finishes.")
        paths = [stems.stem_path(folder, n) for n in stems.STEM_NAMES]
        if not folder.is_dir() and not any(blobs.in_bucket(p) for p in paths):
            ctx.stem_jobs.forget(folder)  # a failed job: clear its error
            raise HTTPException(404, "This track has no stems.")
        if r["sha256"]:
            with closing(ctx.conn()) as c:
                shared = c.execute("SELECT 1 FROM tracks WHERE sha256 = ? AND id != ?", (r["sha256"], track_id)).fetchone()
            if shared:
                raise HTTPException(409, "Another copy of this audio uses the same stems, so they stay.")
        freed = sum(_size(p) for p in paths)
        ctx.stem_jobs.forget(folder)
        stems.remove(folder)
        ctx.event(ctx.owner_of(request), "stems", "Deleted stems for 1 track")
        return {"state": "none", "freed_bytes": freed}

    return api
