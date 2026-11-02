"""Lay out the Worker from the app (TAS-84): run before `pywrangler dev` or `pywrangler deploy`.

- src/tastr/: the tastr package's Python files (the Worker bundle takes Python modules; other files are left out).
- src/tastr/web/static/index.html: the page, with each /static/ file's content hash already in its URL
  (server.versioned_index reads the files, which aren't in the bundle).
- public/static/: the browser files, served by Workers Static Assets.

    python worker/build.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PKG = ROOT / "tastr"


def build() -> None:
    sys.path.insert(0, str(ROOT))
    from tastr.web.server import versioned_index

    src, public = HERE / "src" / "tastr", HERE / "public"
    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(public, ignore_errors=True)
    for f in PKG.rglob("*.py"):
        if "__pycache__" in f.parts:
            continue
        dest = src / f.relative_to(PKG)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
    page = src / "web" / "static" / "index.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(versioned_index())
    shutil.copytree(PKG / "web" / "static", public / "static", ignore=shutil.ignore_patterns("index.html"))
    n = sum(1 for _ in src.rglob("*.py"))
    m = sum(1 for p in (public / "static").rglob("*") if p.is_file())
    print(f"worker/src/tastr: {n} Python files; worker/public/static: {m} files")


if __name__ == "__main__":
    build()
