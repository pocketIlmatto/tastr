"""Accuracy benchmark: run the engine over labeled tracks and score it.

`tastr bench DATASET [PATH]` from the command line; see docs/bench.md.
"""

from .datasets import DATASETS, Item, Truth, load  # noqa: F401
from .runner import format_summary, run  # noqa: F401
