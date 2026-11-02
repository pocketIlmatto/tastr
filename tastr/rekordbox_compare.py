"""Compare the analysis already stored in a Tastr library with Rekordbox's, for the tracks both have.

No re-analysis: Tastr's BPM, key and beat grid come from the library, Rekordbox's from its collection XML
(tastr/engine/bench/rekordbox.py). Tracks are matched by file name, then by artist and title. Where you labeled a
track in Tastr, both apps are also scored against your label. `tastr rekordbox-compare` (docs/rekordbox.md).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .engine.bench import metrics
from .engine.bench.datasets import parse_key
from .engine.bench.rekordbox import RbTrack, find_xml, parse_xml
from .engine.bench.runner import summarize
from .engine.music import note_name

DURATION_SLACK = 3.0  # seconds; an artist + title match must also be about as long


def _norm(text: str | None) -> str:
    """'Night Shift (Extended Mix)' and 'night shift extended mix' compare equal."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _file_keys(path: str | None) -> list[str]:
    if not path:
        return []
    name = Path(path).name.lower()
    return list(dict.fromkeys([name, name.rsplit(".", 1)[0]]))


def _tastr_tracks(conn: sqlite3.Connection, owner: str | None) -> list[dict[str, Any]]:
    where, args = ("WHERE t.owner = ?", (owner,)) if owner else ("", ())
    rows = conn.execute(
        "SELECT t.id, t.title, t.artist, t.duration, t.local_path, t.source_uri, t.bpm, t.key_tonic, t.key_mode, "
        f"t.key_name, l.bpm AS label_bpm, l.key_name AS label_key FROM tracks t "
        f"LEFT JOIN track_labels l ON l.track_id = t.id {where} ORDER BY t.id", args).fetchall()
    return [dict(r) for r in rows]


def match(tastr: list[dict], rb: list[RbTrack]) -> list[tuple[dict, RbTrack, str]]:
    """(Tastr track, Rekordbox track, how matched) pairs. Each track is used once; ambiguous names are skipped."""
    def index(items, keys_of):
        out: dict[str, Any] = {}
        for it in items:
            for k in keys_of(it):
                out[k] = None if k in out else it
        return out

    rb_files = index(rb, lambda t: _file_keys(t.location))
    rb_names = index(rb, lambda t: [f"{_norm(t.artist)}|{_norm(t.name)}"] if _norm(t.name) else [])
    pairs, used = [], set()
    for t in tastr:
        hit, how = None, ""
        for k in _file_keys(t["local_path"]) + _file_keys(t["source_uri"]):
            if rb_files.get(k) is not None:
                hit, how = rb_files[k], "file name"
                break
        if hit is None and _norm(t["title"]):
            cand = rb_names.get(f"{_norm(t['artist'])}|{_norm(t['title'])}")
            if cand is not None and (not cand.duration or not t["duration"]
                                     or abs(cand.duration - t["duration"]) <= DURATION_SLACK):
                hit, how = cand, "artist and title"
        if hit is not None and id(hit) not in used:
            used.add(id(hit))
            pairs.append((t, hit, how))
    return pairs


def _beats(conn: sqlite3.Connection, track_id: int) -> tuple[list[float], list[float]]:
    r = conn.execute("SELECT json FROM cards WHERE track_id = ?", (track_id,)).fetchone()
    core = json.loads(r[0]).get("core", {}) if r else {}
    return core.get("beat_times") or [], core.get("downbeat_times") or []


def compare_pair(conn: sqlite3.Connection, t: dict, rb: RbTrack) -> dict:
    """One matched track: Tastr scored with Rekordbox as the reference, plus both against your label if any."""
    row: dict = {"track_id": t["id"], "title": " - ".join(x for x in (t["artist"], t["title"]) if x),
                 "tastr_bpm": t["bpm"], "rekordbox_bpm": rb.bpm, "tastr_key": t["key_name"],
                 "rekordbox_key": f"{note_name(rb.tonic)} {rb.mode}" if rb.tonic is not None else None}
    if rb.bpm:
        row.update(metrics.tempo_scores(t["bpm"], rb.bpm))
    if rb.tonic is not None and rb.mode:
        row.update(metrics.key_scores(t["key_tonic"], t["key_mode"], rb.tonic, rb.mode))
    beats, downbeats = _beats(conn, t["id"])
    if rb.beats and beats:
        row.update(metrics.beat_scores(beats, rb.beats, "beat"))
    if rb.downbeats and downbeats:
        row.update(metrics.beat_scores(downbeats, rb.downbeats, "downbeat"))
    return row


