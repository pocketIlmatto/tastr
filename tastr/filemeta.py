"""Reading embedded tags and technical info from audio files.

Everything here is best effort: a file with no tags, or one mutagen can't parse,
yields empty dicts rather than an error, and the caller falls back to the file name.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

try:
    import mutagen
    import mutagen.mp3
    import mutagen.mp4
except ImportError:  # keep ingest working if the optional parse dependency is missing
    mutagen = None

# Bump when the reader starts finding more (or different) fields, so `tastr serve` refreshes stored cards once.
READER_VERSION = 3

# Our field name -> keys to try in mutagen's "easy" tag mapping, in order.
_TAG_KEYS = {
    "title": ("title",),
    "artist": ("artist",),
    "album": ("album",),
    "album_artist": ("albumartist",),
    "year": ("date", "originaldate"),
    "genre": ("genre",),
    "track": ("tracknumber",),
    "label": ("organization",),
    "bpm": ("bpm",),
    "key": ("initialkey",),
    "comment": ("comment", "description"),
    "isrc": ("isrc",),
    "musicbrainz_recording": ("musicbrainz_trackid",),  # Picard's "track id" is the recording MBID
}

_FORMATS = {".mp3": "MP3", ".m4a": "M4A", ".aac": "AAC", ".flac": "FLAC", ".ogg": "OGG", ".wav": "WAV", ".aif": "AIFF", ".aiff": "AIFF"}


def _first(easy, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        try:
            vals = easy.get(k)
        except Exception:  # mutagen's easy wrappers raise for keys a format doesn't support; try the next key
            continue
        if vals:
            v = str(vals[0]).strip()
            if v:
                return v
    return None


# WAV/AIFF expose raw ID3 frames even in easy mode, so read these frames directly.
_ID3_FRAMES = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "album_artist": "TPE2", "year": "TDRC", "genre": "TCON",
               "track": "TRCK", "label": "TPUB", "bpm": "TBPM", "key": "TKEY", "isrc": "TSRC"}

# Names of user-defined fields (ID3 TXXX, MP4 "----") that carry our fields, matched case-insensitively.
_FREEFORM_NAMES = {"label": ("label", "organization", "publisher"), "key": ("initialkey", "key", "initial key"),
                   "bpm": ("bpm",), "comment": ("comment", "description"), "isrc": ("isrc",),
                   "musicbrainz_recording": ("musicbrainz track id",)}


def _id3_tags(raw) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, frame in _ID3_FRAMES.items():
        f = raw.get(frame)
        if f is not None and f.text and str(f.text[0]).strip():
            out[name] = str(f.text[0]).strip()
    for f in raw.getall("UFID"):  # Picard stores the recording MBID as a unique file id
        if f.owner == "http://musicbrainz.org" and f.data:
            out["musicbrainz_recording"] = f.data.decode("ascii", "replace").strip()
            break
    for f in raw.getall("COMM"):
        if f.text and str(f.text[0]).strip():
            out["comment"] = str(f.text[0]).strip()
            break
    # Some taggers (ffmpeg among them) put these in user-defined TXXX frames instead.
    txxx = {f.desc.lower(): str(f.text[0]).strip() for f in raw.getall("TXXX") if f.text and str(f.text[0]).strip()}
    for name, descs in _FREEFORM_NAMES.items():
        if name not in out:
            v = next((txxx[d] for d in descs if d in txxx), None)
            if v:
                out[name] = v
    return out


# MP4/M4A atoms; the rest come from freeform ("----:mean:name") atoms.
_MP4_ATOMS = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "album_artist": "aART", "year": "\xa9day",
              "genre": "\xa9gen", "comment": "\xa9cmt"}


def _mp4_text(v) -> str | None:
    if isinstance(v, (bytes, bytearray)):
        v = bytes(v).decode("utf-8", "replace")
    v = str(v).strip().strip("\x00").strip()
    return v or None


def _mp4_tags(raw) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, atom in _MP4_ATOMS.items():
        vals = raw.get(atom)
        if vals and (v := _mp4_text(vals[0])):
            out[name] = v
    trkn = raw.get("trkn")
    if trkn and trkn[0] and trkn[0][0]:
        out["track"] = str(trkn[0][0])
    tmpo = raw.get("tmpo")
    if tmpo and tmpo[0]:
        out["bpm"] = str(tmpo[0])
    free = {k.split(":", 2)[-1].lower(): v for k, v in raw.items() if k.startswith("----")}
    for name, names in _FREEFORM_NAMES.items():
        if name in out:
            continue
        for n in names:
            vals = free.get(n)
            if vals and (v := _mp4_text(vals[0])):
                out[name] = v
                break
    return out


def _reopen(cls, p: Path):
    """The file again as `cls` (mutagen.mp4.MP4 or mutagen.mp3.MP3), whose raw tags easy mode hides; None on failure."""
    try:
        return cls(p)
    except Exception as e:  # mutagen raises many types for damaged or unknown files
        log.debug("can't read tags from %s: %s", p.name, e)
        return None


def _easy_tags(easy) -> dict[str, str]:
    tags: dict[str, str] = {}
    for name, keys in _TAG_KEYS.items():
        if easy.tags is not None and (v := _first(easy.tags, keys)):
            tags[name] = v
    return tags


def _read_tags(p: Path, easy):
    """(the mutagen file the tags came from, tags); (None, {}) when it can't be read."""
    if isinstance(easy, mutagen.mp4.MP4):  # easy mode hides most atoms, so read the raw ones
        raw = _reopen(mutagen.mp4.MP4, p)
        return (raw, _mp4_tags(raw.tags or {})) if raw is not None else (None, {})
    if isinstance(easy, mutagen.mp3.MP3):  # EasyID3 has no comment/key mapping, so read the frames
        raw = _reopen(mutagen.mp3.MP3, p)
        return (raw, _id3_tags(raw.tags) if raw.tags is not None else {}) if raw is not None else (None, {})
    if hasattr(easy.tags, "getall"):  # plain ID3 (WAV/AIFF): no easy mapping
        return easy, _id3_tags(easy.tags)
    return easy, _easy_tags(easy)


