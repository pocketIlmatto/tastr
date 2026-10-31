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


def review_queue(conn: sqlite3.Connection, limit: int = 20, owner: str | None = None) -> list[dict[str, Any]]:
    """Tracks most worth a look, most suspect first. Tracks you put in the list yourself come first (`mine`,
    labeled or not); ones you took out never show. The rest are unlabeled tracks with a low "likely right" score
    on BPM or key, or a catalog listing that disagrees. `chance_right` is the lower of the BPM and key "likely
    right" scores (None on cards analyzed before calibration); `reasons` says why each is listed, and
    `catalog_bpm` / `catalog_key` (Camelot) are what a disagreeing catalog lists."""
    from . import lookup

    labeled = {r["track_id"] for r in db.list_labels(conn, owner)}
    flags = db.review_flags(conn, owner)
    out = []
    for t in db.list_tracks(conn, owner):
        flag = flags.get(t["id"])
        if flag == "skipped" or (t["id"] in labeled and flag != "flagged"):
            continue
        card = db.load_card(conn, t["id"])
        if card is None:
            continue
        reasons, scores = [], []
        bpm_p = card.core.bpm_likely_correct
        key_p = card.core.key.likely_correct if card.core.key else None
        for name, p in (("BPM", bpm_p), ("key", key_p)):
            if p is not None:
                scores.append(p)
                if p < 0.6:
                    reasons.append(f"{name} {round(100 * p)}% likely right")
        disagree, cat_bpm, cat_key = 0, None, None
        for r in db.get_lookups(conn, t["id"]):
            if r.get("status") != "found":
                continue
            for what, why, value in _catalog_reasons(card, r, lookup.compare(card, r)):
                disagree += 1
                reasons.append(why)
                if what == "bpm" and cat_bpm is None:
                    cat_bpm = value
                if what == "key" and cat_key is None:
                    cat_key = value
        mine = flag == "flagged"
        if mine:
            reasons.insert(0, "You put this back in review (it has a label)" if t["id"] in labeled else "You added this to review")
        if not reasons:
            continue
        k = card.core.key
        out.append({"track_id": t["id"], "title": card.source.title, "artist": card.source.artist,
                    "bpm": card.core.bpm, "key": k.name if k else None, "camelot": k.camelot if k else None,
                    "chance_right": min(scores) if scores else None, "disagreements": disagree, "reasons": reasons,
                    "mine": mine, "catalog_bpm": cat_bpm, "catalog_key": cat_key})
    out.sort(key=lambda r: (not r["mine"], -r["disagreements"],
                            r["chance_right"] if r["chance_right"] is not None else 1.0))
    return out[:limit]