def _label_rows(t: dict, rb: RbTrack) -> tuple[dict, dict] | None:
    """(Tastr's, Rekordbox's) scores against your label, or None when the track has none."""
    key = parse_key(t["label_key"]) if t["label_key"] else None
    if not t["label_bpm"] and not key:
        return None
    out = []
    for bpm, tonic, mode in ((t["bpm"], t["key_tonic"], t["key_mode"]), (rb.bpm, rb.tonic, rb.mode)):
        r: dict = {"id": t["id"]}
        if t["label_bpm"]:
            r.update(metrics.tempo_scores(bpm, t["label_bpm"]))
        if key:
            r.update(metrics.key_scores(tonic, mode, *key))
        out.append(r)
    return out[0], out[1]


def compare(conn: sqlite3.Connection, xml: str | Path | bytes, owner: str | None = None) -> dict:
    """`xml`: the export's path (or a folder holding it), or its contents. `owner`: only that person's tracks."""
    tastr = _tastr_tracks(conn, owner)
    rb = parse_xml(xml if isinstance(xml, bytes) else find_xml(xml))
    pairs = match(tastr, rb)
    rows = [compare_pair(conn, t, r) | {"matched_by": how} for t, r, how in pairs]
    labeled = [x for x in (_label_rows(t, r) for t, r, _ in pairs) if x]
    disagree = sorted((r for r in rows if r.get("acc1") == 0.0 or r.get("key_correct") == 0.0),
                      key=lambda r: (r.get("acc2", 1.0), r.get("key_weighted", 1.0)))
    return {
        "tastr_tracks": len(tastr), "rekordbox_tracks": len(rb), "matched": len(rows),
        "matched_by": {how: sum(1 for *_, h in pairs if h == how) for how in ("file name", "artist and title")},
        "agreement": summarize(rows),
        "labeled": {"tracks": len(labeled), "tastr": summarize([a for a, _ in labeled]),
                    "rekordbox": summarize([b for _, b in labeled])} if labeled else None,
        "disagreements": disagree,
        "rows": rows,
    }


AGREE_LABELS = {"acc1": "Same BPM (within 4%)", "acc2": "Same BPM incl. x2 / half", "key_correct": "Same key",
                "key_weighted": "Key (MIREX weighted)", "beat_f": "Beats line up (70 ms)",
                "downbeat_f": "Bar starts line up (70 ms)"}


def format_report(rep: dict, show: int = 15) -> str:
    a = rep["agreement"]
    lines = [f"{rep['matched']} tracks in both ({rep['tastr_tracks']} in Tastr, {rep['rekordbox_tracks']} analyzed in "
             f"Rekordbox; matched by " + ", ".join(f"{k} {v}" for k, v in rep["matched_by"].items()) + ")",
             "How often Tastr agrees with Rekordbox:"]
    for m, label in AGREE_LABELS.items():
        if m in a:
            lines.append(f"  {label:28} {a[m]:.3f}  n={a[m + '_n']}")
    if a.get("key_categories"):
        lines.append("  key differences (Tastr vs Rekordbox): " + ", ".join(f"{k} {v}" for k, v in a["key_categories"].items()))
    if a.get("tempo_octave_errors"):
        lines.append("  BPM ratios (Tastr / Rekordbox): " + ", ".join(f"{k} {v}" for k, v in a["tempo_octave_errors"].items()))
    lab = rep["labeled"]
    if lab:
        lines.append(f"Against your labels ({lab['tracks']} tracks):  Rekordbox   Tastr")
        for m in ("acc1", "acc2", "key_correct", "key_weighted"):
            if m in lab["rekordbox"]:
                lines.append(f"  {AGREE_LABELS[m]:28} {lab['rekordbox'][m]:9.3f} {lab['tastr'][m]:7.3f}  n={lab['rekordbox'][m + '_n']}")
    if rep["disagreements"]:
        lines.append(f"Biggest disagreements ({len(rep['disagreements'])} in all; label these to see who's right):")
        for r in rep["disagreements"][:show]:
            lines.append(f"  #{r['track_id']} {r['title'][:50]}: Tastr {r['tastr_bpm']} {r['tastr_key']}, "
                         f"Rekordbox {r['rekordbox_bpm']} {r['rekordbox_key']}")
    return "\n".join(lines)
