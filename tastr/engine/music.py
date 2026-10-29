"""Music-theory helpers shared by the analyzer and the suggestion engine."""

from __future__ import annotations

import numpy as np

NOTE_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]

# Semitone offsets of each mode, and Ableton Live 12's name for that scale.
MODES = {
    "Major": [0, 2, 4, 5, 7, 9, 11],
    "Dorian": [0, 2, 3, 5, 7, 9, 10],
    "Phrygian": [0, 1, 3, 5, 7, 8, 10],
    "Lydian": [0, 2, 4, 6, 7, 9, 11],
    "Mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "Minor": [0, 2, 3, 5, 7, 8, 10],
    "Locrian": [0, 1, 3, 5, 6, 8, 10],
}
# Church-mode order, used to name the mode built on each degree of the major scale.
MODE_ROTATION = ["Major", "Dorian", "Phrygian", "Lydian", "Mixolydian", "Minor", "Locrian"]

ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII"]


def note_name(pc: int) -> str:
    return NOTE_NAMES[int(pc) % 12]


def camelot(tonic: int, mode: str) -> str:
    """Camelot wheel code (e.g. 8A = A minor, 8B = C major)."""
    major_pc = tonic if mode == "major" else (tonic + 3) % 12
    number = (7 * major_pc + 8) % 12 or 12
    return f"{number}{'B' if mode == 'major' else 'A'}"


# The key model's input is KEY_INPUT_SCALE * key_profile(chroma): the profile sums to about 1, so this
# puts each bin near 1 for the fitted weights (tastr.engine.bench.train_key uses the same scale).
KEY_INPUT_SCALE = 12.0


def key_profile(chroma: np.ndarray) -> np.ndarray:
    """Mean of the per-frame normalized chroma (12 x frames), so loud frames don't dominate the key."""
    chroma = np.asarray(chroma, dtype=float)
    return (chroma / (chroma.sum(axis=0, keepdims=True) + 1e-9)).mean(axis=1)


def key_probabilities(chroma_profile: np.ndarray, weights: np.ndarray | None = None,
                      bias: np.ndarray | None = None) -> np.ndarray:
    """Probability of each of the 24 keys (index tonic * 2 + 1 for minor) from a 12-bin key profile.

    A softmax over the learned key model (key_model.py, fitted on GiantSteps Key and Beatport EDM Key
    by tastr.engine.bench.train_key); pass `weights`/`bias` to try other fitted constants.
    """
    if weights is None or bias is None:
        from . import key_model

        weights, bias = key_model.WEIGHTS, key_model.BIAS
    z = weights @ (KEY_INPUT_SCALE * np.asarray(chroma_profile, dtype=float)) + bias
    p = np.exp(z - z.max())
    return p / p.sum()


def _key_name(index: int) -> str:
    return f"{note_name(index // 2)} {'minor' if index % 2 else 'major'}"


def estimate_key(chroma_profile: np.ndarray, weights: np.ndarray | None = None,
                 bias: np.ndarray | None = None) -> dict:
    """Most likely key of a 12-bin key profile under the learned key model, with the next three as alternatives."""
    profile = np.asarray(chroma_profile, dtype=float)
    if not np.any(profile > 0):
        return {"tonic": 0, "mode": "major", "probability": 0.0, "confidence": 0.0, "alternatives": []}
    p = key_probabilities(profile, weights, bias)
    order = np.argsort(-p)
    best, second = int(order[0]), int(order[1])
    return {
        "tonic": best // 2,
        "mode": "minor" if best % 2 else "major",
        "probability": round(float(p[best]), 3),
        # How far the best key is ahead of the runner-up, 0 (a coin flip) to 1 (no contest).
        "confidence": round(float(p[best] - p[second]), 3),
        "alternatives": [{"key": _key_name(int(i)), "probability": round(float(p[i]), 3)} for i in order[1:4]],
    }


def refine_mode(chroma_profile: np.ndarray, tonic: int, mode: str) -> str:
    """Guess a modal flavour (Dorian, Mixolydian...) from the scale degrees that stand out."""
    p = np.roll(np.asarray(chroma_profile, dtype=float), -tonic)
    p = p / (p.max() or 1.0)
    if mode == "minor":
        if p[1] > p[2] * 1.1 and p[1] > 0.35:
            return "Phrygian"
        if p[9] > p[8] * 1.15 and p[9] > 0.35:
            return "Dorian"
        if p[11] > p[10] * 1.2 and p[11] > 0.4:
            return "Harmonic Minor"
        return "Minor"
    if p[10] > p[11] * 1.15 and p[10] > 0.35:
        return "Mixolydian"
    if p[6] > p[5] * 1.15 and p[6] > 0.35:
        return "Lydian"
    return "Major"


def chord_templates() -> list[tuple[str, int, str, np.ndarray]]:
    """Major and minor triad templates for all 12 roots, root weighted slightly higher."""
    out = []
    for root in range(12):
        for quality, intervals in (("maj", (0, 4, 7)), ("min", (0, 3, 7))):
            t = np.zeros(12)
            for i, iv in enumerate(intervals):
                t[(root + iv) % 12] = 1.2 if i == 0 else 1.0
            name = note_name(root) + ("" if quality == "maj" else "m")
            out.append((name, root, quality, t / np.linalg.norm(t)))
    return out


_TEMPLATES = chord_templates()


def match_chord(chroma_vec: np.ndarray) -> dict:
    v = np.asarray(chroma_vec, dtype=float)
    norm = np.linalg.norm(v)
    if norm == 0:
        return {"name": "N", "root": None, "quality": None, "score": 0.0}
    v = v / norm
    best = max(_TEMPLATES, key=lambda t: float(t[3] @ v))
    return {"name": best[0], "root": best[1], "quality": best[2], "score": round(float(best[3] @ v), 3)}


def scale_pcs(tonic: int, scale: str) -> list[int]:
    if scale == "Harmonic Minor":
        steps = [0, 2, 3, 5, 7, 8, 11]
    else:
        steps = MODES.get(scale, MODES["Major"] if scale == "major" else MODES["Minor"])
    return [(tonic + s) % 12 for s in steps]


def degree_of(pc: int, tonic: int, scale: str) -> int | None:
    """0-based scale degree of a pitch class, or None if it is outside the scale."""
    pcs = scale_pcs(tonic, scale)
    return pcs.index(pc % 12) if pc % 12 in pcs else None


def roman_numeral(root: int, quality: str, tonic: int, scale: str) -> str:
    deg = degree_of(root, tonic, scale)
    if deg is None:
        # Outside the key: name it as a flat/sharp degree relative to the tonic.
        interval = (root - tonic) % 12
        labels = {1: "bII", 3: "bIII", 6: "#IV", 8: "bVI", 10: "bVII", 11: "VII"}
        base = labels.get(interval, "?")
    else:
        base = ROMAN[deg]
    return base if quality == "maj" else base.lower()


def chord_scale(root: int, quality: str, tonic: int, key_scale: str) -> str:
    """The mode that fits over a chord, derived from where it sits in the key."""
    parent = MODES.get(key_scale)
    if parent is None or degree_of(root, tonic, key_scale) is None:
        return f"{note_name(root)} {'Mixolydian' if quality == 'maj' else 'Dorian'}"
    # Find the parent major scale's tonic, then name the mode starting on `root`.
    key_mode_index = MODE_ROTATION.index(key_scale)
    major_tonic = (tonic - MODES["Major"][key_mode_index]) % 12
    deg_in_major = MODES["Major"].index((root - major_tonic) % 12)
    return f"{note_name(root)} {MODE_ROTATION[deg_in_major]}"
