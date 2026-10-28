import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

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
def truth_core(synth_wav):
    """Swap the core stage for one that reports the synth track's ground truth."""
    from synth import TruthCoreStage

    from tastr.pipeline import register_stage, registered_stages

    original = registered_stages()["core"]
    register_stage(TruthCoreStage(synth_wav[1]), replace=True)
    yield
    register_stage(original, replace=True)
