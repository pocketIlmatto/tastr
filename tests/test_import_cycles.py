"""No import cycles between tastr modules (TAS-39).

Every import counts, including ones inside functions, since a lazy import still ties the two modules
together. Shared registries and types live in leaf modules (engine/context.py, lookup/base.py,
resolvers/base.py) so the modules that fill them don't import back the ones that read them.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _modules() -> dict[str, Path]:
    mods = {}
    for p in (ROOT / "tastr").rglob("*.py"):
        parts = list(p.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        mods[".".join(parts)] = p
    return mods


def _graph() -> dict[str, set[str]]:
    mods = _modules()

    def resolve(name: str) -> str | None:
        while name and name not in mods:
            name = name.rsplit(".", 1)[0] if "." in name else ""
        return name or None

    graph: dict[str, set[str]] = {m: set() for m in mods}
    for m, p in mods.items():
        pkg = m if p.name == "__init__.py" else m.rsplit(".", 1)[0]
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = pkg.split(".")
                    base = base[: len(base) - (node.level - 1)]
                    mod = ".".join(base + ([node.module] if node.module else []))
                else:
                    mod = node.module or ""
                targets = [f"{mod}.{a.name}" if f"{mod}.{a.name}" in mods else mod for a in node.names]
            else:
                continue
            for t in targets:
                r = resolve(t)
                if r and r != m:
                    graph[m].add(r)
    return graph


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components with more than one module (Tarjan)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    out: list[list[str]] = []

    def visit(v: str) -> None:
        index[v] = low[v] = len(index)
        stack.append(v)
        on_stack.add(v)
        for w in graph[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                out.append(sorted(comp))

    for v in graph:
        if v not in index:
            visit(v)
    return out


def test_cycle_finder_sees_a_cycle():
    assert _cycles({"a": {"b"}, "b": {"a"}, "c": {"a"}}) == [["a", "b"]]


def test_no_import_cycles():
    assert _cycles(_graph()) == []
