import ast
import json
import re
import sys
import types
from pathlib import Path

import pytest

from tastr.cli import main
from tastr import stems
from tastr.cloud import jobs

ROOT = Path(__file__).resolve().parent.parent


def _consts(name: str) -> dict:
    # The Modal apps need Modal (and modal_app.py deploy-time settings) to import, so read them from the source.
    tree = ast.parse((ROOT / "tastr/cloud" / name).read_text())
    return {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id.endswith(("_DEPS", "PINS"))}


@pytest.mark.parametrize("name", ["modal_app.py", "modal_web.py"])
def test_modal_image_deps_match_pyproject(name):
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    consts = _consts(name)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    web = ("fastapi", "uvicorn", "python-multipart")
    extras = project["optional-dependencies"]
    base = [d for d in project["dependencies"] if not d.startswith(web)]
    assert sorted(consts["ENGINE_DEPS"]) == sorted(base + extras["engine"])
    assert consts["STEMS_DEPS"] == [d for d in extras["stems"] if d != "tastr[engine]"]
    if "TORCH_CPU" in (ROOT / "tastr/cloud" / name).read_text():
        assert f'TORCH_CPU = ("{project["optional-dependencies"]["drums"][0]}"' in (ROOT / "tastr/cloud" / name).read_text()
    assert consts["PINS"] == _consts("modal_app.py")["PINS"]


def test_every_analysis_image_has_the_rust_engine():
    """The Rust engine makes every card (there is no Python fallback), so each Modal image that analyzes builds
    its library in; the build-time warm-up fails the deploy otherwise."""
    src = (ROOT / "tastr/cloud/modal_web.py").read_text()
    for name in ("image", "drums_image"):
        assert re.search(rf"^{name} = \(\n    with_rust_engine\(", src, re.M), name


def test_docker_image_has_no_dsp_libraries():
    """The web server analyzes nothing itself (Modal or the browser does), so its image leaves out the engine
    extra; it keeps the Modal images' numpy pin."""
    docker = (ROOT / "Dockerfile").read_text()
    pins = _consts("modal_app.py")["PINS"]
    assert '".[hosted,cloud]"' in docker and all(f'"{p}"' in docker for p in pins if p.startswith("numpy"))
    assert not any(lib in docker for lib in ("librosa", "scipy", "numba", "llvmlite", "engine]"))


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
    assert jobs.run_spec_bench("meter", "hjdb", tmp_path)["tracks"] == 1


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
    assert calls == [("drum_bench", ("mdb", "mdb-drums", 3, False, "all"), {})]
    assert "R alone 1.00 (n=2)" in capsys.readouterr().out
    assert json.loads(out.read_text())["tracks"] == 2
    assert main(["drum-bench", "idmt"]) == 2


def test_drum_bench_write_bundle(tmp_path):
    from test_drums import mdb_folder

    root = mdb_folder(tmp_path / "mdb")
    assert main(["drum-bench", "mdb", str(root), "--write-bundle"]) == 0
    assert "Song" in json.loads((root / "mdb-annotations.json").read_text())


def test_drum_bench_remote_with_the_drum_model(monkeypatch, tmp_path):
    report = {"suite": "idmt", "tracks": 1, "seconds": 1.0, "audio_seconds": 10.0, "fp_at": {}, "excused_cymbal_onsets": 0,
              "unlabeled_onsets_per_ref": {}, "timing": {"seconds": 2.0, "jobs": 4},
              "summary": {"kick": {"f": 1.0, "p": 1.0, "r": 1.0, "n": 2}}}
    calls = fake_modal(monkeypatch, report)
    assert main(["drum-bench", "idmt", "idmt-smt-drums", "--remote", "--drum-model"]) == 0
    assert calls == [("drum_bench_model", ("idmt", "idmt-smt-drums", None, False, "all"), {})]


def test_drums_extra_shares_pytorch():
    tomllib = pytest.importorskip("tomllib")
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    assert any(d.startswith("torch") for d in extras["drums"])


def test_modal_images_add_local_files_last(monkeypatch):
    """Modal refuses an image build step after `add_local_*` (that failed a real deploy), so check every image."""
    class Image:
        def __init__(self, local=False):
            self.local = local

        def _step(self, *a, **kw):
            assert not self.local, "a build step after add_local_python_source"
            return Image()

        apt_install = pip_install = run_commands = env = add_local_dir = _step  # add_local_dir: copied (rust_image)

        def add_local_python_source(self, *a, **kw):
            assert not self.local
            return Image(local=True)

    images = []

    class App:
        def __init__(self, name):
            pass

        def function(self, image, **kw):
            images.append(image)
            return lambda f: f

        def local_entrypoint(self, **kw):
            return lambda f: f

    fake = types.SimpleNamespace(
        Image=types.SimpleNamespace(debian_slim=lambda **kw: Image()), App=App, is_local=lambda: True,
        CloudBucketMount=lambda *a, **kw: None, Secret=types.SimpleNamespace(from_name=lambda name: None))
    monkeypatch.setitem(sys.modules, "modal", fake)
    monkeypatch.setenv("TASTR_R2_ENDPOINT", "https://example.r2.cloudflarestorage.com")
    import importlib

    sys.modules.pop("tastr.cloud.modal_app", None)
    importlib.import_module("tastr.cloud.modal_app")
    sys.modules.pop("tastr.cloud.modal_app", None)
    assert images and all(i.local for i in images)  # every function's image ships the tastr source


