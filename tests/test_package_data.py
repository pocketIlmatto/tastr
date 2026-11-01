"""Every file the server needs ships in the installed package (the Fly image does `pip install .`, not editable).

A new folder under tastr/web/static/ needs its own glob in pyproject's package-data, or it is missing on the
live site while local runs from the checkout still work (TAS-69: static/app/ 404'd after the ES module split).
"""

import fnmatch
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_non_python_files_are_in_package_data():
    globs = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["tastr"]
    pkg = ROOT / "tastr"
    missing = [
        str(rel)
        for f in pkg.rglob("*")
        if f.is_file() and f.suffix not in {".py", ".pyc"} and "__pycache__" not in f.parts
        for rel in [f.relative_to(pkg)]
        # setuptools globs don't cross folders: "web/static/*" doesn't match web/static/app/main.js.
        if not any(fnmatch.fnmatch(str(rel), g) and rel.parent == Path(g).parent for g in globs)
    ]
    assert not missing, f"not in [tool.setuptools.package-data] in pyproject.toml: {missing}"
