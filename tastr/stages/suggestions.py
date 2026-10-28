""""Try this in Ableton" suggestions: drum pattern ideas, bass movement, chord-scale guesses.

STUB: one setup suggestion from whatever core already knows. Real
suggestions append Suggestion(category, title, body, data); put structured
payloads (16-step patterns as "x...x...", note lists) in `data` so the UI can
draw them.
"""

from __future__ import annotations

from ..card import Suggestion
from ..pipeline import AnalysisContext, register_stage


class SuggestionsStage:
    name = "suggestions"
    version = "0.0.1-stub"
    requires = ("core", "sections", "tags")

    def run(self, ctx: AnalysisContext) -> None:
        core = ctx.card.core
        out = []
        if core.bpm:
            out.append(Suggestion(
                category="setup",
                title=f"Set the project tempo to {core.bpm:.0f} BPM",
                body="Match Live's tempo to the track before sketching parts against it.",
                data={"bpm": core.bpm},
            ))
        ctx.card.suggestions = out


register_stage(SuggestionsStage())
