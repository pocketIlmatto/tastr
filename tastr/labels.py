"""Your own labels: corrected or confirmed BPM, key and section starts, and the queue of tracks worth a look.

The engine is tuned on public datasets that are mostly EDM previews and short clips. Labels on the tracks
you actually use are the only check on full-length tracks, drops, and conventions like drum & bass at 174
vs 87. They live in the `track_labels` table, separate from the analysis, so re-analyzing never touches
them. `tastr labels export` writes them as a `tastr bench manifest` file, so every engine change can be
scored against them as well.

The review queue (`tastr review`, the app's Review tab) ranks unlabeled tracks by how likely their analysis is
wrong: a low "likely right" score on BPM or key, or a catalog listing (tastr.lookup) that disagrees with it.
You can also put a track in the list yourself or take one out (the `review_flags` table).
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import db

BPM_RANGE = (30.0, 300.0)


class LabelError(ValueError):
    """A label that can't be stored as given."""


def clean(labels: dict[str, Any]) -> dict[str, Any]:
    """Check and normalize label fields: bpm a number in BPM_RANGE, key one parse_key reads, boundaries as
    seconds or m:ss separated by spaces, commas or semicolons. Empty strings mean "not labeled"."""
    from .engine.bench.datasets import parse_key, parse_time

    out: dict[str, Any] = {}
    bpm = labels.get("bpm")
    if bpm not in (None, ""):
        try:
            bpm = float(bpm)
        except (TypeError, ValueError):
            raise LabelError(f"BPM {bpm!r} isn't a number") from None
        if not BPM_RANGE[0] <= bpm <= BPM_RANGE[1]:
            raise LabelError(f"BPM {bpm:g} is outside {BPM_RANGE[0]:g}-{BPM_RANGE[1]:g}")
        out["bpm"] = bpm
    key = str(labels.get("key_name") or labels.get("key") or "").strip()
    if key:
        if parse_key(key) is None:
            raise LabelError(f"key {key!r} isn't one Tastr reads (try \"A minor\", \"Am\" or \"8A\")")
        out["key_name"] = key
    b = labels.get("boundaries")
    if isinstance(b, (list, tuple)):
        b = " ".join(str(x) for x in b)
    if b not in (None, ""):
        try:
            times = sorted(parse_time(x) for x in re.split(r"[;\s,]+", str(b).strip()) if x)
        except ValueError:
            raise LabelError(f"section starts {b!r} aren't seconds or m:ss") from None
        out["boundaries"] = " ".join(f"{t:g}" for t in times)
    if labels.get("note"):
        out["note"] = str(labels["note"]).strip()[:500]
    if labels.get("kept") and ("bpm" in out or "key_name" in out):
        out["kept"] = True  # "Keep as is": Tastr's own reading, confirmed
    return out


def export_manifest(conn: sqlite3.Connection, out: str | Path, owner: str | None = None) -> int:
    """Write every labeled track with its audio on disk as a `tastr bench manifest` CSV; returns the count."""
    rows = [r for r in db.list_labels(conn, owner) if r["path"] and Path(r["path"]).is_file()]
    with Path(out).open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "path", "bpm", "key", "boundaries"])
        for r in rows:
            w.writerow([f"track-{r['track_id']}", str(Path(r["path"]).resolve()),
                        f"{r['bpm']:g}" if r["bpm"] else "", r["key_name"] or "", r["boundaries"] or ""])
    return len(rows)


# Key names as the review list spells them: the Camelot wheel's spellings (Bb major, C# minor), in ASCII.
_MAJOR = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
_MINOR = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


def key_label(tonic: int, mode: str) -> str:
    return f"{(_MINOR if mode == 'minor' else _MAJOR)[tonic % 12]} {mode}"


def _bpm_text(v: float) -> str:
    return f"{round(v, 1):g}"


def _catalog_reasons(card, res: dict[str, Any], cmp: dict[str, Any]) -> list[tuple[str, str, Any]]:
    """("bpm" | "key", reason, the catalog's value) for each way a catalog listing disagrees with the card. The
    reason spells out the catalog's value, e.g. "freqblog lists 61.1 BPM, half Tastr's"."""
    from .engine.music import camelot
    from .lookup import parse_listed_key

    out = []
    who, ours = res["provider"], card.core
    if cmp["bpm"] not in (None, "agrees"):
        how = {"half": "half Tastr's", "double": "double Tastr's"}.get(cmp["bpm"], f"not Tastr's {_bpm_text(ours.bpm)}")
        out.append(("bpm", f"{who} lists {_bpm_text(res['bpm'])} BPM, {how}", round(res["bpm"], 1)))
    if cmp["key"] not in (None, "agrees"):
        listed = (res["tonic"], res["mode"]) if res.get("tonic") is not None and res.get("mode") else \
            parse_listed_key(res.get("key"), res.get("camelot"))
        cam = camelot(*listed)
        tastr = key_label(ours.key.tonic, ours.key.mode)
        d = (listed[0] - ours.key.tonic) % 12
        how = {"relative": "the relative key: same notes, other mode",
               "parallel": f"the parallel key of Tastr's {tastr}: same root, other mode",
               "fifth": f"a fifth {'above' if d == 7 else 'below'} Tastr's {tastr}"}.get(cmp["key"], f"not Tastr's {tastr}")
        out.append(("key", f"{who} lists {key_label(*listed)} ({cam}), {how}", cam))
    return out


