"""Vibe benchmark: do danceability, energy and the mood-like vibe tags rank tracks the way listeners
label them? And which weights would rank them better? See docs/vibes.md.

    rows = analyze(load("mtg-moods", "~/data/mtg-moods"), jobs=4)
    print(format_report(score(rows)))
    print(format_fit(fit(rows)))

Two datasets, both fetched by `tastr fetch` (tastr/cloud/fetch.py):

- `mtg-moods`: MTG-Jamendo tracks with the clean music-classification annotations (danceable, party,
  aggressive, relaxed, happy, sad; yes/no where all three annotators agreed) and the uploaders'
  mood/theme tags (dark, energetic, relaxing, ...). Layout: <root>/annotations-clean.tsv,
  <root>/moodtheme.tsv, audio at <root>/audio/<PATH from the TSVs>.
- `deam`: DEAM clips with listeners' arousal and valence (1-9, averaged over the clip).
  Layout: <root>/annotations/*static_annotations_averaged_songs*.csv, audio <song_id>.mp3 anywhere under <root>.

Rows keep the raw inputs of every weighted sum (loudness, onset rate, brightness, percussiveness, pulse
clarity, BPM), so `fit` refits the weights in seconds without touching audio again.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from .datasets import Item, Truth, _audio_by_stem

STAGES = ["core", "sections", "tags"]

# The clean annotation file's yes/no taxonomies, and the label each is stored under.
MTG_BINARY = {"danceability": ("danceable", "danceable"), "mood_party": ("party", "party"),
              "mood_aggressive": ("aggressive", "aggressive"), "mood_relaxed": ("relaxed", "relaxed"),
              "mood_happy": ("happy", "happy"), "mood_sad": ("sad", "sad")}
# Uploader mood/theme tags worth checking against a Tastr measure. A track in the mood/theme set without
# a tag counts as a "no", which is noisy (uploaders tag sparsely), so these are only scored by ranking.
MTG_MOODS = ["energetic", "relaxing", "calm", "dark", "sad", "melancholic", "happy", "uplifting", "party",
             "ambiental", "soundscape", "groovy", "fast", "slow", "heavy", "powerful", "meditative", "upbeat"]

# Which labels each Tastr measure is meant to agree with. A tag missing from a track's scores counts as 0.
CHECKS: dict[str, list[str]] = {
    "danceability": ["danceable", "party", "mood:party", "mood:groovy"],
    "energy": ["arousal", "aggressive", "mood:energetic", "mood:powerful", "relaxed", "mood:calm"],
    "tag:danceable": ["danceable", "party"],
    "tag:high energy": ["arousal", "aggressive", "mood:energetic"],
    "tag:chill": ["relaxed", "mood:relaxing", "mood:calm"],
    "tag:melancholic": ["sad", "mood:sad", "mood:melancholic", "valence"],
    "tag:dark": ["mood:dark", "valence"],
    "tag:bright": ["happy", "valence"],
    "tag:euphoric": ["happy", "mood:uplifting", "valence"],
    "tag:atmospheric": ["mood:ambiental", "mood:soundscape", "mood:meditative"],
    "tag:driving": ["mood:energetic", "danceable"],
    "tag:hypnotic": ["mood:meditative", "danceable"],
}
CONTINUOUS = ("arousal", "valence")  # scored by rank correlation; the rest by ROC AUC


# --- Datasets --------------------------------------------------------------------------------------


def _read_tsv(path: Path) -> list[tuple[str, str, float, list[str]]]:
    """(track id, PATH, DURATION, tags) per row of an MTG-Jamendo TSV."""
    rows = []
    with path.open() as fh:
        next(fh, None)
        for line in fh:
            cols = line.rstrip("\n").split("\t")
            if len(cols) >= 5:
                rows.append((cols[0], cols[3], float(cols[4] or 0), cols[5:]))
    return rows


def mtg_labels(root: str | Path) -> dict[str, dict]:
    """{track id: {"path", "duration", "labels"}} from the two TSVs, audio or not."""
    root = Path(root)
    out: dict[str, dict] = {}
    clean = root / "annotations-clean.tsv"
    if clean.exists():
        for tid, path, dur, tags in _read_tsv(clean):
            labels = {}
            for t in tags:
                tax, _, votes = t.partition("---")
                if tax in MTG_BINARY:
                    name, yes = MTG_BINARY[tax]
                    labels[name] = int(votes.split(",")[0] == yes)
            if labels:
                out.setdefault(tid, {"path": path, "duration": dur, "labels": {}})["labels"].update(labels)
    moods = root / "moodtheme.tsv"
    if moods.exists():
        for tid, path, dur, tags in _read_tsv(moods):
            have = {t.split("---", 1)[1] for t in tags if t.startswith("mood/theme---")}
            entry = out.setdefault(tid, {"path": path, "duration": dur, "labels": {}})
            entry["labels"].update({f"mood:{m}": int(m in have) for m in MTG_MOODS})
    return out


def mtg_moods(root: str | Path) -> list[Item]:
    root = Path(root)
    items = []
    for tid, e in sorted(mtg_labels(root).items()):
        audio = root / "audio" / e["path"]
        if audio.is_file():
            items.append(Item(id=f"mtg-{tid}", path=str(audio), truth=Truth(), meta={"labels": e["labels"]}))
    return items


def deam(root: str | Path) -> list[Item]:
    root = Path(root)
    labels: dict[str, dict] = {}
    for f in sorted(root.rglob("*static_annotations_averaged_songs*.csv")):
        with f.open(newline="") as fh:
            for row in csv.DictReader(fh):
                row = {k.strip(): v.strip() for k, v in row.items() if k}
                try:
                    labels[row["song_id"]] = {"arousal": float(row["arousal_mean"]), "valence": float(row["valence_mean"])}
                except (KeyError, ValueError):
                    pass
    audio = _audio_by_stem(root)
    return [Item(id=f"deam-{sid}", path=str(audio[sid]), truth=Truth(), meta={"labels": lab})
            for sid, lab in sorted(labels.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0) if sid in audio]


DATASETS = {"mtg-moods": mtg_moods, "deam": deam}


def load(name: str, path: str | Path) -> list[Item]:
    if name not in DATASETS:
        raise ValueError(f"unknown vibe dataset {name!r}; choose from {', '.join(DATASETS)}")
    return DATASETS[name](path)


# --- Analysis --------------------------------------------------------------------------------------


def row_from_card(item: Item, card) -> dict:
    """The measures Tastr shows and the raw inputs of their weighted sums."""
    c, f = card.core, card.features
    return {
        "id": item.id, "labels": item.meta.get("labels", {}), "errors": dict(card.errors),
        "bpm": c.bpm, "mode": c.key.mode if c.key else None, "loudness_db": c.loudness_db, "energy": c.energy,
        "features": {k: f[k] for k in ("brightness", "percussiveness", "pulse_clarity", "danceability", "onset_rate",
                                       "low_end", "section_contrast") if k in f},
        "tags": dict(card.extras.get("tags.vibe_scores", {})),
    }


def _work(item: Item) -> dict:
    from .runner import analyze_item

    t0 = time.time()
    try:
        row = row_from_card(item, analyze_item(item, STAGES))
    except Exception as e:  # one unreadable file shouldn't sink the run
        row = {"id": item.id, "labels": item.meta.get("labels", {}), "errors": {"bench": f"{type(e).__name__}: {e}"}}
    row["seconds"] = round(time.time() - t0, 2)
    return row


def analyze(items: list[Item], jobs: int = 1, limit: int | None = None, progress=None) -> list[dict]:
    items = items[:limit] if limit else items
    rows: list[dict] = []
    if not items:
        return rows
    rows.append(_work(items[0]))  # compiles librosa's numba kernels once before workers share the cache
    if progress:
        progress(len(rows), len(items), rows[-1])
    if jobs > 1:
        with ProcessPoolExecutor(jobs) as pool:
            for row in pool.map(_work, items[1:]):
                rows.append(row)
                if progress:
                    progress(len(rows), len(items), row)
    else:
        for it in items[1:]:
            rows.append(_work(it))
            if progress:
                progress(len(rows), len(items), rows[-1])
    return rows


# --- Scoring ---------------------------------------------------------------------------------------


def auc(scores, labels) -> float | None:
    """ROC AUC: the chance a random "yes" track scores above a random "no" one (ties count half)."""
    from scipy.stats import rankdata

    s, y = np.asarray(scores, float), np.asarray(labels, int)
    n1, n0 = int(y.sum()), int((1 - y).sum())
    if n1 == 0 or n0 == 0:
        return None
    r = rankdata(s)
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def spearman(a, b) -> float | None:
    from scipy.stats import spearmanr

    a, b = np.asarray(a, float), np.asarray(b, float)
    if a.size < 3 or np.std(a) == 0 or np.std(b) == 0:
        return None
    return float(spearmanr(a, b).statistic)


def measure(row: dict, name: str) -> float | None:
    if name.startswith("tag:"):
        return float(row["tags"].get(name[4:], 0.0)) if "tags" in row else None
    if name == "energy":
        return row.get("energy")
    return row.get("features", {}).get(name)


def agreement(rows: list[dict], name: str, label: str, values=None) -> tuple[float | None, int]:
    """AUC (yes/no labels) or Spearman (arousal, valence) of a measure against a label, and n."""
    pairs = [(values[i] if values is not None else measure(r, name), r["labels"][label])
             for i, r in enumerate(rows) if label in r.get("labels", {})]
    pairs = [(m, y) for m, y in pairs if m is not None]
    if not pairs:
        return None, 0
    m, y = zip(*pairs)
    return (spearman(m, y) if label in CONTINUOUS else auc(m, y)), len(pairs)


def shown_precision(rows: list[dict], tag: str, label: str, cut: float) -> tuple[float | None, float | None, int]:
    """Of the tracks showing `tag` (score >= cut): the share labeled yes, the share overall, and how many."""
    lab = [r for r in rows if label in r.get("labels", {}) and "tags" in r]
    if not lab:
        return None, None, 0
    shown = [r for r in lab if r["tags"].get(tag, 0.0) >= cut]
    base = sum(r["labels"][label] for r in lab) / len(lab)
    return (sum(r["labels"][label] for r in shown) / len(shown) if shown else None), base, len(shown)


def score(rows: list[dict]) -> dict:
    ok = [r for r in rows if "bench" not in r.get("errors", {})]
    checks = []
    for name, labels in CHECKS.items():
        for label in labels:
            v, n = agreement(ok, name, label)
            if n and v is not None:
                entry = {"measure": name, "label": label, "n": n, ("spearman" if label in CONTINUOUS else "auc"): round(v, 3)}
                if name.startswith("tag:") and label not in CONTINUOUS:
                    prec, base, shown = shown_precision(ok, name[4:], label, 0.2)
                    entry |= {"shown": shown, "precision": None if prec is None else round(prec, 3), "base_rate": round(base, 3)}
                checks.append(entry)
    return {"tracks": len(rows), "failed": len(rows) - len(ok), "checks": checks}


def format_report(report: dict) -> str:
    lines = [f"{report['tracks']} tracks ({report['failed']} failed). AUC 0.5 = chance, 1 = perfect ranking; "
             "below 0.5 ranks the wrong way round."]
    for c in report["checks"]:
        stat = f"Spearman {c['spearman']:+.3f}" if "spearman" in c else f"AUC {c['auc']:.3f}"
        extra = ""
        if "shown" in c:
            prec = "-" if c["precision"] is None else f"{c['precision']:.2f}"
            extra = f"   shown on {c['shown']}, {prec} of them yes (base {c['base_rate']:.2f})"
        lines.append(f"  {c['measure']:18} vs {c['label']:16} {stat}  n={c['n']}{extra}")
    return "\n".join(lines)


# --- Refitting -------------------------------------------------------------------------------------

# The engine's current formulas (tastr/engine/stages/core.py), rebuilt from a row's raw inputs.
ENERGY_INPUTS = ["loudness", "onset_rate", "brightness", "percussiveness"]
ENERGY_WEIGHTS = (0.35, 0.25, 0.20, 0.20)
DANCE_INPUTS = ["pulse_clarity", "tempo_fit", "percussiveness"]
DANCE_WEIGHTS = (0.55, 0.25, 0.20)
TEMPO_CENTER, TEMPO_WIDTH = 122.0, 30.0


def energy_inputs(row: dict) -> list[float] | None:
    f = row.get("features", {})
    if row.get("loudness_db") is None or not {"onset_rate", "brightness", "percussiveness"} <= f.keys():
        return None
    return [float(np.clip((row["loudness_db"] + 30) / 22, 0, 1)), float(np.clip(f["onset_rate"] / 6.0, 0, 1)),
            float(f["brightness"]), float(np.clip(f["percussiveness"] / 0.5, 0, 1))]


def dance_inputs(row: dict, center: float = TEMPO_CENTER, width: float = TEMPO_WIDTH) -> list[float] | None:
    f = row.get("features", {})
    if not {"pulse_clarity", "percussiveness"} <= f.keys():
        return None
    bpm = row.get("bpm")
    tempo_fit = float(np.exp(-(((bpm - center) / width) ** 2))) if bpm else 0.0
    return [float(f["pulse_clarity"]), tempo_fit, float(np.clip(f["percussiveness"] / 0.5, 0, 1))]


def in_train(row_id: str) -> bool:
    """A fixed 70/30 split by id, so every fit and report holds out the same tracks."""
    return int(hashlib.md5(row_id.encode()).hexdigest(), 16) % 10 < 7


def simplex(k: int, step: float = 0.05):
    """Every k weights in multiples of `step` that are >= 0 and sum to 1."""
    n = round(1 / step)
    for cut in itertools.combinations(range(n + k - 1), k - 1):
        parts = np.diff([-1, *cut, n + k - 1]) - 1
        yield tuple(round(p * step, 4) for p in parts)


def _agree(X, rows, weights, label) -> float | None:
    v, _ = agreement(rows, "", label, values=list(np.asarray(X) @ np.asarray(weights)))
    return v


def fit_weights(rows: list[dict], inputs, label: str, baseline, settings=((),), base_setting=()) -> dict | None:
    """Best weights on the simplex for agreement with `label` on the train split, with held-out scores.

    `inputs(row, *setting)` returns the measure's raw inputs for one of `settings` (e.g. the tempo bell's
    center and width for danceability); today's formula is `baseline` weights at `base_setting`.
    """
    rows = [r for r in rows if label in r.get("labels", {}) and "bench" not in r.get("errors", {})]
    train = [r for r in rows if in_train(r["id"])]
    test = [r for r in rows if not in_train(r["id"])]
    if len(train) < 20 or len(test) < 10:
        return None
    best = None
    for s in settings:
        tr = [(r, inputs(r, *s)) for r in train]
        tr = [(r, x) for r, x in tr if x is not None]
        X, R = [x for _, x in tr], [r for r, _ in tr]
        for w in simplex(len(X[0])):
            v = _agree(X, R, w, label)
            if v is not None and (best is None or v > best[0]):
                best = (v, w, s)

    def held_out(w, s):
        te = [(r, inputs(r, *s)) for r in test]
        te = [(r, x) for r, x in te if x is not None]
        return _agree([x for _, x in te], [r for r, _ in te], w, label), len(te)

    base_s = base_setting
    base_test, n_test = held_out(baseline, base_s)
    tr = [(r, inputs(r, *base_s)) for r in train]
    tr = [(r, x) for r, x in tr if x is not None]
    return {"label": label, "n_train": len(tr), "n_test": n_test,
            "baseline": {"weights": list(baseline), "setting": list(base_s),
                         "train": _agree([x for _, x in tr], [r for r, _ in tr], baseline, label), "test": base_test},
            "fitted": {"weights": list(best[1]), "setting": list(best[2]), "train": best[0], "test": held_out(best[1], best[2])[0]}}


TEMPO_GRID = [(c, w) for c in (100.0, 110.0, 115.0, 122.0, 128.0, 135.0) for w in (15.0, 30.0, 45.0, 60.0)]


def fit(rows: list[dict]) -> list[dict]:
    """Refit danceability and energy against every label that applies to them."""
    out = []
    for label in ("danceable", "party"):
        r = fit_weights(rows, dance_inputs, label, DANCE_WEIGHTS, TEMPO_GRID, (TEMPO_CENTER, TEMPO_WIDTH))
        if r:
            out.append({"measure": "danceability", "inputs": DANCE_INPUTS + ["tempo bell (center, width)"]} | r)
    for label in ("arousal", "aggressive", "mood:energetic"):
        r = fit_weights(rows, energy_inputs, label, ENERGY_WEIGHTS)
        if r:
            out.append({"measure": "energy", "inputs": ENERGY_INPUTS} | r)
    return out


def format_fit(results: list[dict]) -> str:
    if not results:
        return "not enough labeled tracks to fit (needs 20 train and 10 held-out)"

    def fmt(v):
        return "-" if v is None else f"{v:.3f}"

    lines = ["Weights fitted on 70% of the tracks; 'held out' is the other 30%, which the fit never saw."]
    for r in results:
        b, f = r["baseline"], r["fitted"]
        names = r["inputs"]
        lines.append(f"{r['measure']} vs {r['label']} (train {r['n_train']}, held out {r['n_test']}):")
        for tag, x in (("today ", b), ("fitted", f)):
            ws = ", ".join(f"{n} {w:.2f}" for n, w in zip(names, x["weights"]))
            setting = f", tempo bell {x['setting'][0]:g} +/- {x['setting'][1]:g}" if x["setting"] else ""
            lines.append(f"  {tag}  train {fmt(x['train'])}  held out {fmt(x['test'])}   {ws}{setting}")
    return "\n".join(lines)
