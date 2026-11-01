import ast
import subprocess
import sys
from pathlib import Path

import pytest

import tastr.engine
from tastr.engine import analyze_file, rust

ENGINE = Path(tastr.engine.__file__).parent


def test_engine_imports_nothing_from_the_rest_of_tastr():
    """The engine should stay liftable into its own library."""
    bad = []
    for f in ENGINE.rglob("*.py"):
        depth = len(f.relative_to(ENGINE).parts) - 1  # package levels below tastr/engine
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if node.level > depth + 1 or (node.level == 0 and mod.startswith("tastr") and not mod.startswith("tastr.engine")):
                    bad.append(f"{f.relative_to(ENGINE)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                bad += [f"{f.relative_to(ENGINE)}:{node.lineno}" for a in node.names
                        if a.name.startswith("tastr") and not a.name.startswith("tastr.engine")]
    assert not bad


def test_engine_import_does_not_load_app_modules():
    code = "import sys, tastr.engine, tastr.engine.bench; print(sorted(m for m in sys.modules if m.startswith('tastr') and not m.startswith('tastr.engine')))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "['tastr']"


@pytest.mark.skipif(not rust.available(), reason="needs the Rust engine built")
def test_analyze_file(synth_wav):
    card = analyze_file(synth_wav[0])
    assert card.source.title == "synth_124_Am"
    assert abs(card.core.bpm - 124) < 1
    assert card.stages_run == rust.STAGES


def test_hpss_matches_librosa_sample_for_sample():
    """signals.hpss is a faster route to librosa.effects.hpss; every stage tuned on librosa's output relies on it
    being the same, including clips shorter than the 31-frame median window."""
    import librosa
    import numpy as np

    from tastr.engine import signals
    from tastr.engine.card import Source, TrackCard
    from tastr.engine.pipeline import AnalysisContext

    rng = np.random.default_rng(0)
    for n in (3000, 7700, 22050 * 6):
        y = rng.standard_normal(n).astype(np.float32)
        ctx = AnalysisContext(y=y, sr=22050, card=TrackCard(source=Source(kind="file", uri="x")))
        for ours, theirs in zip(signals.hpss(ctx), librosa.effects.hpss(y)):
            assert ours.dtype == theirs.dtype and np.array_equal(ours, theirs)