def _score_reasons(card, settled: bool) -> tuple[list[str], list[float]]:
    """Why a low "likely right" score puts the track in review, and the scores themselves."""
    reasons, scores = [], []
    key_p = card.core.key.likely_correct if card.core.key else None
    for name, p in (("BPM", card.core.bpm_likely_correct), ("key", key_p)):
        if p is not None:
            scores.append(p)
            if p < 0.6 and not settled:
                reasons.append(f"{name} {round(100 * p)}% likely right")
    return reasons, scores


def _catalog_disagreements(conn: sqlite3.Connection, track_id: int, card) -> tuple[list[str], Any, Any]:
    """(a reason per catalog disagreement, the first disagreeing catalog BPM, the first disagreeing Camelot key)."""
    from . import lookup

    reasons, first = [], {"bpm": None, "key": None}
    for r in db.get_lookups(conn, track_id):
        if r.get("status") != "found":
            continue
        for what, why, value in _catalog_reasons(card, r, lookup.compare(card, r)):
            reasons.append(why)
            if first[what] is None:
                first[what] = value
    return reasons, first["bpm"], first["key"]


def _queue_row(conn: sqlite3.Connection, track_id: int, card, marks: list[dict[str, Any]], flag: str | None,
               settled: bool, labeled: bool) -> dict[str, Any] | None:
    """The track's review_queue entry, or None when nothing puts it there."""
    pat_reasons, way_off = pattern_reasons(_with_status(card, marks))
    score_reasons, scores = _score_reasons(card, settled)
    cat_reasons, cat_bpm, cat_key = _catalog_disagreements(conn, track_id, card) if not settled else ([], None, None)
    mine = flag == "flagged"
    reasons = pat_reasons + score_reasons + cat_reasons
    if mine:
        reasons.insert(0, "You put this back in review (it has a label)" if labeled else "You added this to review")
    if not reasons:
        return None
    k = card.core.key
    return {"track_id": track_id, "title": card.source.title, "artist": card.source.artist,
            "bpm": card.core.bpm, "key": k.name if k else None, "camelot": k.camelot if k else None,
            "chance_right": min(scores) if scores else None, "disagreements": len(cat_reasons), "reasons": reasons,
            "mine": mine, "catalog_bpm": cat_bpm, "catalog_key": cat_key, "way_off": way_off}


def review_queue(conn: sqlite3.Connection, limit: int = 20, owner: str | None = None) -> list[dict[str, Any]]:
    """Tracks most worth a look, most suspect first. Tracks you put in the list yourself come first (`mine`,
    labeled or not); ones you took out never show. The rest are unlabeled tracks with a low "likely right" score
    on BPM or key, or a catalog listing that disagrees. `chance_right` is the lower of the BPM and key "likely
    right" scores (None on cards analyzed before calibration); `reasons` says why each is listed, and
    `catalog_bpm` / `catalog_key` (Camelot) are what a disagreeing catalog lists.

    Pattern reviews add tracks too, labeled or not: a part you marked Way off and haven't saved the right pattern
    for, or a mark a re-analysis made stale (pattern_reasons). Way-off tracks come right after yours; `way_off`
    counts their sections."""
    labeled = {r["track_id"] for r in db.list_labels(conn, owner)}
    flags = db.review_flags(conn, owner)
    marks: dict[int, list[dict[str, Any]]] = {}
    for r in db.list_pattern_reviews(conn, owner):
        marks.setdefault(r["track_id"], []).append(r)
    out = []
    for t in db.list_tracks(conn, owner):
        flag = flags.get(t["id"])
        settled = t["id"] in labeled and flag != "flagged"  # its BPM and key are labeled: only pattern reasons count
        if flag == "skipped" or (settled and t["id"] not in marks):
            continue
        card = db.load_card(conn, t["id"])
        if card is not None and (row := _queue_row(conn, t["id"], card, marks.get(t["id"], []), flag, settled,
                                                   t["id"] in labeled)):
            out.append(row)
    out.sort(key=lambda r: (not r["mine"], -r["way_off"], -r["disagreements"],
                            r["chance_right"] if r["chance_right"] is not None else 1.0))
    return out[:limit]


