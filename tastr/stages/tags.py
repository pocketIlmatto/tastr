"""Vibe tags ("dark", "driving", "euphoric"...).

STUB: no tags yet. Read ctx.card.core / features / sections and append
Tag(tag, score) entries with scores in 0..1.
"""

from __future__ import annotations

from ..pipeline import AnalysisContext, register_stage


class TagsStage:
    name = "tags"
    version = "0.0.1-stub"
    requires = ("core", "sections")

    def run(self, ctx: AnalysisContext) -> None:
        ctx.card.tags = []


register_stage(TagsStage())
