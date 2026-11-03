"""Rekordbox's analysis, read from a collection XML export, for comparing Tastr with it.

Rekordbox (File > Export Collection in xml format) writes one TRACK per file with its BPM (`AverageBpm`), key
(`Tonality`, "Am" or "8A" depending on the key display setting), beat grid (`TEMPO` anchors) and cue points
(`POSITION_MARK`). Phrase analysis, vocal positions and the colored waveform are not in the XML (they live in
the encrypted database and the ANLZ files), so they are not compared here.

Two uses (docs/rekordbox.md):

- `rekordbox_items(xml)`: the `rekordbox` dataset, which treats Rekordbox's answers as the reference, so a bench
  run says how often Tastr agrees with Rekordbox on your own library.
- `score(items, xml)`: on a labeled dataset whose audio was also analyzed in Rekordbox, score Rekordbox's answers
  against the dataset's labels with the same metrics as Tastr, for a head-to-head on the same tracks.

Nothing here runs Rekordbox; it only reads what it exported.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..audio import AUDIO_EXTENSIONS
from . import metrics
from .datasets import DATASETS, Item, Truth, parse_key


@dataclass
class RbTrack:
    """One track's analysis as Rekordbox exported it."""

    location: str  # the file path on the machine that ran Rekordbox
    name: str = ""
    artist: str = ""
    duration: float | None = None
    bpm: float | None = None
    key_text: str = ""
    tonic: int | None = None
    mode: str | None = None
    beats: list[float] = field(default_factory=list)
    downbeats: list[float] = field(default_factory=list)
    beats_per_bar: int | None = None
    grid_anchors: int = 0  # more than one: a dynamic (variable tempo) grid
    hot_cues: list[float] = field(default_factory=list)
    memory_cues: list[float] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return Path(self.location).name


def _float(text: str | None) -> float | None:
    try:
        return float(text) if text not in (None, "") else None
    except ValueError:
        return None


def _location(url: str) -> str:
    """'file://localhost/Users/me/Music/A%20B.mp3' -> '/Users/me/Music/A B.mp3' (Windows drive paths too)."""
    if not url.startswith("file:"):
        return unquote(url)
    path = unquote(urlparse(url).path)
    if len(path) > 2 and path[0] == "/" and path[2] == ":":  # /C:/Music/x.mp3
        path = path[1:]
    return path


def expand_grid(anchors: list[tuple[float, float, int, int]], end: float) -> tuple[list[float], list[float]]:
    """Beat and downbeat times from TEMPO anchors (start s, bpm, beats per bar, beat number of the anchor).

    Each anchor's tempo holds until the next anchor; the last holds to `end`. Beat numbers count on from each
    anchor's `Battito`, so a downbeat is a beat whose number is 1.
    """
    beats, downbeats = [], []
    anchors = sorted(a for a in anchors if a[1] > 0)
    for i, (start, bpm, per_bar, number) in enumerate(anchors):
        stop = anchors[i + 1][0] if i + 1 < len(anchors) else end
        step = 60.0 / bpm
        k = 0
        t = start
        while t < stop - 1e-6:
            beats.append(round(t, 4))
            if (number - 1 + k) % per_bar == 0:
                downbeats.append(round(t, 4))
            k += 1
            t = start + k * step
    return beats, downbeats