# ---------- pattern reviews: your verdict on the generated drums, bass and chords, per section ----------
# The engine writes one pattern per part and section (the suggestions' data.by_section; an older card, or a part
# without by_section, has one pattern for every section). You mark each Right, Close or Way off, and can save your
# edit as the right pattern. Reviews are keyed by the section's bars, so a re-analysis keeps them; the pattern you
# judged is stored too, so a re-analysis that changes it marks the review stale and the track goes back in review.

PART_NAMES = {"drums": "Drums", "bass": "Bass", "chords": "Chords"}
VERDICT_NAMES = {"right": "right", "close": "close", "way_off": "way off"}
NO_SECTIONS = (0, 0)  # the bars a review is stored under on a card without sections


def _pattern_sugs(card) -> dict[str, Any]:
    """The suggestion behind each part, found the way the app's Drums, bass & chords view finds them."""
    out: dict[str, Any] = {}
    for s in card.suggestions:
        d = s.data or {}
        if d.get("origin") == "template":
            continue
        if "drums" not in out and d.get("origin") == "detected" and isinstance(d.get("pattern"), dict):
            out["drums"] = s
        elif "bass" not in out and s.category == "bass" and isinstance(d.get("loop"), list):
            out["bass"] = s
        elif "chords" not in out and s.category == "harmony" and isinstance(d.get("progression"), list):
            out["chords"] = s
    return out


def _shape(part: str, d: dict[str, Any]) -> dict[str, Any]:
    if part == "drums":
        return {k: v for k, v in (d.get("pattern") or {}).items() if isinstance(v, str)}
    if part == "bass":
        return {"notes": list(d.get("loop") or []), "rhythm": d.get("rhythm") or None}
    return {"chords": list(d.get("progression") or [])}


def detected_patterns(card) -> dict[tuple[str, int, int], dict[str, Any]]:
    """{(part, start_bar, end_bar): {"section": {idx, label, letter} or None, "pattern": ...}} for each part and
    section the card has a pattern for. Patterns are drums {lane: steps}, bass {notes, rhythm}, chords {chords}."""
    sugs = _pattern_sugs(card)
    by_sec = {p: s.data.get("by_section") for p, s in sugs.items() if s.data.get("by_section")}
    secs = next(iter(by_sec.values()), None)
    out: dict[tuple[str, int, int], dict[str, Any]] = {}
    for part, sug in sugs.items():
        if not secs:
            out[(part, *NO_SECTIONS)] = {"section": None, "pattern": _shape(part, sug.data)}
            continue
        mine = {x.get("idx"): x for x in by_sec.get(part) or []}
        for x in secs:
            if x.get("start_bar") is None or x.get("end_bar") is None:
                continue
            src = mine.get(x.get("idx")) or sug.data  # a part without by_section plays its one loop everywhere
            out[(part, x["start_bar"], x["end_bar"])] = {
                "section": {"idx": x.get("idx"), "label": x.get("label"), "letter": x.get("letter")},
                "pattern": _shape(part, src)}
    return out


STEPS = re.compile(r"[x.o]{1,32}")


def _clean_drums(v: dict[str, Any]) -> dict[str, Any]:
    if not all(isinstance(s, str) and STEPS.fullmatch(s) for s in v.values()):
        raise LabelError("drum lanes are step strings like \"x...x...\"")
    return {str(k): s for k, s in v.items()}


def _clean_seq(part: str, v: dict[str, Any]) -> list[str | None]:
    what = "notes" if part == "bass" else "chords"
    seq = v.get(what)
    if not isinstance(seq, list) or len(seq) > 16 or not all(x is None or isinstance(x, str) for x in seq):
        raise LabelError(f"the {part} need a list of up to 16 {what}")
    return [x or None for x in seq]


def _clean_truth(part: str, v: Any) -> dict[str, Any]:
    """A right pattern as the app sends it (its edit), in the stored shape."""
    if not isinstance(v, dict):
        raise LabelError("the right pattern must be an object")
    if part == "drums":
        return _clean_drums(v)
    seq = _clean_seq(part, v)
    if part == "chords":
        return {"chords": seq}
    rhythm = v.get("rhythm")
    if rhythm is not None and not (isinstance(rhythm, str) and STEPS.fullmatch(rhythm)):
        raise LabelError("the bass rhythm is a step string like \"x.......x.......\"")
    return {"notes": seq, "rhythm": rhythm}


