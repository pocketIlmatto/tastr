"""The template drum patterns the server lists (tastr/engine/patterns.py) are the ones the Rust engine suggests
(engine-rs/src/stages/suggestions.rs)."""

import re
from pathlib import Path

from tastr.engine.patterns import STYLE_LABELS, VARIATIONS

RUST = Path(__file__).parents[1] / "engine-rs" / "src" / "stages" / "suggestions.rs"


def _fn(src: str, name: str) -> str:
    start = src.index(f"fn {name}(")
    return src[start:src.index("\n}\n", start)]


def test_variations_match_the_rust_engine():
    body = _fn(RUST.read_text(), "variations")
    styles = re.findall(r'\n        "(\w+)" => ', body)
    arms = re.split(r'\n        "\w+" => ', body)[1:]
    got = {}
    for style, arm in zip(styles, arms):
        got[style] = [(name, dict(re.findall(r'\("(\w+)", "([x.o]+)"\)', lanes)))
                      for name, lanes in re.findall(r'\(\s*"([^"]+)",\s*&\[(.*?)\]', arm, re.DOTALL)]
    want = {style: [(name, dict(p)) for name, p in items] for style, items in VARIATIONS.items()}
    assert got == want


def test_style_labels_match_the_rust_engine():
    body = _fn(RUST.read_text(), "style_label")
    got = dict(re.findall(r'"(\w+)" => "([^"]+)"', body))
    got["waltz"] = re.search(r'_ => "([^"]+)"', body).group(1)
    assert got == STYLE_LABELS
