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


def beat_this_folder(root, dataset="hjdb"):
    """A two-track Beat This! layout: one original spectrogram plus an augmented copy, beats at 120 BPM."""
    import numpy as np

    rng = np.random.default_rng(0)
    spec = rng.random((1500, 128)).astype(np.float16)  # 30 s at 50 fps, frames x bands as released
    np.savez(root / f"{dataset}.npz", **{"hjdb_1_a/track": spec, "hjdb_1_a/track_ps-3": spec})
    beats = root / "beat_this_annotations-1.0" / dataset / "annotations" / "beats"
    beats.mkdir(parents=True)
    (beats / "hjdb_1_a.beats").write_text("\n".join(f"{i * 0.5:.2f}\t{i % 4 + 1}" for i in range(60)))
    return root


def test_write_originals_drops_augmented_copies(tmp_path):
    import numpy as np

    beat_this_folder(tmp_path)
    assert main(["spec-bench", "tempo", str(tmp_path), "--write-originals"]) == 0
    assert list(np.load(tmp_path / "hjdb-originals.npz").keys()) == ["hjdb_1_a/track"]
    assert json.loads((tmp_path / "hjdb-annotations.json").read_text())["beats"]["hjdb_1_a"][1] == [0.5, 2.0]
    # The full file and the per-track annotations can go; the benches read the slim files.
    import shutil

    (tmp_path / "hjdb.npz").unlink()
    shutil.rmtree(tmp_path / "beat_this_annotations-1.0")
    report = jobs.run_spec_bench("tempo", "hjdb", tmp_path)
    assert report["summary"]["all"]["tracks"] == 1 and report["tracks"][0]["ref"] == 120.0
    assert jobs.run_spec_bench("downbeats", "hjdb", tmp_path)["tracks"] == 1


def test_spec_bench_remote(monkeypatch, tmp_path, capsys):
    report = {"tracks": 1, "right": 1.0, "half_bar_off": 0.0, "by_group": {"1": 1.0}, "timing": {"load_seconds": 0.5}}
    calls = fake_modal(monkeypatch, report)
    out = tmp_path / "r.json"
    assert main(["spec-bench", "downbeats", "beat-this", "--dataset", "hjdb", "--limit", "5", "--remote",
                 "--out", str(out)]) == 0
    assert calls == [("spec_bench", ("downbeats", "hjdb", "beat-this", 5), {})]
    assert "bar 1 right 1.000" in capsys.readouterr().out
    assert json.loads(out.read_text())["tracks"] == 1


@pytest.mark.audio
def test_key_profiles_train_without_a_cache(tmp_path, capsys):
    """`tastr key-profiles` writes what `train_key --profiles` reads, with no audio or feature cache after."""
    sys.path.insert(0, str(ROOT / "tests"))
    from synth import make_track, write_wav

    from tastr.engine.bench import key_features, train_key

    folder = tmp_path / "gs"
    (folder / "annotations" / "key").mkdir(parents=True)
    (folder / "audio").mkdir()
    for sid, (tonic, mode, name) in enumerate([(9, "minor", "A minor"), (0, "major", "C major")]):
        y, _ = make_track(bpm=124, tonic=tonic, mode=mode, bars_per_section=(2, 2, 2))
        write_wav(folder / "audio" / f"{sid}.LOFI.wav", y)
        (folder / "annotations" / "key" / f"{sid}.LOFI.key").write_text(name)
    out = tmp_path / "gs-profiles.json"
    assert main(["key-profiles", "giantsteps-key", str(folder), "--out", str(out)]) == 0
    rows = key_features.read_profiles(out)["giantsteps-key"]
    assert [(r["tonic"], r["mode"], r["label_conf"]) for r in rows] == [(9, "minor", -1), (0, "major", -1)]

    # The same rows as a feature cache gives, so a cache can be converted before it's deleted.
    feats = tmp_path / "features"
    assert key_features.cache("giantsteps-key", folder, feats) == []
    cached = tmp_path / "from-cache.json"
    assert main(["key-profiles", "giantsteps-key", str(folder), "--from-cache", str(feats), "--out", str(cached)]) == 0
    import numpy as np

    for a, b in zip(rows, key_features.read_profiles(cached)["giantsteps-key"]):
        assert np.allclose(a["profile"], b["profile"], atol=1e-5)

    assert train_key.main(["--profiles", str(out), "--profiles", str(out), "--no-synth"]) == 0
    assert "giantsteps-key: 4 training tracks" in capsys.readouterr().out


def test_key_profiles_remote(monkeypatch, tmp_path, capsys):
    result = {"tracks": [{"id": "1", "dataset": "beatport-key", "profile": [0.5] * 12, "silent": False,
                          "tonic": 2, "mode": "minor", "label_conf": 2}], "failed": [], "timing": {"load_seconds": 1}}
    calls = fake_modal(monkeypatch, result)
    out = tmp_path / "bp.json"
    assert main(["key-profiles", "beatport-key", "beatport-key", "--remote", "--limit", "5", "--out", str(out)]) == 0
    assert calls == [("key_profiles", ("beatport-key", "beatport-key", 5), {})]
    assert json.loads(out.read_text())["tracks"][0]["label_conf"] == 2


@pytest.mark.audio
def test_run_drum_bench_on_a_folder(tmp_path):
    from test_drums import mdb_folder

    report = jobs.run_drum_bench("mdb", mdb_folder(tmp_path / "mdb"), limit=1)
    assert report["suite"] == "mdb" and report["tracks"] == 1 and report["timing"]["jobs"] == 1
    assert jobs.run_drum_bench("mdb", tmp_path / "mdb", drums_only=True)["audio"] == "drum_only"
    with pytest.raises(ValueError, match="unknown drum suite"):
        jobs.run_drum_bench("enst", tmp_path)
    with pytest.raises(ValueError, match="needs the dataset folder"):
        jobs.run_drum_bench("mdb", None)


def test_drum_bench_remote(monkeypatch, tmp_path, capsys):
    report = {"suite": "mdb", "tracks": 2, "seconds": 3.0, "audio_seconds": 60.0, "fp_at": {}, "excused_cymbal_onsets": 0,
              "unlabeled_onsets_per_ref": {"clap": 0.01}, "timing": {"seconds": 4.0, "jobs": 4},
              "summary": {"tom": {"f": 0.5, "p": 0.5, "r": 0.5, "n": 4, "lenient": {"f": 0.6, "p": 0.6, "r": 0.6, "n": 4}, "r_alone": 1.0, "r_with": 0.0, "n_alone": 2, "n_with": 2}}}
    calls = fake_modal(monkeypatch, report)
    out = tmp_path / "r.json"
    assert main(["drum-bench", "mdb", "mdb-drums", "--limit", "3", "--remote", "--out", str(out)]) == 0
    assert calls == [("drum_bench", ("mdb", "mdb-drums", 3, False), {})]
    assert "R alone 1.00 (n=2)" in capsys.readouterr().out
    assert json.loads(out.read_text())["tracks"] == 2
    assert main(["drum-bench", "idmt"]) == 2


def test_drum_bench_write_bundle(tmp_path):
    from test_drums import mdb_folder

    root = mdb_folder(tmp_path / "mdb")
    assert main(["drum-bench", "mdb", str(root), "--write-bundle"]) == 0
    assert "Song" in json.loads((root / "mdb-annotations.json").read_text())
