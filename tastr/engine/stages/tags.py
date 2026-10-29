"""Vibe tags ("dark", "driving", "euphoric"...): pure rules over core numbers and features."""

from __future__ import annotations

from ..card import Tag
from ..pipeline import AnalysisContext, register_stage

MIN_SCORE = 0.2

# Rough tempo homes for common styles. These are hints, not genre classification.
TEMPO_STYLES = [
    ("downtempo", 70, 100),
    ("hip-hop tempo", 80, 100),
    ("house tempo", 118, 130),
    ("techno tempo", 125, 145),
    ("dubstep/halftime tempo", 138, 150),
    ("drum & bass tempo", 160, 180),
]


def vibe_scores(bpm, mode, energy, f: dict) -> dict[str, float]:
    """Score every tag whose inputs are present (missing inputs skip the rule).

    Returns every positive score, including those under MIN_SCORE, sorted high to low.
    """
    minor = None if mode is None else mode == "minor"
    bright = f.get("brightness")
    perc = f.get("percussiveness")
    pulse = f.get("pulse_clarity")
    dance = f.get("danceability")
    low = f.get("low_end")
    contrast = f.get("section_contrast")
    scores: dict[str, float] = {}

    def add(tag: str, score: float) -> None:
        if score > 0:
            scores[tag] = max(scores.get(tag, 0.0), round(min(score, 1.0), 3))

    if energy is not None:
        add("high energy", (energy - 0.6) / 0.3)
        if bpm is not None:
            add("chill", (0.45 - energy) / 0.25 if bpm < 115 else (0.35 - energy) / 0.25)
        if minor and energy < 0.5:
            add("melancholic", 0.5 + (0.4 - energy))
    if bright is not None and minor is not None:
        add("dark", (0.45 - bright) / 0.3 + (0.25 if minor else -0.2))
        add("bright", (bright - 0.5) / 0.3 + (-0.15 if minor else 0.15))
        if energy is not None and not minor:
            add("euphoric", (energy - 0.55) / 0.25 + (bright - 0.45))
    if pulse is not None and bpm is not None and 115 <= bpm <= 150:
        add("driving", (pulse - 0.45) / 0.35)
    if dance is not None:
        add("danceable", (dance - 0.55) / 0.3)
    if pulse is not None and contrast is not None:
        add("hypnotic", (pulse - 0.4) / 0.4 - 3 * contrast + 0.3)
    if low is not None:
        add("bass-heavy", (low - 0.25) / 0.25)
    if perc is not None:
        add("percussive", (perc - 0.35) / 0.25)
        if pulse is not None:
            add("atmospheric", (0.2 - perc) / 0.15 + (0.4 - pulse))
    if contrast is not None:
        add("dynamic", (contrast - 0.2) / 0.15)
    swing_conf, swing_pct = f.get("swing_confidence"), f.get("swing_percent")
    if swing_conf is not None and swing_pct is not None and swing_conf > 0.3:
        add("swung", (swing_pct - 54) / 8)
    if bpm is not None:
        for name, lo, hi in TEMPO_STYLES:
            if lo <= bpm <= hi:
                add(name, 0.5)

    return dict(sorted(scores.items(), key=lambda kv: -kv[1]))


def vibe_tags(bpm, mode, energy, f: dict) -> list[Tag]:
    """The tags worth showing: scores of MIN_SCORE and up."""
    return [Tag(t, s) for t, s in vibe_scores(bpm, mode, energy, f).items() if s >= MIN_SCORE]


class TagsStage:
    name = "tags"
    version = "0.1.0"
    requires = ("core", "sections")

    def run(self, ctx: AnalysisContext) -> None:
        card = ctx.card
        key = card.core.key
        scores = vibe_scores(card.core.bpm, key.mode if key else None, card.core.energy, card.features)
        card.tags = [Tag(t, s) for t, s in scores.items() if s >= MIN_SCORE]
        card.extras["tags.vibe_scores"] = scores  # includes the ones under MIN_SCORE, for "show all scores"


register_stage(TagsStage())