def parse_xml(path: str | Path | bytes) -> list[RbTrack]:
    """Every track in a Rekordbox collection XML (a file, or its contents) that has a BPM or key (i.e. was analyzed)."""
    root = ET.fromstring(path) if isinstance(path, bytes) else ET.parse(str(path)).getroot()
    tracks = []
    for el in root.iterfind("./COLLECTION/TRACK"):
        loc = el.get("Location")
        if not loc:
            continue
        duration = _float(el.get("TotalTime"))
        key_text = (el.get("Tonality") or "").strip()
        key = parse_key(key_text) if key_text else None
        anchors = []
        meters = set()
        for t in el.iterfind("TEMPO"):
            start, bpm = _float(t.get("Inizio")), _float(t.get("Bpm"))
            if start is None or not bpm:
                continue
            metro = (t.get("Metro") or "4/4").split("/")[0]
            per_bar = int(metro) if metro.isdigit() and int(metro) > 0 else 4
            meters.add(per_bar)
            number = int(_float(t.get("Battito")) or 1)
            anchors.append((start, bpm, per_bar, number))
        end = duration if duration else (max(a[0] for a in anchors) + 600 if anchors else 0)
        beats, downbeats = expand_grid(anchors, end) if anchors else ([], [])
        hot, memory = [], []
        for m in el.iterfind("POSITION_MARK"):
            start = _float(m.get("Start"))
            if start is None or m.get("Type", "0") != "0":  # plain cues only, not loops / fades
                continue
            (memory if m.get("Num", "-1") == "-1" else hot).append(start)
        bpm = _float(el.get("AverageBpm"))
        if not bpm and not key:
            continue  # not analyzed
        tracks.append(RbTrack(
            location=_location(loc), name=el.get("Name") or "", artist=el.get("Artist") or "", duration=duration,
            bpm=bpm or None, key_text=key_text, tonic=key[0] if key else None, mode=key[1] if key else None,
            beats=beats, downbeats=downbeats, beats_per_bar=meters.pop() if len(meters) == 1 else None,
            grid_anchors=len(anchors), hot_cues=sorted(hot), memory_cues=sorted(memory)))
    return tracks


def find_xml(path: str | Path) -> Path:
    """The export itself, or a folder holding it (`rekordbox.xml`, else its only .xml file)."""
    path = Path(path).expanduser()
    if not path.is_dir():
        return path
    if (path / "rekordbox.xml").is_file():
        return path / "rekordbox.xml"
    found = sorted(path.glob("*.xml"))
    if len(found) != 1:
        raise FileNotFoundError(f"put the Rekordbox export in {path} as rekordbox.xml")
    return found[0]


# --- Matching Rekordbox tracks to audio -------------------------------------------


def _keys(name: str) -> list[str]:
    """Names a file is matched by: the whole file name, then everything before the first dot."""
    name = name.lower()
    return [name, name.split(".")[0]]


def match(tracks: list[RbTrack], paths: list[str | None]) -> dict[int, RbTrack]:
    """Index into `paths` -> the Rekordbox track for that file, matched by file name (the folders differ between
    the machine that ran Rekordbox and the bench). Two Rekordbox tracks with the same name count as one when
    Rekordbox gave them the same BPM and key (one file imported twice, e.g. a track in two datasets' folders);
    otherwise that name is left unmatched."""
    def same(a: RbTrack, b: RbTrack) -> bool:
        return (a.bpm, a.tonic, a.mode) == (b.bpm, b.tonic, b.mode)

    by_name: dict[str, RbTrack | None] = {}
    clashed: set[str] = set()
    for t in tracks:
        for k in dict.fromkeys(_keys(t.filename)):
            if k in clashed:
                continue
            if k in by_name and not same(by_name[k], t):
                by_name[k] = None
                clashed.add(k)
            else:
                by_name.setdefault(k, t)
    out = {}
    for i, p in enumerate(paths):
        if not p:
            continue
        for k in _keys(Path(p).name):
            if by_name.get(k):
                out[i] = by_name[k]
                break
    return out


# --- The `rekordbox` dataset: Rekordbox as the reference -------------------------------


def rekordbox_items(path: str | Path, audio_dir: str | Path | None = None) -> list[Item]:
    """Your tracks, with Rekordbox's BPM, key, beat grid and meter as the labels.

    `path` is the XML export, or a folder holding it as `rekordbox.xml`. Each track's audio is its `Location` when that file exists here (the same Mac);
    otherwise a file of the same name under `audio_dir` (default: the XML's folder and below), which is how a
    copy uploaded to the dataset bucket is found.
    """
    path = find_xml(path)
    tracks = parse_xml(path)
    local = {}
    root = Path(audio_dir) if audio_dir else path.parent
    if root.is_dir():
        for f in root.rglob("*"):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                for k in _keys(f.name):
                    local.setdefault(k, f)
    items = []
    for i, t in enumerate(tracks):
        audio = Path(t.location) if Path(t.location).is_file() else next(
            (local[k] for k in _keys(t.filename) if k in local), None)
        if audio is None:
            continue
        truth = Truth(bpm=t.bpm, tonic=t.tonic, mode=t.mode, beats=t.beats or None,
                      downbeats=t.downbeats or None, beats_per_bar=t.beats_per_bar)
        label = " - ".join(x for x in (t.artist, t.name) if x) or Path(t.location).stem
        items.append(Item(id=f"rb-{i:04d}-{label[:60]}", path=str(audio), truth=truth,
                          meta={"rekordbox_key": t.key_text, "grid_anchors": t.grid_anchors}))
    return items


