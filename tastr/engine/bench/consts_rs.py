"""Reads and rewrites the fitted constants in engine-rs/src/consts.rs: the key model (`train_key --write`) and
the confidence calibration knots (`calibrate --write`). They live only there; rebuild the engine afterwards.

Each constant is replaced whole, with the `///` comment lines right above it, and the file is run through
`cargo fmt` when cargo is installed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import numpy as np

PATH = Path(__file__).resolve().parents[3] / "engine-rs" / "src" / "consts.rs"
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _span(src: str, name: str) -> tuple[int, int, int]:
    """(start of its doc comment, start of `pub const`, end after its `;`) of constant `name`."""
    m = re.search(rf"^pub const {name}\b", src, re.M)
    if not m:
        raise KeyError(f"{name} is not in {PATH.name}")
    end = src.index(";", src.index("=", m.start())) + 1
    lines_before = src[: m.start()].split("\n")[:-1]
    doc = m.start()
    while lines_before and lines_before[-1].startswith("///"):
        doc -= len(lines_before.pop()) + 1
    return doc, m.start(), end


def read(name: str, path: Path = PATH) -> np.ndarray:
    """The numbers of constant `name`, flattened in source order."""
    src = path.read_text()
    _, start, end = _span(src, name)
    body = src[src.index("=", start) + 1 : end]
    return np.array([float(v) for v in _NUMBER.findall(body)])


def _num(v: float) -> str:
    s = repr(float(v))
    return s if ("." in s or "e" in s or "inf" in s or "nan" in s) else s + ".0"


def matrix(name: str, m: np.ndarray, doc: list[str]) -> str:
    m = np.asarray(m, dtype=float)
    rows = "\n".join("    [" + ", ".join(_num(v) for v in r) + "]," for r in m)
    return _doc(doc) + f"pub const {name}: [[f64; {m.shape[1]}]; {m.shape[0]}] = [\n{rows}\n];"


def vector(name: str, v: np.ndarray, doc: list[str]) -> str:
    v = np.asarray(v, dtype=float).ravel()
    return _doc(doc) + f"pub const {name}: [f64; {v.size}] = [" + ", ".join(_num(x) for x in v) + "];"


def knots(name: str, pairs, doc: list[str]) -> str:
    items = ", ".join(f"({_num(x)}, {_num(p)})" for x, p in pairs)
    return _doc(doc) + f"pub const {name}: [(f64, f64); {len(pairs)}] = [{items}];"


def _doc(lines: list[str]) -> str:
    return "".join(f"/// {line}\n" for line in lines)


def write(blocks: dict[str, str], path: Path = PATH) -> Path:
    """Replace each named constant (and its doc comment) with the given Rust source, then format the file."""
    src = path.read_text()
    for name, text in blocks.items():
        doc, _, end = _span(src, name)
        src = src[:doc] + text + src[end:]
    path.write_text(src)
    if shutil.which("cargo") and path == PATH:
        subprocess.run(["cargo", "fmt"], cwd=PATH.parents[1], check=False)
    return path
