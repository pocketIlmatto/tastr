"""The Python reference engine's stages, which the benchmarks score. Importing this package registers them.
The app's cards come from the Rust engine (engine-rs/src/stages/), which also has the tags, suggestions and
views stages; their Python copies were removed in TAS-47.

    core         bpm, key, energy, loudness, beats/downbeats    -> card.core
    sections     rough section timestamps and labels             -> card.sections

Any stage may also add scalar `card.features`, `card.series` curves and
structured `card.extras`. Prefix extras keys with the stage name.
"""

from . import core, sections  # noqa: F401
