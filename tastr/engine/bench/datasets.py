"""Labeled datasets the benchmark can run on.

Every loader returns a list of `Item`s: audio (a file path, or a callable that
synthesizes it) plus whatever reference labels that dataset has. Labels a
dataset doesn't provide stay None and their metrics are skipped.

None of these download anything: point them at a folder you fetched with the
dataset's own scripts (see docs/bench.md for where to get each one).
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Callable

import numpy as np

from ..audio import AUDIO_EXTENSIONS
from ..music import NOTE_NAMES


@dataclass
class Truth:
    bpm: float | None = None
    tonic: int | None = None  # pitch class, 0 = C
    mode: str | None = None  # "major" | "minor"
    beats: list[float] | None = None
    downbeats: list[float] | None = None
    boundaries: list[float] | None = None  # section start times, seconds
    labels: list[str] | None = None  # section labels, same length as boundaries
    beats_per_bar: int | None = None  # 3 or 4 when the labeled bars are steadily that long


@dataclass
class Item:
    id: str
    truth: Truth
    path: str | None = None
    synth: Callable[[], np.ndarray] | None = field(default=None, repr=False)
    meta: dict = field(default_factory=dict)


# --- Label parsing ------------------------------------------------------------

_PC = {n.lower(): i for i, n in enumerate(NOTE_NAMES)}
_PC.update({"db": 1, "d#": 3, "gb": 6, "g#": 8, "a#": 10, "cb": 11, "e#": 5, "fb": 4, "b#": 0})


def parse_key(text: str) -> tuple[int, str] | None:
    """'A minor', 'Ebm', 'F# maj', 'c#:min', '8A' (Camelot) -> (tonic, mode). None if unknown."""
    s = text.strip().lower().replace("♯", "#").replace("♭", "b")
    m = re.fullmatch(r"(1[0-2]|[1-9])\s*([ab])", s)
    if m:  # Camelot: number n, A = minor, B = major
        n, letter = int(m.group(1)), m.group(2)
        major_pc = (7 * (n - 8)) % 12  # inverse of music.camelot
        return ((major_pc - 3) % 12, "minor") if letter == "a" else (major_pc, "major")
    m = re.fullmatch(r"([a-g][#b]?)\s*[:\s]?\s*(major|minor|maj|min|m)?", s)
    if not m or m.group(1) not in _PC:
        return None
    mode = "minor" if m.group(2) in ("minor", "min", "m") else "major"
    return _PC[m.group(1)], mode


def parse_time(text: str) -> float:
    """'83.5', '1:23.5' or '1:02:03' -> seconds."""
    parts = [float(p) for p in text.strip().split(":")]
    t = 0.0
    for p in parts:
        t = t * 60 + p
    return t


def _audio_by_stem(root: Path) -> dict[str, Path]:
    """Index audio files by name with all extensions stripped ("123.LOFI.mp3" -> "123")."""
    out = {}
    for f in root.rglob("*"):
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
            out[f.name.split(".")[0]] = f
    return out


# --- Datasets -------------------------------------------------------------------


def _synth_audio(bpm, tonic, mode, bars):
    from .synth import make_track

    return make_track(bpm=bpm, tonic=tonic, mode=mode, bars_per_section=bars)[0]


def synth(n: int = 12, seed: int = 0) -> list[Item]:
    """Synthetic tracks with exact labels (tastr.bench.synth). A smoke test, not a benchmark."""
    from .synth import make_track

    rng = np.random.default_rng(seed)
    items = []
    for i in range(n):
        bpm = float(rng.choice([90, 100, 110, 120, 124, 128, 140, 150, 170, 174]))
        tonic = int(rng.integers(12))
        mode = "minor" if i % 2 == 0 else "major"
        bars = (4, 8, 4)
        y, t = make_track(bpm=bpm, tonic=tonic, mode=mode, bars_per_section=bars)
        beat = 60.0 / bpm
        n_beats = int(y.size / 22050 / beat)
        beats = [k * beat for k in range(n_beats)]
        truth = Truth(bpm=bpm, tonic=tonic, mode=mode, beats=beats, downbeats=beats[::4],
                      boundaries=[s["start"] for s in t["sections"]], labels=["intro", "main", "outro"])
        items.append(Item(id=f"synth-{i:02d}-{bpm:g}-{NOTE_NAMES[tonic]}{'m' if mode == 'minor' else ''}",
                          truth=truth, synth=partial(_synth_audio, bpm, tonic, mode, bars)))
    return items


def giantsteps_key(root: str | Path) -> list[Item]:
    """GiantSteps Key: 604 two-minute EDM previews, one global key each.

    Expects the repo layout: <root>/annotations/key/<id>.LOFI.key (text like
    "A minor") and audio anywhere under <root> named <id>.LOFI.mp3.
    """
    root = Path(root)
    audio = _audio_by_stem(root)
    items = []
    for f in sorted(root.rglob("*.key")):
        key = parse_key(f.read_text().strip().split("\n")[0])
        sid = f.name.split(".")[0]
        if key is None or sid not in audio:
            continue
        items.append(Item(id=f"gs-key-{sid}", path=str(audio[sid]), truth=Truth(tonic=key[0], mode=key[1])))
    return items


def beatport_key(root: str | Path) -> list[Item]:
    """Beatport EDM Key: 1,486 two-minute EDM previews, one global key each.

    Zenodo record 1101082, unzipped under <root>: key files <root>/keys/<id> <name>.txt
    (text like "D minor") and audio anywhere under <root> named "<id> <name>.mp3".
    Only plain major/minor labels are scored; ambiguous ("E minor | E major"),
    atonal ("X"), modal ("F minor phrygian") and "other" labels are skipped.
    """
    root = Path(root)
    audio = {}
    for f in root.rglob("*"):
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS and not f.name.startswith("._"):
            audio[f.name.split(" ")[0]] = f
    items = []
    for f in sorted(root.rglob("*.txt")):
        if f.name.startswith("._"):  # macOS resource forks in the zips
            continue
        label = f.read_text().strip()
        key = parse_key(label) if re.fullmatch(r"[A-Ga-g][#b]? (major|minor)", label) else None
        sid = f.name.split(" ")[0]
        if key is None or not sid.isdigit() or sid not in audio:
            continue
        items.append(Item(id=f"bp-key-{sid}", path=str(audio[sid]), truth=Truth(tonic=key[0], mode=key[1])))
    return items


def fma_keys(root: str | Path) -> list[Item]:
    """FMA Keys (FMAK, Zenodo 10719860): Free Music Archive songs across 17 genres, key and mode labeled by
    an expert annotator, about half major. Full songs, not previews.

    Reads <root>/fma_keys_metadata.csv (track_id, spotify_uri, key_and_mode like "F# Major") and audio
    named "<track id>.mp3" (zero-padded, as FMA ships it) anywhere under <root>, as `tastr fetch fma-keys`
    lays it out. Only songs whose zip has been fetched are scored.
    """
    root = Path(root)
    audio = {}
    for f in root.rglob("*.mp3"):
        if f.is_file() and not f.name.startswith("._") and f.stem.isdigit():
            audio[int(f.stem)] = f
    items = []
    with (root / "fma_keys_metadata.csv").open(newline="") as fh:
        for row in csv.DictReader(fh):
            key = parse_key(row.get("key_and_mode") or "")
            tid = int(row["track_id"]) if (row.get("track_id") or "").isdigit() else None
            if key is None or tid not in audio:
                continue
            items.append(Item(id=f"fmak-{tid}", path=str(audio[tid]), truth=Truth(tonic=key[0], mode=key[1])))
    return items


def ballroom(root: str | Path) -> list[Item]:
    """Ballroom (Gouyon et al. 2006): 698 30 s excerpts of eight ballroom styles with a tempo and beat/bar labels.

    Reads <root>/tempo/<id>.bpm (the ISMIR 2004 labels), <root>/beats/<id>.beats ("time beat-in-bar" per line,
    from CPJKU/BallroomAnnotations) and audio <root>/audio/<style>/<id>.wav, as `tastr fetch ballroom` lays
    it out. The meter is the bar length when at least 90% of the labeled bars agree (Waltz is 3, the rest 4).
    The style goes into meta. A few excerpts are known near-duplicates (Sturm 2014); they are kept.
    """
    root = Path(root)
    audio = {f.stem: f for f in (root / "audio").rglob("*.wav") if f.is_file() and not f.name.startswith(".")}
    items = []
    for sid, wav in sorted(audio.items()):
        truth = Truth()
        bpm_file = root / "tempo" / f"{sid}.bpm"
        if bpm_file.is_file():
            try:
                truth.bpm = float(bpm_file.read_text().split()[0])
            except (ValueError, IndexError):
                pass
        beats_file = root / "beats" / f"{sid}.beats"
        if beats_file.is_file():
            times, pos = [], []
            for line in beats_file.read_text().splitlines():
                cols = line.split()
                if len(cols) >= 2:
                    times.append(float(cols[0]))
                    pos.append(int(float(cols[1])))
            if times:
                truth.beats = times
                truth.downbeats = [t for t, n in zip(times, pos) if n == 1] or None
                truth.beats_per_bar = _bar_length(pos)
        if truth.bpm is None and truth.beats is None:
            continue
        items.append(Item(id=f"ballroom-{sid}", path=str(wav), truth=truth, meta={"style": wav.parent.name}))
    return items


def _bar_length(pos: list[int]) -> int | None:
    """Beats per bar when at least 90% of the labeled bars have the same length, else None."""
    from collections import Counter

    ones = [i for i, n in enumerate(pos) if n == 1]
    lengths = [pos[i - 1] for i in ones[1:]]
    if len(lengths) < 2:
        return None
    m, count = Counter(lengths).most_common(1)[0]
    return m if count >= 0.9 * len(lengths) else None


def beatport_tempo(root: str | Path) -> list[Item]:
    """Beatport EDM Key previews with the BPM Beatport lists for each track.

    Reads <root>/meta/<id> <name>.json (from original_metadata.zip in Zenodo record 1101082)
    and matches audio named "<id> <name>.mp3" anywhere under <root>, as beatport_key does.
    Tracks with a missing, zero or NaN bpm are skipped. Beatport's bpm is the DJ tempo, and
    drum & bass is often listed at half time (~87), see docs/bench.md.
    """
    root = Path(root)
    audio = {}
    for f in root.rglob("*"):
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS and not f.name.startswith("._"):
            audio[f.name.split(" ")[0]] = f
    items = []
    for f in sorted((root / "meta").glob("*.json")):
        if f.name.startswith("._"):
            continue
        meta = json.loads(re.sub(r":\s*nan\b", ": null", f.read_text()))  # some files have a bare nan
        bpm = meta.get("bpm")
        sid = str(meta.get("id") or f.name.split(" ")[0])
        if not bpm or sid not in audio:
            continue
        items.append(Item(id=f"bp-tempo-{sid}", path=str(audio[sid]), truth=Truth(bpm=float(bpm))))
    return items


def giantsteps_tempo(root: str | Path) -> list[Item]:
    """GiantSteps Tempo: 664 EDM previews with a global BPM.

    Uses <root>/annotations_v2/tempo/*.bpm when present (the corrected labels),
    else <root>/annotations/tempo/*.bpm. Audio named <id>.LOFI.mp3 under <root>.
    """
    root = Path(root)
    audio = _audio_by_stem(root)
    ann = root / "annotations_v2" / "tempo"
    if not ann.is_dir():
        ann = root / "annotations" / "tempo"
    items = []
    for f in sorted(ann.glob("*.bpm")):
        sid = f.name.split(".")[0]
        try:
            bpm = float(f.read_text().split()[0])
        except (ValueError, IndexError):
            continue
        if sid in audio:
            items.append(Item(id=f"gs-tempo-{sid}", path=str(audio[sid]), truth=Truth(bpm=bpm)))
    return items


def harmonix(root: str | Path, audio_dir: str | Path | None = None) -> list[Item]:
    """Harmonix Set: beats, downbeats, sections and BPM for 912 pop/EDM tracks.

    Annotations come from the dataset repo (<root>/dataset/beats_and_downbeats,
    <root>/dataset/segments, <root>/dataset/metadata.csv). The audio is not
    distributed with it; put your copies in `audio_dir` (default <root>/audio)
    named by the track id, e.g. 0001_12step.mp3.
    """
    root = Path(root)
    ds = root / "dataset" if (root / "dataset").is_dir() else root
    audio = _audio_by_stem(Path(audio_dir) if audio_dir else root / "audio")
    bpms = {}
    meta = ds / "metadata.csv"
    if meta.exists():
        with meta.open(newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    bpms[row["File"]] = float(row["BPM"])
                except (KeyError, ValueError):
                    pass
    items = []
    for f in sorted((ds / "beats_and_downbeats").glob("*.txt")):
        sid = f.stem
        if sid not in audio:
            continue
        beats, downbeats = [], []
        for line in f.read_text().splitlines():
            cols = line.split()
            if len(cols) >= 2:
                t = float(cols[0])
                beats.append(t)
                if cols[1] in ("1", "1.0"):
                    downbeats.append(t)
        boundaries, labels = [], []
        seg = ds / "segments" / f"{sid}.txt"
        if seg.exists():
            for line in seg.read_text().splitlines():
                cols = line.split(maxsplit=1)
                if len(cols) == 2 and cols[1].strip().lower() != "end":
                    boundaries.append(float(cols[0]))
                    labels.append(cols[1].strip())
        truth = Truth(bpm=bpms.get(sid), beats=beats, downbeats=downbeats,
                      boundaries=boundaries or None, labels=labels or None)
        items.append(Item(id=f"harmonix-{sid}", path=str(audio[sid]), truth=truth))
    return items


def manifest(path: str | Path) -> list[Item]:
    """Your own labeled tracks, from a CSV or JSON file.

    Columns / keys (all but `path` optional):
      path        audio file, absolute or relative to the manifest
      bpm         e.g. 124
      key         e.g. "A minor", "Am", "8A"
      boundaries  section starts, e.g. "0:00 0:32 1:04" (spaces or semicolons)
      labels      matching section names, e.g. "intro build drop"
    """
    path = Path(path)
    if path.suffix.lower() == ".json":
        rows = json.loads(path.read_text())
    else:
        with path.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
    items = []
    for i, row in enumerate(rows):
        audio = Path(str(row["path"])).expanduser()
        if not audio.is_absolute():
            audio = path.parent / audio
        key = parse_key(str(row["key"])) if row.get("key") else None
        b = row.get("boundaries")
        if isinstance(b, str):
            b = [parse_time(x) for x in re.split(r"[;\s,]+", b) if x]
        labels = row.get("labels")
        if isinstance(labels, str):
            labels = [x for x in re.split(r"[;\s,]+", labels) if x]
        truth = Truth(bpm=float(row["bpm"]) if row.get("bpm") else None,
                      tonic=key[0] if key else None, mode=key[1] if key else None,
                      boundaries=b or None, labels=labels or None)
        items.append(Item(id=row.get("id") or audio.stem or f"row-{i}", path=str(audio), truth=truth))
    return items


DATASETS: dict[str, Callable[..., list[Item]]] = {
    "synth": synth,
    "giantsteps-key": giantsteps_key,
    "giantsteps-tempo": giantsteps_tempo,
    "beatport-key": beatport_key,
    "fma-keys": fma_keys,
    "ballroom": ballroom,
    "beatport-tempo": beatport_tempo,
    "harmonix": harmonix,
    "manifest": manifest,
}


def load(name: str, path: str | None = None, **kw) -> list[Item]:
    if name not in DATASETS:
        raise ValueError(f"unknown dataset {name!r}; choose from {', '.join(DATASETS)}")
    if name == "synth":
        return synth(**kw)
    if not path:
        raise ValueError(f"dataset {name!r} needs a path")
    return DATASETS[name](path, **kw)
