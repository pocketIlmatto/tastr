"""Optional ML tags: MTG's pre-trained Essentia classifiers on a track's audio (docs/essentia.md).

One Discogs-EffNet embedding (16 kHz mono) feeds the classifier heads: Discogs styles, yes/no moods,
MTG-Jamendo mood/theme tags and instruments, voice/instrumental, tonal/atonal; one MSD-MusiCNN embedding feeds
the emoMusic arousal/valence head. `analyze(path)` returns every head's output averaged over the track, and
`tags(result)` turns that into the tags the card can show.

Off unless TASTR_ML_TAGS=on, because of the licenses: MTG's weights are for non-commercial use only
(CC BY-NC-SA 4.0 per the models page, CC BY-NC-ND 4.0 per its LICENSE file) and Essentia is AGPL-3.0. Neither
is part of Tastr: Essentia is an optional install (`pip install 'tastr[ml-tags]'`), and the weights (about
40 MB) download on first use into TASTR_ESSENTIA_MODELS (default ~/.tastr/essentia-models), or are baked into
the Modal image.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
import urllib.request
from pathlib import Path

BASE_URL = "https://essentia.upf.edu/models"
VERSION = 1  # bump when the heads or the tag mapping change, so stored results can be told apart

EFFNET = "feature-extractors/discogs-effnet/discogs-effnet-bs64-1"
MUSICNN = "feature-extractors/musicnn/msd-musicnn-1"
# Heads on the Discogs-EffNet embedding.
EFFNET_HEADS = ["danceability", "mood_aggressive", "mood_happy", "mood_party", "mood_relaxed", "mood_sad",
                "mood_acoustic", "mood_electronic", "voice_instrumental", "gender", "tonal_atonal",
                "mtg_jamendo_moodtheme", "mtg_jamendo_instrument", "genre_discogs400", "engagement_regression",
                "approachability_regression"]
# Arousal/valence heads on the MSD-MusiCNN embedding (outputs [valence, arousal], 1-9).
MUSICNN_HEADS = {"emomusic": "emomusic-msd-musicnn-2", "muse": "muse-msd-musicnn-2", "deam": "deam-msd-musicnn-2"}
INSTALL_HINT = "ML tags need Essentia, an optional install: pip install 'tastr[ml-tags]'"


def head_path(head: str) -> str:
    folder = head.replace("_regression", "").replace("_2c", "").replace("_3c", "")
    return f"classification-heads/{folder}/{head}-discogs-effnet-1"


def model_paths() -> list[str]:
    """Every model's path under BASE_URL / models_dir(), without the .pb/.json suffix."""
    return ([EFFNET, MUSICNN] + [head_path(h) for h in EFFNET_HEADS]
            + [f"classification-heads/{k}/{v}" for k, v in MUSICNN_HEADS.items()])


def models_dir() -> Path:
    return Path(os.environ.get("TASTR_ESSENTIA_MODELS")
                or Path(os.environ.get("TASTR_HOME", Path.home() / ".tastr")) / "essentia-models")


def download_command(root: str) -> str:
    """One shell step that downloads every model's weights and metadata (about 40 MB) into `root`."""
    parts = [f"mkdir -p {root}"]
    for p in model_paths():
        for ext in (".pb", ".json"):
            parts.append(f"curl -sSfL --create-dirs -o {root}/{p}{ext} {BASE_URL}/{p}{ext}")
    return " && ".join(parts)


def switched_on() -> bool:
    """TASTR_ML_TAGS=on: this server offers ML tags. Off by default (the weights are non-commercial only)."""
    return os.environ.get("TASTR_ML_TAGS", "").strip().lower() in ("on", "1", "true", "yes")


def installed() -> bool:
    """Whether Essentia (with TensorFlow) is importable here."""
    return importlib.util.find_spec("essentia") is not None


def cached(root: Path | None = None) -> bool:
    root = root or models_dir()
    return all((root / f"{p}{ext}").is_file() for p in model_paths() for ext in (".pb", ".json"))


def download(root: Path | None = None) -> Path:
    """Fetch any missing weights into `root` (models_dir()); returns it."""
    root = root or models_dir()
    for p in model_paths():
        for ext in (".pb", ".json"):
            f = root / f"{p}{ext}"
            if not f.is_file():
                f.parent.mkdir(parents=True, exist_ok=True)
                tmp = f.with_suffix(f.suffix + ".part")
                with urllib.request.urlopen(f"{BASE_URL}/{p}{ext}", timeout=60) as r, tmp.open("wb") as out:
                    out.write(r.read())
                tmp.replace(f)
    return root


