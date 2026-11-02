"""Lay out the Worker from the app (TAS-84): run before `pywrangler dev` or `pywrangler deploy`.

- src/tastr/: the tastr package's Python files (the Worker bundle takes Python modules; other files are left out).
- src/tastr/web/static/index.html: the page, with each /static/ file's content hash already in its URL
  (server.versioned_index reads the files, which aren't in the bundle).
- public/static/: the browser files, served by Workers Static Assets, with the UI's modules also in
  public/static/app-<hash>/, where the page loads them (server.versioned_index), so a deploy never mixes old and
  new modules.
- public/sqljs/: sql.js, which runs hosted searches in the browser (TAS-49), from node_modules (`npm install`
  here; package.json pins the version tastr/web/routes/search.py names).
- public/engine/tastr_engine.wasm: the Rust engine's WebAssembly build, for cards made in the browser
  (TASTR_BROWSER_ENGINE), when it has been built (`cargo build --release --lib --target wasm32-unknown-unknown`
  in engine-rs/; the deploy workflow does). src/tastr_build.py records its hash for `rust.wasm_id()`.
  On Fly the server hands out these files itself (Dockerfile); here Static Assets do.

    python worker/build.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PKG = ROOT / "tastr"


def build() -> None:
    sys.path.insert(0, str(ROOT))
    from tastr.engine import rust
    from tastr.web.routes.search import SQLJS_FILES, SQLJS_VERSION
    from tastr.web.server import app_version, versioned_index

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
    shutil.copytree(PKG / "web" / "static" / "app", public / "static" / f"app-{app_version()}")

    sqljs = HERE / "node_modules" / "sql.js"
    if not (sqljs / "package.json").is_file():
        sys.exit("worker/node_modules has no sql.js: run `npm install` in worker/ first")
    version = json.loads((sqljs / "package.json").read_text())["version"]
    if version != SQLJS_VERSION:
        sys.exit(f"worker/package.json has sql.js {version}, the app asks for {SQLJS_VERSION} (routes/search.py)")
    (public / "sqljs").mkdir()
    for name in [*SQLJS_FILES, "LICENSE"]:
        shutil.copy2(sqljs / ("dist" if name in SQLJS_FILES else "") / name, public / "sqljs" / name)

    wasm, wasm_id = rust.wasm_path(), rust.wasm_id()
    if wasm is not None:
        (public / "engine").mkdir()
        shutil.copy2(wasm, public / "engine" / rust.WASM_NAME)
    (HERE / "src" / "tastr_build.py").write_text(
        f'"""Made by worker/build.py."""\n\nENGINE_WASM_ID = {wasm_id if wasm else None!r}\n')
    n = sum(1 for _ in src.rglob("*.py"))
    m = sum(1 for p in (public / "static").rglob("*") if p.is_file())
    print(f"worker/src/tastr: {n} Python files; worker/public/static: {m} files; sql.js {version}; "
          + (f"engine {wasm_id}" if wasm else "no engine build (cards in the browser stay off)"))


if __name__ == "__main__":
    build()
