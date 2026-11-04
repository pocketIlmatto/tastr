"""Music-theory names the app shows: note names, Camelot codes, scale degrees and Roman numerals. The engine
(engine-rs/src/music.rs) does the estimation; tests/test_web.py checks the UI's numerals against roman_numeral."""

from __future__ import annotations

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

ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII"]


def note_name(pc: int) -> str:
    return NOTE_NAMES[int(pc) % 12]


def camelot(tonic: int, mode: str) -> str:
    """Camelot wheel code (e.g. 8A = A minor, 8B = C major)."""
    major_pc = tonic if mode == "major" else (tonic + 3) % 12
    number = (7 * major_pc + 8) % 12 or 12
    return f"{number}{'B' if mode == 'major' else 'A'}"


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
