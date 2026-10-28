"""Rough section detection (intro / build / drop / breakdown / outro...).

STUB: emits a single section covering the whole track. A real detector
should use ctx.card.core.downbeat_times to snap boundaries to bars and fill
start_bar / end_bar / letter / energy.
"""

from __future__ import annotations

from ..card import Section
from ..pipeline import AnalysisContext, register_stage


class SectionsStage:
    name = "sections"
    version = "0.0.1-stub"
    requires = ("core",)

    def run(self, ctx: AnalysisContext) -> None:
        ctx.card.sections = [Section(start=0.0, end=ctx.card.duration or 0.0, label="full", letter="A")]


register_stage(SectionsStage())