# --- Rekordbox vs the labels, next to Tastr -------------------------------------


def score_track(item: Item, t: RbTrack) -> dict:
    """Rekordbox's answers for one labeled track, scored like `runner.score_item` scores Tastr's."""
    from ..music import note_name

    truth = item.truth
    row: dict = {"id": item.id, "est_bpm": t.bpm,
                 "est_key": f"{note_name(t.tonic)} {t.mode}" if t.tonic is not None else None}
    if truth.bpm:
        row.update(metrics.tempo_scores(t.bpm, truth.bpm))
    if truth.tonic is not None and truth.mode:
        row.update(metrics.key_scores(t.tonic, t.mode, truth.tonic, truth.mode))
    if truth.beats and t.beats:
        row.update(metrics.beat_scores(t.beats, truth.beats, "beat"))
    if truth.downbeats and t.downbeats:
        row.update(metrics.beat_scores(t.downbeats, truth.downbeats, "downbeat"))
    if truth.beats_per_bar in (3, 4) and t.beats_per_bar:
        row["meter_correct"] = float(t.beats_per_bar == truth.beats_per_bar)
        row["ref_meter"] = truth.beats_per_bar
    return row


def score(items: list[Item], xml: str | Path, tastr_rows: list[dict] | None = None) -> dict:
    """Rekordbox's scores on the dataset's tracks it analyzed, and Tastr's on exactly those tracks.

    Returns {"matched", "unmatched", "rekordbox": summary, "tastr": summary, "rows": [...]}. Tastr's summary is
    only there when its rows from the same run are passed. Sections are not compared: Rekordbox's phrases are
    not in the XML.
    """
    from .runner import summarize

    tracks = parse_xml(find_xml(xml))
    found = match(tracks, [it.path for it in items])
    rb_rows = [score_track(items[i], t) for i, t in sorted(found.items())]
    out: dict = {"xml_tracks": len(tracks), "matched": len(found), "unmatched": len(items) - len(found),
                 "rekordbox": summarize(rb_rows), "rows": rb_rows}
    if tastr_rows is not None:
        ids = {r["id"] for r in rb_rows}
        same = [r for r in tastr_rows if r.get("id") in ids]
        out["tastr"] = summarize(same)
    return out


def add_to_report(report: dict, items: list[Item], xml: str | Path, limit: int | None = None) -> dict:
    """Score Rekordbox's exported analysis of the run's tracks next to it, as report["rekordbox"]."""
    report["rekordbox"] = score(items[:limit] if limit else items, xml, report["rows"])
    return report


def format_comparison(cmp: dict) -> str:
    """The head-to-head as a small table: one line per metric both sides were scored on."""
    from .runner import LABELS, SUMMARY_METRICS

    rb, ta = cmp["rekordbox"], cmp.get("tastr", {})
    lines = [f"Rekordbox vs Tastr on the {cmp['matched']} tracks both analyzed "
             f"({cmp['unmatched']} of the dataset's tracks aren't in the XML)",
             f"  {'':24} {'Rekordbox':>10} {'Tastr':>8}"]
    for m in SUMMARY_METRICS:
        if m in rb:
            t = f"{ta[m]:8.3f}" if m in ta else f"{'-':>8}"
            lines.append(f"  {LABELS[m]:24} {rb[m]:10.3f} {t}  n={rb[m + '_n']}")
    for name, s in (("Rekordbox", rb), ("Tastr", ta)):
        if s.get("key_categories"):
            lines.append(f"  {name} key results: " + ", ".join(f"{k} {v}" for k, v in s["key_categories"].items()))
        if s.get("tempo_octave_errors"):
            lines.append(f"  {name} BPM octave errors: " + ", ".join(f"{k} {v}" for k, v in s["tempo_octave_errors"].items()))
    return "\n".join(lines)


DATASETS["rekordbox"] = rekordbox_items
