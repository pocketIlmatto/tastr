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

# Analyses and benchmarks in the tests run on the Rust engine, as they do in the app; they need its library built
# (cd engine-rs && cargo build --release --lib), which CI does whenever the audio tests run.
from tastr.engine import rust as _rust  # noqa: E402
from synth import make_track, write_wav  # noqa: E402


if os.environ.get("TASTR_TEST_DO_SQLITE"):
    # Run the suite with every db.connect() going through the Durable Object adapter (tastr/do_sqlite.py), as the
    # Cloudflare Worker does (TAS-84). Tests of local-only tools (pack/unpack, the local SQL box, old-database
    # migrations) are expected to fail this way.
    @pytest.fixture(autouse=True)
    def _durable_object_db():
        from tastr import db, do_sqlite

        stores: dict = {}
        db.use_storage(lambda path: stores.setdefault(str(path), do_sqlite.FakeStorage(path)))
        yield
        db.use_storage(None)  # the stores stay open: a test's background thread may still be finishing


@pytest.hookimpl(tryfirst=True)  # before -m "not audio" deselects
def pytest_collection_modifyitems(config, items):
    """A `rust` test analyzes audio through the app: it is an audio test, and skips without the library."""
    missing = pytest.mark.skip(reason="needs the Rust engine built (cd engine-rs && cargo build --release --lib)")
    for item in items:
        if item.get_closest_marker("rust"):
            item.add_marker(pytest.mark.audio)
            if not _rust.available():
                item.add_marker(missing)


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

