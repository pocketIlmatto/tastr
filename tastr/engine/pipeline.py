"""Analysis pipeline: decoded audio -> a sequence of stages -> a TrackCard.

Plug-in point: a stage is any object with `name`, `version`, `requires` and a
`run(ctx)` method that writes into `ctx.card`. Register it with
`register_stage` and it runs after the stages it requires.

Stages share expensive intermediate signals (HPSS, onset envelope, chroma...)
through `ctx.cache` / `ctx.get(name, fn)`, so compute them there instead of
recomputing per stage.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import numpy as np

from .card import TrackCard

log = logging.getLogger(__name__)

SR = 22050
HOP = 512


@dataclass
class AnalysisContext:
    y: np.ndarray  # mono float32 audio
    sr: int
    card: TrackCard
    hop: int = HOP
    cache: dict[str, Any] = field(default_factory=dict)

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


_REGISTRY: dict[str, Stage] = {}


def register_stage(stage: Stage, *, replace: bool = False) -> Stage:
    if stage.name in _REGISTRY and not replace:
        raise ValueError(f"stage {stage.name!r} already registered")
    _REGISTRY[stage.name] = stage
    return stage


def unregister_stage(name: str) -> None:
    _REGISTRY.pop(name, None)


def registered_stages() -> dict[str, Stage]:
    _load_builtin_stages()
    return dict(_REGISTRY)


def _load_builtin_stages() -> None:
    from . import stages  # noqa: F401  (importing registers them)


def resolve_order(names: list[str] | None = None) -> list[Stage]:
    """Topologically order the requested stages (default: all), pulling in requirements."""
    reg = registered_stages()
    wanted = list(reg) if names is None else names
    order: list[Stage] = []
    seen: set[str] = set()

    def visit(name: str, chain: tuple[str, ...]) -> None:
        if name in seen:
            return
        if name in chain:
            raise ValueError(f"stage dependency cycle: {' -> '.join(chain + (name,))}")
        if name not in reg:
            raise KeyError(f"unknown stage {name!r}; registered: {sorted(reg)}")
        for dep in reg[name].requires:
            visit(dep, chain + (name,))
        seen.add(name)
        order.append(reg[name])

    for n in wanted:
        visit(n, ())
    return order


def run_pipeline(y: np.ndarray, sr: int, card: TrackCard, stages: list[str] | None = None) -> TrackCard:
    """Run stages over decoded audio. A failing stage is recorded in card.errors and
    stages that depend on it are skipped; the rest still run."""
    card.duration = round(y.size / sr, 3)
    card.sample_rate = sr
    ctx = AnalysisContext(y=y, sr=sr, card=card)
    for stage in resolve_order(stages):
        failed_deps = [d for d in stage.requires if d in card.errors or d not in card.stages_run]
        if failed_deps:
            card.errors[stage.name] = f"skipped: required stage(s) {failed_deps} did not complete"
            continue
        try:
            stage.run(ctx)
            card.stages_run[stage.name] = stage.version
        except Exception as e:  # keep going: a partial card beats no card
            log.warning("stage %s failed: %s", stage.name, e)
            card.errors[stage.name] = f"{type(e).__name__}: {e}"
            log.debug(traceback.format_exc())
    return card
