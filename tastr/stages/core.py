"""Core features: BPM, key, energy, loudness, beat grid.

STUB: fills only what is trivial so the rest of the pipeline has something
to run on. Real estimators replace the TODOs below.
"""

from __future__ import annotations

import numpy as np

from ..pipeline import AnalysisContext, register_stage


class CoreStage:
    name = "core"
    version = "0.0.1-stub"
    requires: tuple[str, ...] = ()

    def run(self, ctx: AnalysisContext) -> None:
        core = ctx.card.core
        y = ctx.y
        core.loudness_db = round(float(20 * np.log10(np.sqrt(np.mean(y**2)) + 1e-9)), 2)
        # TODO(core): core.bpm, core.bpm_confidence, core.beat_times, core.downbeat_times
        # TODO(core): core.key = Key(tonic=..., mode=..., scale=..., camelot=music.camelot(...), confidence=...)
        # TODO(core): core.energy (0..1) and ctx.card.series["energy"] (per bar, for the UI)


register_stage(CoreStage())
