"""docs/hosting-map.md names every setting, secret and Modal function the deploy files use, so it can't fall behind."""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAP = (ROOT / "docs/hosting-map.md").read_text()


def _fly() -> dict:
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    return tomllib.loads((ROOT / "fly.toml").read_text())


def _missing(names) -> list[str]:
    return sorted(n for n in set(names) if not re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", MAP))


def test_map_names_fly_settings():
    fly = _fly()
    names = [fly["app"], fly["primary_region"], *fly["env"]]
    names += [m["source"] for m in fly["mounts"]] + [fly["http_service"]["checks"][0]["path"]]
    assert _missing(names) == []


def test_map_names_hosted_settings():
    src = (ROOT / "tastr/web/hosted.py").read_text()
    names = re.findall(r'env\.get\("([A-Z_]+)"', src)
    assert names and _missing(names) == []


def test_map_names_backup_and_deploy_secrets():
    litestream = re.findall(r"\$\{([A-Z_]+)\}", (ROOT / "deploy/litestream.yml").read_text())
    deploy = re.findall(r"secrets\.([A-Z_]+)", (ROOT / ".github/workflows/deploy.yml").read_text())
    start = re.findall(r"\$((?:TASTR|LITESTREAM|MODAL)_[A-Z_]+)", (ROOT / "deploy/start.sh").read_text())
    assert litestream and deploy and _missing(litestream + deploy + start) == []


def test_map_names_modal_functions():
    tree = ast.parse((ROOT / "tastr/cloud/modal_web.py").read_text())
    fns = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)
           and any("function" in ast.unparse(d) for d in n.decorator_list)]
    app = next(ast.literal_eval(n.value) for n in tree.body
               if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "APP_NAME")
    assert fns and _missing([*fns, app]) == []


def test_map_names_audio_bucket_settings():
    names = re.findall(r'environ\.get\("(TASTR_AUDIO_[A-Z_]+)"', (ROOT / "tastr/blobs.py").read_text())
    assert names and _missing(names) == []
