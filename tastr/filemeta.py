"""Reading embedded tags and technical info from audio files.

Everything here is best effort: a file with no tags, or one mutagen can't parse,
yields empty dicts rather than an error, and the caller falls back to the file name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import mutagen
except ImportError:  # keep ingest working if the optional parse dependency is missing
    mutagen = None

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
}

_FORMATS = {".mp3": "MP3", ".m4a": "M4A", ".aac": "AAC", ".flac": "FLAC", ".ogg": "OGG", ".wav": "WAV", ".aif": "AIFF", ".aiff": "AIFF"}


def _first(easy, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        try:
            vals = easy.get(k)
        except Exception:
            continue
        if vals:
            v = str(vals[0]).strip()
            if v:
                return v
    return None


# WAV/AIFF expose raw ID3 frames even in easy mode, so read these frames directly.
_ID3_FRAMES = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "album_artist": "TPE2", "year": "TDRC", "genre": "TCON",
               "track": "TRCK", "label": "TPUB", "bpm": "TBPM", "key": "TKEY"}


def _id3_tags(raw) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, frame in _ID3_FRAMES.items():
        f = raw.get(frame)
        if f is not None and f.text and str(f.text[0]).strip():
            out[name] = str(f.text[0]).strip()
    for f in raw.getall("COMM"):
        if f.text and str(f.text[0]).strip():
            out["comment"] = str(f.text[0]).strip()
            break
    return out


def read_file_meta(path: str | Path) -> tuple[dict[str, str], dict[str, Any]]:
    """Return (tags, audio_info) for `path`. Both are empty when nothing could be read."""
    p = Path(path)
    tags: dict[str, str] = {}
    info: dict[str, Any] = {}
    try:
        info["file_size"] = p.stat().st_size
    except OSError:
        pass
    if mutagen is None:
        return tags, info
    try:
        easy = mutagen.File(p, easy=True)
    except Exception:
        return tags, info
    if easy is None:
        return tags, info
    if hasattr(easy.tags, "getall"):  # plain ID3 (WAV/AIFF): no easy mapping
        tags = _id3_tags(easy.tags)
    else:
        for name, keys in _TAG_KEYS.items():
            v = _first(easy.tags, keys) if easy.tags is not None else None
            if v:
                tags[name] = v
    a = getattr(easy, "info", None)
    if a is not None:
        info["format"] = _FORMATS.get(p.suffix.lower(), p.suffix.lstrip(".").upper() or None)
        for attr, key in (("sample_rate", "sample_rate"), ("channels", "channels"), ("bits_per_sample", "bit_depth"),
                          ("bitrate", "bitrate"), ("length", "duration")):
            v = getattr(a, attr, None)
            if v:
                info[key] = v
        codec = getattr(a, "codec_description", None) or getattr(a, "codec", None) or type(a).__module__.rsplit(".", 1)[-1].upper()
        info["codec"] = str(codec)
    return tags, info


_TAG_LABELS = [("title", "Title"), ("artist", "Artist"), ("album", "Album"), ("album_artist", "Album artist"), ("year", "Year"),
               ("genre", "Genre"), ("track", "Track"), ("label", "Label"), ("bpm", "BPM"), ("key", "Key"), ("comment", "Comment")]


def _mmss(sec: float) -> str:
    s = round(sec)
    return f"{s // 60}:{s % 60:02d}"


def _size(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"


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

    source = {"Kind": s.kind, ("URL" if s.kind == "url" else "File"): s.uri if s.kind == "url" else Path(s.uri).name}
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