class Models:
    """Every model loaded once; `predict(path)` returns ({head: {class: mean output}}, timings)."""

    def __init__(self, root: Path | str | None = None):
        import essentia
        import essentia.standard as es

        essentia.log.warningActive = False
        self.root = Path(root) if root else models_dir()

        def meta(p):
            return json.loads((self.root / f"{p}.json").read_text())

        def out_name(m, purpose, default):
            for o in m.get("schema", {}).get("outputs", []):
                if o.get("output_purpose") == purpose:
                    return o["name"]
            return default

        def in_name(m, default):
            ins = m.get("schema", {}).get("inputs", [])
            return ins[0]["name"] if ins else default

        e, mc = meta(EFFNET), meta(MUSICNN)
        self.effnet = es.TensorflowPredictEffnetDiscogs(graphFilename=str(self.root / f"{EFFNET}.pb"),
                                                        output=out_name(e, "embeddings", "PartitionedCall:1"))
        self.musicnn = es.TensorflowPredictMusiCNN(graphFilename=str(self.root / f"{MUSICNN}.pb"),
                                                   output=out_name(mc, "embeddings", "model/dense/BiasAdd"))
        self.heads = {}
        for name, path, base in ([(h, head_path(h), "effnet") for h in EFFNET_HEADS]
                                 + [(k, f"classification-heads/{k}/{v}", "musicnn") for k, v in MUSICNN_HEADS.items()]):
            m = meta(path)
            algo = es.TensorflowPredict2D(graphFilename=str(self.root / f"{path}.pb"),
                                          input=in_name(m, "model/Placeholder"),
                                          output=out_name(m, "predictions", "model/Softmax"))
            self.heads[name] = (algo, m.get("classes", []), base)
        self.loader = es.MonoLoader

    def predict(self, path: str) -> tuple[dict, dict]:
        import numpy as np

        t0 = time.time()
        audio = self.loader(filename=str(path), sampleRate=16000, resampleQuality=4)()
        t1 = time.time()
        emb = {"effnet": self.effnet(audio), "musicnn": self.musicnn(audio)}
        t2 = time.time()
        out = {}
        for name, (algo, classes, base) in self.heads.items():
            p = np.asarray(algo(emb[base])).mean(axis=0)
            names = classes if len(classes) == len(p) else [str(i) for i in range(len(p))]
            out[name] = {c: round(float(v), 4) for c, v in zip(names, p)}
        return out, {"load": round(t1 - t0, 2), "embed": round(t2 - t1, 2), "heads": round(time.time() - t2, 2),
                     "audio_seconds": round(len(audio) / 16000, 1)}


_models: Models | None = None


def analyze(path: str | os.PathLike) -> dict:
    """Every head's output for one audio file, plus the tags they give. Downloads the weights on first use."""
    global _models
    if not installed():
        raise RuntimeError(INSTALL_HINT)
    if _models is None:
        if not cached():
            download()
        _models = Models()
    heads, seconds = _models.predict(str(path))
    result = {"version": VERSION, "heads": compact(heads), "seconds": seconds}
    result["tags"] = tags(result["heads"])
    return result


# --- From outputs to tags ------------------------------------------------------------------------
# Cut-offs from the small Essentia bench (docs/essentia.md, 2026-11-04): provisional until the full sets run.

TOP_KEEP = {"genre_discogs400": 10, "mtg_jamendo_instrument": 10}  # the rest of these big heads isn't stored
# Yes/no heads: (head, class, tag, cut). At 0.7 the shown tracks were 52-100% labeled yes (base rates 9-47%).
MOODS = [("mood_relaxed", "relaxed", "relaxed", 0.7), ("mood_happy", "happy", "happy", 0.7),
         ("mood_sad", "sad", "sad", 0.7), ("mood_aggressive", "aggressive", "aggressive", 0.7),
         ("mood_party", "party", "party", 0.7)]
# MTG-Jamendo mood/theme tags that describe the music (not where it's used, like "advertising" or "film").
MOODTHEME = {"calm", "cool", "dark", "deep", "dramatic", "dream", "emotional", "energetic", "epic", "fast", "fun",
             "groovy", "happy", "heavy", "hopeful", "inspiring", "melancholic", "meditative", "positive",
             "powerful", "relaxing", "retro", "romantic", "sad", "sexy", "slow", "soft", "space", "upbeat", "uplifting"}
MOODTHEME_CUT, MOODTHEME_MAX = 0.12, 3  # about the top 5% of these outputs
STYLE_CUT, STYLE_MAX = 0.15, 3  # the median track's top style scores 0.22, its third 0.11
VOICE_CUT = 0.7  # voice >= 0.7: "vocal"; <= 0.3: "instrumental" (0.82 precision at 0.7)


def compact(heads: dict) -> dict:
    """The big heads cut to their top entries, so a stored result stays a few KB."""
    out = {}
    for name, v in heads.items():
        keep = TOP_KEEP.get(name)
        out[name] = dict(sorted(v.items(), key=lambda kv: -kv[1])[:keep]) if keep else v
    return out


def style_name(label: str) -> str:
    """'Electronic---Deep House' -> 'Deep House'."""
    return label.split("---")[-1]


def tags(heads: dict) -> list[dict]:
    """[{tag, group, score}] from the heads' outputs: style, mood, mood/theme and voice."""
    out: list[dict] = []
    styles = sorted(heads.get("genre_discogs400", {}).items(), key=lambda kv: -kv[1])
    for label, s in styles[:STYLE_MAX]:
        if s >= STYLE_CUT or (not out and s >= 0.1):  # the top style counts from 0.1
            out.append({"tag": style_name(label), "group": "style", "score": round(s, 3), "label": label})
    for head, cls, tag, cut in MOODS:
        s = heads.get(head, {}).get(cls)
        if s is not None and s >= cut:
            out.append({"tag": tag, "group": "mood", "score": round(s, 3)})
    have = {t["tag"] for t in out}
    mt = sorted(((c, s) for c, s in heads.get("mtg_jamendo_moodtheme", {}).items() if c in MOODTHEME),
                key=lambda kv: -kv[1])
    for c, s in [kv for kv in mt if kv[1] >= MOODTHEME_CUT][:MOODTHEME_MAX]:
        tag = {"dream": "dreamy", "relaxing": "relaxed"}.get(c, c)
        if tag not in have:
            out.append({"tag": tag, "group": "mood", "score": round(s, 3)})
            have.add(tag)
    v = heads.get("voice_instrumental", {}).get("voice")
    if v is not None and (v >= VOICE_CUT or v <= 1 - VOICE_CUT):
        out.append({"tag": "vocal" if v >= VOICE_CUT else "instrumental", "group": "voice",
                    "score": round(v if v >= VOICE_CUT else 1 - v, 3)})
    return out
