"""docs/hosting-map.md names every setting, secret and Modal function the deploy files use (worker/wrangler.jsonc,
the deploy workflows, tastr/web/hosted.py, tastr/blobs.py, tastr/cloud/modal_web.py), so it can't fall behind."""

import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MAP = (ROOT / "docs/hosting-map.md").read_text()


def _wrangler() -> dict:
    """worker/wrangler.jsonc, without its // comments."""
    text = re.sub(r"(^|\s)//[^\n]*", r"\1", (ROOT / "worker/wrangler.jsonc").read_text())
    return json.loads(text)


def _missing(names) -> list[str]:
    return sorted(n for n in set(names) if not re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", MAP))


def test_map_names_worker_settings():
    w = _wrangler()
    names = [w["name"], *w["vars"], w["durable_objects"]["bindings"][0]["class_name"],
             *(v for d in w.get("d1_databases", []) for v in (d["binding"], d["database_name"]))]
    assert _missing(names) == []


def test_map_names_hosted_settings():
    src = (ROOT / "tastr/web/hosted.py").read_text()
    names = re.findall(r'env\.get\("([A-Z_]+)"', src)
    assert names and _missing(names) == []


def test_map_names_deploy_secrets():
    workflows = [ROOT / ".github/workflows/deploy.yml", ROOT / ".github/workflows/deploy-worker.yml"]
    names = [n for f in workflows for n in re.findall(r"secrets\.([A-Z_]+)", f.read_text())]
    assert "CLOUDFLARE_API_TOKEN" in names and _missing(names) == []


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