def _load_modal_web(monkeypatch):
    """tastr/cloud/modal_web.py with a stand-in `modal`; returns (the module, each function's image)."""
    class Image:
        def __init__(self, local=False, copied=False):
            self.local, self.copied = local, copied

        def _step(self, *a, **kw):
            assert not self.local or self.copied, "a build step after add_local_python_source"
            return Image(self.local, self.copied)

        apt_install = pip_install = run_commands = run_function = env = add_local_dir = _step

        def add_local_python_source(self, *a, copy=False, **kw):
            assert not self.local
            return Image(local=True, copied=copy)

    images = []

    class App:
        def __init__(self, name):
            assert name == "tastr-web"

        def function(self, image, **kw):
            images.append(image)
            return lambda f: f

    same = lambda **kw: (lambda f: f)  # noqa: E731 - decorators that change nothing here
    fake = types.SimpleNamespace(Image=types.SimpleNamespace(debian_slim=lambda **kw: Image()), App=App,
                                 concurrent=same, asgi_app=same)
    monkeypatch.setitem(sys.modules, "modal", fake)
    import importlib

    sys.modules.pop("tastr.cloud.modal_web", None)
    mod = importlib.import_module("tastr.cloud.modal_web")
    sys.modules.pop("tastr.cloud.modal_web", None)
    return mod, images


def test_modal_web_image_builds_steps_only_on_copied_source(monkeypatch):
    """Like modal_app: a build step after add_local_python_source fails a deploy unless the source is copied."""
    _, images = _load_modal_web(monkeypatch)
    assert len(images) == 5 and all(i.local for i in images)


@pytest.fixture
def modal_api(monkeypatch):
    """remote.py over HTTPS (TASTR_MODAL_URL), sent to modal_web's `api` app with the Modal functions faked."""
    from fastapi.testclient import TestClient

    from tastr.cloud import remote
    from test_foundation import full_card

    mod, _ = _load_modal_web(monkeypatch)
    card, calls, sent = full_card().to_dict(), [], []

    class Fn:
        def __init__(self, name, result=None, fail=None):
            self.name, self.result, self.fail = name, result, fail
            self.remote = types.SimpleNamespace(aio=self.call)
            self.remote_gen = types.SimpleNamespace(aio=self.gen)

        async def call(self, *a):
            calls.append((self.name, a))
            if self.fail:
                raise RuntimeError(self.fail)
            return self.result

        async def gen(self, *a):
            calls.append((self.name, a))
            yield {"stage": "decode", "i": 0, "n": 1}
            yield {"card": card}

    monkeypatch.setattr(mod, "analyze_stream", Fn("analyze_stream"), raising=False)
    monkeypatch.setattr(mod, "analyze_drums", Fn("analyze_drums", card), raising=False)
    monkeypatch.setattr(mod, "stems", Fn("stems", {"drums": b"flac", "bass": b"flac2"}), raising=False)
    client = TestClient(mod.api())

    def post(url, data, headers):
        sent.append((url, headers))
        r = client.post(url.removeprefix("https://modal.example"), content=data, headers=headers)
        if r.status_code >= 400:
            raise remote.RemoteError(f"Modal answered {r.status_code}")
        yield r.content[:7]  # in pieces, as a network delivers it
        yield r.content[7:]

    monkeypatch.setenv("TASTR_MODAL_URL", "https://modal.example/")
    monkeypatch.setenv("TASTR_MODAL_KEY", "wk-1")
    monkeypatch.setenv("TASTR_MODAL_SECRET", "ws-1")
    remote.use_transport(post)
    yield types.SimpleNamespace(mod=mod, Fn=Fn, calls=calls, sent=sent, card=card)
    remote.use_transport(None)


def test_remote_analyze_over_https_reports_stages(modal_api, tmp_path):
    from tastr.cloud import remote

    song = tmp_path / "a.mp3"
    song.write_bytes(b"mp3 bytes")
    stages = []
    card = remote.analyze(song, {"beats_per_bar": 3}, on_stage=lambda s, i, n: stages.append(s))
    assert card.core.bpm == modal_api.card["core"]["bpm"] and stages == ["decode"]
    assert modal_api.calls == [("analyze_stream", (b"mp3 bytes", ".mp3", None, {"beats_per_bar": 3}))]
    url, headers = modal_api.sent[0]
    assert url == "https://modal.example/analyze_stream"
    assert headers["Modal-Key"] == "wk-1" and headers["Modal-Secret"] == "ws-1"
    remote.analyze(song, {"drum_model": True})
    assert modal_api.calls[-1][0] == "analyze_drums"


