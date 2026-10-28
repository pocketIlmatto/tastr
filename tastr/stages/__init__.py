"""Built-in pipeline stages. Importing this package registers them.

Each module owns one part of the track card:

    core         bpm, key, energy, loudness, beats/downbeats    -> card.core
    sections     rough section timestamps and labels             -> card.sections
    tags         vibe tags                                       -> card.tags
    suggestions  "Try this in Ableton" ideas                     -> card.suggestions

Any stage may also add scalar `card.features`, `card.series` curves and
structured `card.extras`. Prefix extras keys with the stage name.
"""

from . import core, sections, suggestions, tags  # noqa: F401
