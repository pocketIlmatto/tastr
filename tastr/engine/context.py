"""What a stage works with: the analysis context, the Stage interface and the stage registry.

A leaf module (it imports nothing from the engine but the card), so stages and the signals they share can
import it without importing the pipeline that runs them. tastr.engine.pipeline re-exports all of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import numpy as np

from .card import TrackCard

SR = 22050
HOP = 512


@dataclass
class AnalysisContext:
    y: np.ndarray  # mono float32 audio
    sr: int
    card: TrackCard
    hop: int = HOP
    cache: dict[str, Any] = field(default_factory=dict)
    # Per-analysis choices from the caller, e.g. {"beats_per_bar": 3} to set the meter instead of detecting it.
    options: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str, compute: Callable[["AnalysisContext"], Any]) -> Any:
        """Return a shared intermediate, computing it once on first use."""
        if name not in self.cache:
            self.cache[name] = compute(self)
        return self.cache[name]


class Stage(Protocol):
    name: str
    version: str
    requires: tuple[str, ...]

    def run(self, ctx: AnalysisContext) -> None: ...


REGISTRY: dict[str, Stage] = {}


def register_stage(stage: Stage, *, replace: bool = False) -> Stage:
    if stage.name in REGISTRY and not replace:
        raise ValueError(f"stage {stage.name!r} already registered")
    REGISTRY[stage.name] = stage
    return stage


def unregister_stage(name: str) -> None:
    REGISTRY.pop(name, None)