def test_remote_stems_over_https_and_errors(modal_api, tmp_path):
    from tastr.cloud import remote

    song = tmp_path / "a.wav"
    song.write_bytes(b"wav")
    assert remote.stems(song) == {"drums": b"flac", "bass": b"flac2"}  # bytes come back base64-encoded
    modal_api.mod.stems = modal_api.Fn("stems", fail="out of GPUs")
    with pytest.raises(remote.RemoteError, match="out of GPUs"):
        remote.stems(song, {"drums": "https://put"})
    assert modal_api.calls[-1] == ("stems", (b"wav", ".wav", {"drums": "https://put"}))
    with pytest.raises(remote.RemoteError, match="404"):
        remote._http_result("nothing", remote._payload("https://audio", ".wav"))


def test_analysis_backend_modal(monkeypatch, tmp_path, db_path):
    """A hosted server sends the audio to the `analyze` function of the app TASTR_MODAL_APP names."""
    from tastr import db, service
    from tastr.engine.card import Source
    from test_foundation import full_card
    monkeypatch.delenv("TASTR_ENGINE", raising=False)  # set for the suite (conftest.py); unset, nothing extra goes

    remote_card = full_card()
    remote_card.source = Source(kind="file", uri="/tmp/x/track.mp3", local_path="/tmp/x/track.mp3")
    apps = []
    calls = fake_modal(monkeypatch, remote_card.to_dict())
    fn = sys.modules["modal"].Function
    real = fn.from_name
    fn.from_name = lambda app, name: apps.append(app) or real(app, name)
    monkeypatch.setenv("TASTR_ANALYSIS_BACKEND", "modal")
    monkeypatch.setenv("TASTR_MODAL_APP", "tastr-web")
    monkeypatch.setenv("TASTR_LOOKUP", "")
    song = tmp_path / "song.mp3"
    song.write_bytes(b"mp3 bytes")
    src = Source(kind="file", uri=str(song), local_path=str(song), title="song")
    r = service.analyze_source(db.connect(db_path), src, {"beats_per_bar": 3}, owner="a@b.c")
    assert r.error is None and apps == ["tastr-web"]
    assert calls == [("analyze", (b"mp3 bytes", ".mp3", None, {"beats_per_bar": 3}), {})]
    assert r.card.source is src and r.card.core.bpm == 124.0
    assert db.load_card(db.connect(db_path), r.track_id).source.local_path == str(song)


def test_remote_analyze_reports_stages_from_analyze_stream(monkeypatch, tmp_path):
    """With a stage callback the server calls the generator `analyze_stream`; an app without it falls back to `analyze`."""
    from tastr.cloud import remote
    from test_foundation import full_card

    card = full_card().to_dict()
    calls, missing = [], set()

    class Fn:
        def __init__(self, name):
            self.name = name

        def hydrate(self):
            if self.name in missing:
                raise LookupError(self.name)

        def remote_gen(self, *a):
            calls.append(self.name)
            yield {"stage": "decode", "i": 0, "n": 1}
            yield {"stage": "core", "i": 0, "n": 5}
            yield {"card": card}

        def remote(self, *a):
            calls.append(self.name)
            return card

    fake = types.SimpleNamespace(Function=types.SimpleNamespace(from_name=lambda app, name: Fn(name)),
                                 exception=types.SimpleNamespace(NotFoundError=LookupError))
    monkeypatch.setitem(sys.modules, "modal", fake)
    song = tmp_path / "song.wav"
    song.write_bytes(b"wav")
    seen = []
    assert remote.analyze(song, on_stage=lambda s, i, n: seen.append(s)).core.bpm == 124.0
    assert seen == ["decode", "core"] and calls == ["analyze_stream"]
    missing.add("analyze_stream")
    calls.clear()
    assert remote.analyze(song, on_stage=seen.append).core.bpm == 124.0 and calls == ["analyze"]


@pytest.mark.rust
def test_run_analyze_stream_yields_stages_then_the_card(synth_wav):
    from tastr.engine.card import TrackCard

    path, _ = synth_wav
    events = list(jobs.run_analyze_stream(path.read_bytes(), ".wav"))
    assert [e["stage"] for e in events[:-1]] == ["decode", "core"]  # Rust reports one step for all its stages
    assert TrackCard.from_dict(events[-1]["card"]).core.bpm


@pytest.mark.rust
def test_run_analyze_returns_a_card(synth_wav):
    from tastr.engine.card import TrackCard

    path, _ = synth_wav
    card = TrackCard.from_dict(jobs.run_analyze(path.read_bytes(), ".wav", ["core"]))  # older servers still send stages
    assert card.duration and card.core.bpm
