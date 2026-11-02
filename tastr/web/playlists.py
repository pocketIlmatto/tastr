"""A saved list as a playlist file (Library › List › Export): M3U8, XSPF or PLS text, and the m3u8 inside the zip.

Each entry is a dict with `location`, `title`, `artist` and `duration` (seconds). The routes decide where entries
point: the track's audio on this server, a signed public link when hosted, or a file inside the zip.
"""

from __future__ import annotations

import re
from typing import Any
from xml.sax.saxutils import escape

FORMATS = {"m3u8": "audio/x-mpegurl", "xspf": "application/xspf+xml", "pls": "audio/x-scpls"}
_BAD = re.compile(r'[\\/:*?"<>|]')


def file_name(name: str) -> str:
    """The list's name as a file name: the characters Windows refuses become _, cut to 60 characters."""
    return _BAD.sub("_", name).strip()[:60] or "Playlist"


def who(e: dict[str, Any]) -> str:
    """"Artist - Title", as players show an entry."""
    title = e.get("title") or "Untitled"
    return f"{e['artist']} - {title}" if e.get("artist") else title


def _secs(e: dict[str, Any]) -> int:
    return round(e.get("duration") or 0)


def _flat(s: str) -> str:
    """One line: a title with a line break in it would start a bogus entry in a line-based playlist."""
    return re.sub(r"[\r\n]+", " ", s)


def m3u8(name: str, entries: list[dict[str, Any]]) -> str:
    lines = ["#EXTM3U", f"#PLAYLIST:{_flat(name)}"]
    for e in entries:
        lines += [f"#EXTINF:{_secs(e)},{_flat(who(e))}", e["location"]]
    return "\n".join(lines) + "\n"


def pls(name: str, entries: list[dict[str, Any]]) -> str:
    lines = ["[playlist]"]
    for i, e in enumerate(entries, 1):
        lines += [f"File{i}={e['location']}", f"Title{i}={_flat(who(e))}", f"Length{i}={_secs(e)}"]
    return "\n".join(lines + [f"NumberOfEntries={len(entries)}", "Version=2"]) + "\n"


def xspf(name: str, entries: list[dict[str, Any]]) -> str:
    def track(e: dict[str, Any]) -> str:
        creator = f"<creator>{escape(e['artist'])}</creator>" if e.get("artist") else ""
        return (f"    <track><location>{escape(e['location'])}</location><title>{escape(e.get('title') or 'Untitled')}</title>"
                f"{creator}<duration>{round((e.get('duration') or 0) * 1000)}</duration></track>")
    return "\n".join(['<?xml version="1.0" encoding="UTF-8"?>', '<playlist version="1" xmlns="http://xspf.org/ns/0/">',
                      f"  <title>{escape(name)}</title>", "  <trackList>", *map(track, entries), "  </trackList>",
                      "</playlist>"]) + "\n"


BUILD = {"m3u8": m3u8, "xspf": xspf, "pls": pls}


def zip_audio_name(pos: int, count: int, base: str, ext: str) -> str:
    """audio/NN Artist - Title.ext: the position padded to 2 digits, or 3 past 99."""
    return f"audio/{str(pos).zfill(3 if count > 99 else 2)} {base}{ext}"
