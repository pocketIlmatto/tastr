"""Essentia classifier bench: do MTG's pre-trained classifier heads rank tracks the way listeners label them,
and do they beat the engine's own vibe measures on the same tracks? See docs/essentia.md.

Research only. The weights are MTG's, licensed for non-commercial use only (CC BY-NC-SA 4.0 per the models
page, CC BY-NC-ND 4.0 per its LICENSE file), so they are downloaded into the Modal image at build time and are
never committed. Essentia itself is AGPL-3.0 and is only installed in that image.

    rows = analyze(pick(items, 200))          # in the Modal image, which has Essentia and the weights
    print(format_report(score(rows)))

Each row holds the engine's vibe row (tastr.engine.bench.vibes.row_from_card) and every head's mean
prediction over the clip, so scoring and comparing needs no audio.

Training-set overlap: the MTG-Jamendo heads (mood/theme, instrument) were trained on MTG-Jamendo's split-0
training set, and the clean yes/no annotations only cover its split-0 test set, so only tracks with a yes/no
label are used from mtg-moods. The DEAM arousal/valence head was trained on DEAM, so it is never scored on it.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..ml_tags import (BASE_URL, EFFNET, EFFNET_HEADS, MUSICNN, MUSICNN_HEADS, Models,  # noqa: F401
                       download_command as _download_command, head_path as _head_path, model_paths)

MODELS_DIR = os.environ.get("TASTR_ESSENTIA_MODELS", "/opt/essentia-models")


def download_command(root: str = MODELS_DIR) -> str:
    return _download_command(root)


# --- Labels ----------------------------------------------------------------------------------------

# Yes/no taxonomies of the clean annotations beyond the six vibes.py keeps: (taxonomy, yes value, label).
EXTRA_BINARY = [("voice_instrumental", "voice", "voice"), ("tonal_atonal", "tonal", "tonal"),
                ("mood_electronic", "electronic", "electronic"), ("mood_acoustic", "acoustic", "acoustic"),
                ("gender", "female", "female")]


def extra_labels(root: str | Path) -> dict[str, dict[str, int]]:
    """{"mtg-<track id>": {label: 0/1}} for EXTRA_BINARY, from <root>/annotations-clean.tsv."""
    path = Path(root) / "annotations-clean.tsv"
    out: dict[str, dict[str, int]] = {}
    if not path.is_file():
        return out
    with path.open() as fh:
        next(fh, None)
        for line in fh:
            cols = line.rstrip("\n").split("\t")
            for t in cols[5:]:
                tax, _, votes = t.partition("---")
                for name, yes, label in EXTRA_BINARY:
                    if tax == name:
                        out.setdefault(f"mtg-{cols[0]}", {})[label] = int(votes.split(",")[0] == yes)
    return out


def held_out(items: list, dataset: str) -> list:
    """The items no head trained on: for mtg-moods, the tracks with a clean yes/no label (split-0 test set)."""
    if dataset != "mtg-moods":
        return items
    binary = {"danceable", "party", "aggressive", "relaxed", "happy", "sad"}
    return [it for it in items if binary & set(it.meta.get("labels", {}))]


def pick(items: list, n: int | None) -> list:
    """A fixed pseudo-random sample of n items (by a hash of the id), the same on every run."""
    items = sorted(items, key=lambda it: hashlib.md5(it.id.encode()).hexdigest())
    return items[:n] if n else items


# --- Running the models ----------------------------------------------------------------------------


def analyze(items: list, extra: dict | None = None, engine: bool = True) -> list[dict]:
    """One row per item: the engine's vibe row (when `engine`) plus every head's predictions."""
    from . import vibes

    models = Models(MODELS_DIR)
    rows = []
    for it in items:
        row = vibes._work(it) if engine else {"id": it.id, "labels": dict(it.meta.get("labels", {}))}
        row["labels"] = {**row.get("labels", {}), **(extra or {}).get(it.id, {})}
        try:
            row["heads"], row["head_seconds"] = models.predict(it.path)
        except Exception as e:  # one unreadable file shouldn't sink the run
            row.setdefault("errors", {})["heads"] = f"{type(e).__name__}: {e}"
        rows.append(row)
    return rows


# --- Scoring ---------------------------------------------------------------------------------------

# Head outputs checked against labels, next to the engine measure that answers the same question
# (None: the engine has no such measure). "head:class" names one output.
CHECKS: list[tuple[str, str, str | None]] = [
    ("danceability:danceable", "danceable", "danceability"),
    ("danceability:danceable", "party", "danceability"),
    ("mood_party:party", "party", "danceability"),
    ("mood_aggressive:aggressive", "aggressive", "energy"),
    ("mood_aggressive:aggressive", "aggressive", "tag:high energy"),
    ("mood_relaxed:relaxed", "relaxed", "tag:chill"),
    ("mood_happy:happy", "happy", "tag:euphoric"),
    ("mood_happy:happy", "happy", "tag:bright"),
    ("mood_sad:sad", "sad", "tag:melancholic"),
    ("mtg_jamendo_moodtheme:dark", "mood:dark", "tag:dark"),
    ("mtg_jamendo_moodtheme:energetic", "mood:energetic", "tag:driving"),
    ("mtg_jamendo_moodtheme:energetic", "mood:energetic", "energy"),
    ("mtg_jamendo_moodtheme:meditative", "mood:meditative", "tag:hypnotic"),
    ("mtg_jamendo_moodtheme:meditative", "mood:meditative", "tag:atmospheric"),
    ("mtg_jamendo_moodtheme:relaxing", "mood:relaxing", "tag:chill"),
    ("mtg_jamendo_moodtheme:melancholic", "mood:melancholic", "tag:melancholic"),
    ("mtg_jamendo_moodtheme:uplifting", "mood:uplifting", "tag:euphoric"),
    ("mtg_jamendo_moodtheme:party", "mood:party", "danceability"),
    ("voice_instrumental:voice", "voice", None),
    ("tonal_atonal:tonal", "tonal", None),
    ("mood_electronic:electronic", "electronic", None),
    ("mood_acoustic:acoustic", "acoustic", None),
    ("gender:female", "female", None),
    # DEAM ratings, Spearman. emoMusic and MuSe heads never saw DEAM; the DEAM head is scored on MTG only.
    ("emomusic:arousal", "arousal", "energy"),
    ("muse:arousal", "arousal", "energy"),
    ("mood_aggressive:aggressive", "arousal", "energy"),
    ("emomusic:valence", "valence", "tag:bright"),
    ("muse:valence", "valence", "tag:bright"),
    ("mood_happy:happy", "valence", "tag:bright"),
    ("mood_sad:sad", "valence", "tag:melancholic"),
    ("mtg_jamendo_moodtheme:dark", "valence", "tag:dark"),
    ("deam:arousal", "aggressive", "energy"),
    ("deam:valence", "happy", "tag:bright"),
]


def head_value(row: dict, name: str) -> float | None:
    head, _, cls = name.partition(":")
    return row.get("heads", {}).get(head, {}).get(cls)


def score(rows: list[dict]) -> dict:
    from . import vibes

    ok = [r for r in rows if "heads" in r]
    checks = []
    for head, label, engine in CHECKS:
        lab = [r for r in ok if label in r.get("labels", {})]
        if len(lab) < 10:
            continue
        hv, n = vibes.agreement(lab, "", label, values=[head_value(r, head) for r in lab])
        entry = {"head": head, "label": label, "n": n, "head_score": None if hv is None else round(hv, 3)}
        if engine and "bench" not in lab[0].get("errors", {}):
            ev, _ = vibes.agreement(lab, engine, label)
            entry |= {"engine": engine, "engine_score": None if ev is None else round(ev, 3)}
        entry["stat"] = "spearman" if label in vibes.CONTINUOUS else "auc"
        checks.append(entry)
    secs = [r["head_seconds"] for r in ok]
    timing = {}
    if secs:
        for k in ("load", "embed", "heads", "audio_seconds"):
            timing[k] = round(sum(s[k] for s in secs) / len(secs), 2)
    styles: dict[str, int] = {}
    for r in ok:
        g = r["heads"].get("genre_discogs400", {})
        for s in sorted(g, key=g.get, reverse=True)[:1]:
            styles[s] = styles.get(s, 0) + 1
    return {"tracks": len(rows), "failed": len(rows) - len(ok), "checks": checks, "mean_seconds": timing,
            "top_styles": dict(sorted(styles.items(), key=lambda kv: -kv[1])[:15])}


def format_report(report: dict) -> str:
    def f(v, stat):
        return "-" if v is None else (f"{v:+.3f}" if stat == "spearman" else f"{v:.3f}")

    lines = [f"{report['tracks']} tracks ({report['failed']} failed). AUC 0.5 = chance; Spearman 0 = none.",
             f"Mean seconds per clip: {report['mean_seconds']}"]
    for c in report["checks"]:
        eng = f"   engine {c['engine']:16} {f(c.get('engine_score'), c['stat'])}" if c.get("engine") else ""
        lines.append(f"  {c['head']:34} vs {c['label']:16} n={c['n']:<4} {c['stat']} {f(c['head_score'], c['stat'])}{eng}")
    lines.append(f"Top styles: {report['top_styles']}")
    return "\n".join(lines)
