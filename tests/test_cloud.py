import ast
import json
import sys
import types
from pathlib import Path

import pytest

from tastr.cli import main
from tastr import stems
from tastr.cloud import jobs

ROOT = Path(__file__).resolve().parent.parent


def test_modal_image_deps_match_pyproject():
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    # modal_app.py needs Modal and deploy-time settings to import, so read its ENGINE_DEPS from the source.
    tree = ast.parse((ROOT / "tastr/cloud/modal_app.py").read_text())
    consts = {n.targets[0].id: n.value for n in tree.body
              if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id.endswith("_DEPS")}
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    web = ("fastapi", "uvicorn", "python-multipart")
    assert sorted(ast.literal_eval(consts["ENGINE_DEPS"])) == sorted(d for d in project["dependencies"] if not d.startswith(web))
    assert ast.literal_eval(consts["STEMS_DEPS"]) == project["optional-dependencies"]["stems"]


@pytest.mark.audio
def test_run_bench_resolves_path_under_root(tmp_path):
    report = jobs.run_bench("synth", None, tmp_path, limit=2)
    assert report["summary"]["tracks"] == 2
    assert report["timing"]["jobs"] == 1 and report["timing"]["load_seconds"] >= 0
    with pytest.raises((ValueError, FileNotFoundError), match="missing"):
        jobs.run_bench("giantsteps-key", "missing", tmp_path)


def test_remote_without_modal(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "modal", None)
    assert main(["bench", "synth", "--remote"]) == 2
    assert "tastr[cloud]" in capsys.readouterr().err


def fake_modal(monkeypatch, result):
    calls = []

    class Fn:
        def __init__(self, name):
            self.name = name

        def remote(self, *a, **kw):
            calls.append((self.name, a, kw))
            return result

    fake = types.SimpleNamespace(Function=types.SimpleNamespace(from_name=lambda app, name: Fn(name)),
                                 exception=types.SimpleNamespace(NotFoundError=LookupError))
    monkeypatch.setitem(sys.modules, "modal", fake)
    return calls


def test_run_stems_returns_flac_bytes(monkeypatch):
    def separate(src, out, progress=None):
        assert src.read_bytes() == b"audio" and src.suffix == ".m4a"
        out.mkdir()
        for n in stems.STEM_NAMES:
            stems.stem_path(out, n).write_bytes(n.encode())

    monkeypatch.setattr(stems, "separate", separate)
    assert jobs.run_stems(b"audio", ".m4a") == {n: n.encode() for n in stems.STEM_NAMES}


def test_stems_backend_modal(monkeypatch, tmp_path):
    monkeypatch.setenv("TASTR_STEMS_BACKEND", "modal")
    calls = fake_modal(monkeypatch, {n: n.encode() for n in stems.STEM_NAMES})
    monkeypatch.setattr(stems.importlib.util, "find_spec", lambda name: object() if name == "modal" else None)
    assert stems.available()
    src = tmp_path / "song.mp3"
    src.write_bytes(b"mp3")
    seen = []
    stems.separate_track(src, tmp_path / "stems", seen.append)
    assert calls == [("stems", (b"mp3", ".mp3"), {})]
    assert stems.is_ready(tmp_path / "stems") and seen[-1] == 1.0
    assert stems.stem_path(tmp_path / "stems", "bass").read_bytes() == b"bass"


@pytest.mark.audio
def test_remote_bench_calls_deployed_function(monkeypatch, tmp_path):
    calls = fake_modal(monkeypatch, jobs.run_bench("synth", None, tmp_path, limit=1))
    out = tmp_path / "r.json"
    assert main(["bench", "beatport-key", "beatport-key", "--remote", "--limit", "50", "--out", str(out)]) == 0
    assert calls == [("bench", ("beatport-key", "beatport-key"), {"stages": None, "limit": 50, "jobs": None})]
    assert json.loads(out.read_text())["summary"]["tracks"] == 1


def test_index_round_trip(tmp_path, monkeypatch):
    from tastr.engine import bench
    from tastr.engine.bench.datasets import Item, Truth

    folder = tmp_path / "bp"
    (folder / "audio").mkdir(parents=True)
    (folder / "audio" / "1 a.mp3").write_bytes(b"")
    loaded = []

    def fake_load(name, path=None, **kw):
        loaded.append(path)
        return [Item(id="bp-key-1", path=str(Path(path) / "audio" / "1 a.mp3"), truth=Truth(tonic=9, mode="minor"))]

    monkeypatch.setattr(bench, "load", fake_load)
    assert main(["bench", "beatport-key", str(folder), "--write-index"]) == 0
    assert len(loaded) == 1

    # Mounted somewhere else, as on Modal: paths follow the folder and the loader isn't called again.
    moved = tmp_path / "mnt" / "bp"
    moved.parent.mkdir()
    folder.rename(moved)
    items = jobs.load_items("beatport-key", moved)
    assert len(loaded) == 1
    assert items[0].path == str(moved / "audio" / "1 a.mp3") and items[0].truth.mode == "minor"
    # Another dataset in the same folder has its own index; without one it falls back to the loader.
    jobs.load_items("beatport-tempo", moved)
    assert len(loaded) == 2
    assert (moved / "tastr-index-beatport-key.json").is_file()
    # An index under #61's single name still counts for its own dataset.
    (moved / "tastr-index-beatport-key.json").rename(moved / "tastr-index.json")
    assert jobs.load_items("beatport-key", moved)[0].id == "bp-key-1" and len(loaded) == 2