def save_pattern_review(conn: sqlite3.Connection, track_id: int, body: dict[str, Any]) -> list[dict[str, Any]]:
    """Store {part, section (its idx in card.sections, or "all"), verdict?, truth?, note?}: a verdict on the detected
    pattern, the right pattern (your edit, or null to drop it), or both. Neither removes the review. Returns the
    track's reviews (pattern_reviews)."""
    card = db.load_card(conn, track_id)
    if card is None:
        raise LabelError(f"no track {track_id}")
    part = body.get("part")
    if part not in db.PATTERN_PARTS:
        raise LabelError(f"part must be one of {', '.join(db.PATTERN_PARTS)}")
    verdict = body.get("verdict") or None
    if verdict is not None and verdict not in db.PATTERN_VERDICTS:
        raise LabelError(f"verdict must be one of {', '.join(db.PATTERN_VERDICTS)} or null")
    found = detected_patterns(card)
    sec = body.get("section", "all")
    if sec in (None, "all"):
        bars = NO_SECTIONS
    else:
        try:
            s = card.sections[int(sec)]
        except (TypeError, ValueError, IndexError):
            raise LabelError(f"no section {sec!r} on this track") from None
        bars = (s.start_bar, s.end_bar)
    det = found.get((part, *bars))
    if det is None:
        raise LabelError(f"this track has no generated {part} for that section")
    truth = body.get("truth")
    review = {"verdict": verdict, "truth": _clean_truth(part, truth) if truth is not None else None,
              "judged": det["pattern"], "stage_version": card.stages_run.get("suggestions"),
              "note": str(body["note"]).strip()[:500] if body.get("note") else None}
    db.set_pattern_review(conn, track_id, part, *bars, review)
    return pattern_reviews(conn, track_id, card)


def _with_status(card, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each review plus `section` (where it is now, or None once the section is gone) and `stale` (the pattern it
    judged isn't what the card has now: a re-analysis changed it, or its section is gone)."""
    found = detected_patterns(card) if card else {}
    for r in rows:
        det = found.get((r["part"], r["start_bar"], r["end_bar"]))
        r["section"] = det["section"] if det else None
        r["stale"] = det is None or det["pattern"] != r["judged"]
    return rows


def pattern_reviews(conn: sqlite3.Connection, track_id: int, card=None) -> list[dict[str, Any]]:
    """A track's pattern reviews, each with `section` and `stale` (see _with_status)."""
    return _with_status(card or db.load_card(conn, track_id), db.get_pattern_reviews(conn, track_id))


def all_pattern_reviews(conn: sqlite3.Connection, owner: str | None = None) -> list[dict[str, Any]]:
    """Every pattern review (db.list_pattern_reviews) with `section` and `stale`, oldest first."""
    rows, cards, out = db.list_pattern_reviews(conn, owner), {}, []
    for r in rows:
        tid = r["track_id"]
        if tid not in cards:
            cards[tid] = db.load_card(conn, tid)
        out.extend(_with_status(cards[tid], [r]))
    return out


def _where(r: dict[str, Any]) -> str:
    s = r.get("section")
    if s:
        return f" in {' '.join(x for x in (str(s.get('label') or 'section').capitalize(), s.get('letter')) if x)}"
    return "" if (r["start_bar"], r["end_bar"]) == NO_SECTIONS else f" in bars {r['start_bar']}-{r['end_bar']}"


def pattern_reasons(rows: list[dict[str, Any]]) -> tuple[list[str], int]:
    """Why a track's pattern reviews put it in the review list, and how many sections are marked Way off: a Way off
    part with no right pattern saved yet, or a mark a re-analysis made stale."""
    reasons, way_off = [], 0
    for r in rows:
        name = PART_NAMES[r["part"]]
        if r["stale"]:
            reasons.append(f"{name}{_where(r)} changed since you marked it" if r["section"]
                           else f"{name} you marked in bars {r['start_bar']}-{r['end_bar']}: that section is gone")
        elif r["verdict"] == "way_off" and r["truth"] is None:
            way_off += 1
            reasons.append(f"{name} way off{_where(r)}")
    return reasons, way_off


def export_patterns(conn: sqlite3.Connection, out: str | Path, owner: str | None = None) -> int:
    """Write every pattern review as JSON for a pattern bench: one object per track with its audio path and its
    reviews (verdict, the right pattern if saved, the pattern judged and the stage version). Returns the count of
    reviews written."""
    tracks: dict[int, dict[str, Any]] = {}
    for r in all_pattern_reviews(conn, owner):
        t = tracks.setdefault(r["track_id"], {"id": f"track-{r['track_id']}", "title": r["title"], "artist": r["artist"],
                                              "path": str(Path(r["path"]).resolve()) if r["path"] else None, "reviews": []})
        t["reviews"].append({k: r[k] for k in ("part", "start_bar", "end_bar", "verdict", "truth", "judged",
                                               "stage_version", "stale", "note", "updated_at")})
    Path(out).write_text(json.dumps(list(tracks.values()), indent=1) + "\n")
    return sum(len(t["reviews"]) for t in tracks.values())