_INFO_ATTRS = (("sample_rate", "sample_rate"), ("channels", "channels"), ("bits_per_sample", "bit_depth"),
               ("bitrate", "bitrate"), ("length", "duration"))


def _audio_info(f, p: Path) -> dict[str, Any]:
    a = getattr(f, "info", None)
    if a is None:
        return {}
    info: dict[str, Any] = {"format": _FORMATS.get(p.suffix.lower(), p.suffix.lstrip(".").upper() or None)}
    lossy_mp4 = isinstance(f, mutagen.mp4.MP4) and getattr(a, "codec", "") != "alac"
    for attr, key in _INFO_ATTRS:
        v = getattr(a, attr, None)
        if v and not (lossy_mp4 and key == "bit_depth"):  # AAC reports a nominal 16 that isn't real
            info[key] = v
    codec = getattr(a, "codec_description", None) or getattr(a, "codec", None) or type(a).__module__.rsplit(".", 1)[-1].upper()
    info["codec"] = str(codec)
    return info


def read_file_meta(path: str | Path) -> tuple[dict[str, str], dict[str, Any]]:
    """Return (tags, audio_info) for `path`. Both are empty when nothing could be read."""
    p = Path(path)
    info: dict[str, Any] = {}
    try:
        info["file_size"] = p.stat().st_size
    except OSError:  # gone or unreadable: the tag read below fails the same way and returns empty dicts
        pass
    if mutagen is None:
        return {}, info
    try:
        easy = mutagen.File(p, easy=True)
    except Exception as e:  # mutagen raises many types for damaged or unknown files
        log.debug("can't read tags from %s: %s", p.name, e)
        return {}, info
    if easy is None:
        return {}, info
    f, tags = _read_tags(p, easy)
    if f is None:
        return tags, info
    return tags, info | _audio_info(f, p)


_TAG_LABELS = [("title", "Title"), ("artist", "Artist"), ("album", "Album"), ("album_artist", "Album artist"), ("year", "Year"),
               ("genre", "Genre"), ("track", "Track"), ("label", "Label"), ("bpm", "BPM"), ("key", "Key"), ("comment", "Comment"), ("isrc", "ISRC"),
               ("musicbrainz_recording", "MusicBrainz recording")]


def _mmss(sec: float) -> str:
    s = round(sec)
    return f"{s // 60}:{s % 60:02d}"


def _size(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"


def legacy_upload_prefix(uri: str) -> str:
    """The "ab12cd34_" prefix older uploads carry in their stored file name ("" for anything else)."""
    p = Path(uri)
    m = re.match(r"[0-9a-f]{8}_", p.name)
    return m.group(0) if m and p.parent.name == "uploads" else ""


def describe(card, analyzed_at: str | None = None) -> dict[str, dict[str, str]]:
    """The card's metadata as display-ready groups: File tags, Audio, Source, Analysis.

    Each group maps label -> text; values that are missing are left out, so an
    empty group means "nothing found".
    """
    s, info = card.source, card.source.audio_info
    audio: dict[str, str] = {}
    if info.get("format"):
        audio["Format"] = info["format"]
    if info.get("codec"):
        audio["Codec"] = info["codec"]
    if info.get("bitrate"):
        audio["Bitrate"] = f"{round(info['bitrate'] / 1000)} kbps"
    sr = info.get("sample_rate") or card.sample_rate
    if sr:
        audio["Sample rate"] = f"{sr / 1000:g} kHz"
    if info.get("bit_depth"):
        audio["Bit depth"] = f"{info['bit_depth']}-bit"
    if info.get("channels"):
        audio["Channels"] = {1: "Mono", 2: "Stereo"}.get(info["channels"], f"{info['channels']} channels")
    dur = info.get("duration") or card.duration
    if dur:
        audio["Duration"] = _mmss(dur)
    if info.get("file_size"):
        audio["File size"] = _size(info["file_size"])

    source = {"Kind": s.kind, ("URL" if s.kind == "url" else "File"): s.uri if s.kind == "url" else Path(s.uri).name.removeprefix(legacy_upload_prefix(s.uri))}
    if s.sha256:
        source["SHA256"] = s.sha256
    analysis: dict[str, str] = {}
    if analyzed_at:
        analysis["Analyzed at"] = analyzed_at
    if card.stages_run:
        analysis["Stages"] = ", ".join(f"{k} {v}" for k, v in card.stages_run.items())
    return {
        "File tags": {label: s.file_tags[k] for k, label in _TAG_LABELS if s.file_tags.get(k)},
        "Audio": audio,
        "Source": source,
        "Analysis": analysis,
    }
