"""The track card: the one data structure every part of Tastr reads or writes.

Pipeline stages fill in parts of a TrackCard; storage persists it; UIs render it.
Fields a stage hasn't filled yet stay None / empty, so a card is always valid
even when only some stages ran.

Bump CARD_SCHEMA_VERSION whenever a field is renamed or changes meaning.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

CARD_SCHEMA_VERSION = 1


@dataclass
class Source:
    kind: str  # "file" | "url"
    uri: str  # absolute path or URL the user gave us
    local_path: str | None = None  # where the audio actually lives on disk
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    sha256: str | None = None  # content hash, used to dedupe re-analysis
    file_tags: dict[str, str] = field(default_factory=dict)  # embedded tags as read: title, artist, album, year...
    audio_info: dict[str, Any] = field(default_factory=dict)  # format, codec, bitrate (bps), sample_rate, channels, file_size...


@dataclass
class Key:
    tonic: int  # pitch class, 0 = C
    mode: str  # "major" | "minor"
    scale: str | None = None  # modal flavour in Live's naming: "Dorian", "Minor", ...
    camelot: str | None = None  # e.g. "8A"
    confidence: float | None = None  # 0..1, top key probability minus the runner-up's
    likely_correct: float | None = None  # 0..1, share of benchmark tracks at this confidence with the exact key

    @property
    def name(self) -> str:
        from .music import note_name

        return f"{note_name(self.tonic)} {self.mode}"


@dataclass
class Core:
    """Filled by the `core` stage."""

    bpm: float | None = None
    bpm_confidence: float | None = None  # 0..1, how clear the pulse is in this track
    bpm_likely_correct: float | None = None  # 0..1, share of benchmark tracks at this confidence within 4% of the label
    key: Key | None = None
    energy: float | None = None  # 0..1, whole-track
    loudness_db: float | None = None
    beat_times: list[float] = field(default_factory=list)  # seconds
    downbeat_times: list[float] = field(default_factory=list)  # seconds, bar starts
    beats_per_bar: int = 4  # 3 (3/4) or 4 (4/4): detected, or set by the user (meter_set)
    meter_set: bool = False  # True when the user picked the meter for this track instead of detecting it


@dataclass
class Section:
    """Filled by the `sections` stage. Times in seconds, bars 1-based and inclusive."""

    start: float
    end: float
    label: str  # "intro" | "verse" | "build" | "drop" | "breakdown" | "outro" | ...
    letter: str | None = None  # similarity group: sections sharing a letter sound alike
    start_bar: int | None = None
    end_bar: int | None = None
    energy: float | None = None  # 0..1 relative to the track
    confidence: float | None = None


@dataclass
class Tag:
    """Filled by the `tags` stage."""

    tag: str
    score: float  # 0..1


@dataclass
class Suggestion:
    """Filled by the `suggestions` stage ("Try this in Ableton")."""

    category: str  # "setup" | "drums" | "bass" | "harmony" | "arrangement" | "sound"
    title: str
    body: str
    data: dict[str, Any] = field(default_factory=dict)  # structured payload, e.g. step patterns


@dataclass
class TrackCard:
    source: Source
    duration: float | None = None
    sample_rate: int | None = None
    core: Core = field(default_factory=Core)
    sections: list[Section] = field(default_factory=list)
    tags: list[Tag] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    # Time series for visualization, keyed by name, e.g. {"energy": Series(...)}.
    series: dict[str, "Series"] = field(default_factory=dict)
    # Free-form scalar features any stage can add ("brightness", "swing_percent"...).
    # They are stored as rows, so new ones need no schema change.
    features: dict[str, float] = field(default_factory=dict)
    # Free-form structured output a stage wants to keep (drum grids, chord lists...).
    extras: dict[str, Any] = field(default_factory=dict)
    stages_run: dict[str, str] = field(default_factory=dict)  # stage name -> stage version
    errors: dict[str, str] = field(default_factory=dict)  # stage name -> error message
    schema_version: int = CARD_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), **kw)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TrackCard":
        core = dict(d.get("core") or {})
        if core.get("key"):
            core["key"] = Key(**core["key"])
        return cls(
            source=Source(**d["source"]),
            duration=d.get("duration"),
            sample_rate=d.get("sample_rate"),
            core=Core(**core),
            sections=[Section(**s) for s in d.get("sections", [])],
            tags=[Tag(**t) for t in d.get("tags", [])],
            suggestions=[Suggestion(**s) for s in d.get("suggestions", [])],
            series={k: Series(**v) for k, v in (d.get("series") or {}).items()},
            features=dict(d.get("features") or {}),
            extras=dict(d.get("extras") or {}),
            stages_run=dict(d.get("stages_run") or {}),
            errors=dict(d.get("errors") or {}),
            schema_version=d.get("schema_version", CARD_SCHEMA_VERSION),
        )


@dataclass
class Series:
    """A sampled curve: values[i] is at times[i] seconds (e.g. energy per bar)."""

    times: list[float]
    values: list[float]
