import os
import sys
import tempfile
from pathlib import Path

import pytest

# Under pytest-xdist every worker JIT-compiles librosa's numba kernels at once; sharing one on-disk
# numba cache between them segfaulted a CI worker, so give each worker its own (set before numba loads).
if os.environ.get("PYTEST_XDIST_WORKER"):
    os.environ["NUMBA_CACHE_DIR"] = str(Path(tempfile.gettempdir()) / f"numba-{os.environ['PYTEST_XDIST_WORKER']}")

sys.path.insert(0, str(Path(__file__).parent))

# Analyses in the tests run on the Rust engine, as they do in the app, whenever its library is built
# (cd engine-rs && cargo build --release --lib); CI always builds it. Without it they run on the Python engine
# and the Rust tests skip. Tests of the Python reference engine's stage logic call its stages directly, and
# fixtures that swap a Python stage (truth_core) switch TASTR_ENGINE to python themselves.
from tastr.engine import rust as _rust  # noqa: E402

os.environ.setdefault("TASTR_ENGINE", "rust" if _rust.lib_path() else "python")

from synth import make_track, write_wav  # noqa: E402


@pytest.fixture(scope="session")
def synth_wav(tmp_path_factory):
    """Path to a 124 BPM A minor synthetic track (see tests/synth.py for ground truth)."""
    y, truth = make_track(bpm=124, tonic=9, mode="minor")
    path = tmp_path_factory.mktemp("audio") / "synth_124_Am.wav"
    write_wav(path, y)
    return path, truth


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def truth_core(synth_wav, monkeypatch):
    """Swap the core stage for one that reports the synth track's ground truth. The swap is a Python stage, so
    analyses in the test run on the Python engine."""
    monkeypatch.setenv("TASTR_ENGINE", "python")
    from synth import TruthCoreStage

    from tastr.engine.pipeline import register_stage, registered_stages

    original = registered_stages()["core"]
    register_stage(TruthCoreStage(synth_wav[1]), replace=True)
    yield
    register_stage(original, replace=True)
