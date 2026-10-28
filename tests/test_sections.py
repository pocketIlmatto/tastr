import numpy as np
import pytest
from synth import make_track

from tastr.card import Core, Series, Source, TrackCard
from tastr.pipeline import AnalysisContext, run_pipeline
from tastr.stages.sections import SectionsStage


def _run(y, sr, downbeats=None, bpm=124.0, series=None):
    card = TrackCard(source=Source(kind="file", uri="x"))
    card.core = Core(bpm=bpm, downbeat_times=list(downbeats or []))
    card.duration = round(y.size / sr, 3)
    card.sample_rate = sr
    if series:
        card.series.update(series)
    SectionsStage().run(AnalysisContext(y=y, sr=sr, card=card))
    return card


def _bars(truth, n):
    return [i * truth["bar_seconds"] for i in range(n)]


def _check_valid(card, min_bars=4):
    secs = card.sections
    assert secs and secs[0].start == 0.0
    assert secs[-1].end == pytest.approx(card.duration)
    for a, b in zip(secs, secs[1:]):
        assert a.end == b.start
    for s in secs:
        assert s.end > s.start
        assert s.label in {"intro", "verse", "chorus", "build", "drop", "breakdown", "outro", "full"}
        assert s.letter and 0 <= s.energy <= 1
        if min_bars:
            assert s.end_bar - s.start_bar + 1 >= min_bars


@pytest.fixture(scope="module")
def synth():
    y, truth = make_track(bpm=124, bars_per_section=(8, 16, 8))
    return y, truth


def test_synth_boundaries_energy_and_labels(synth):
    y, truth = synth
    card = _run(y, 22050, _bars(truth, 32))
    _check_valid(card)
    bar = truth["bar_seconds"]
    bounds = [s.start for s in card.sections[1:]]
    for target in (15.5, 46.5):
        assert min(abs(b - target) for b in bounds) <= bar
    intro, outro = card.sections[0], card.sections[-1]
    drum = max(card.sections, key=lambda s: s.energy)
    assert intro.label == "intro"
    assert drum.label in {"drop", "chorus"}
    assert drum.energy > intro.energy and drum.energy > outro.energy
    assert card.features["section_count"] == len(card.sections)
    assert card.features["section_contrast"] > 0.1


def test_partial_trailing_bar_does_not_hide_a_boundary(synth):
    # The real core stage extends the beat grid over the whole file, so the synth's
    # 1 s tail becomes a 33rd, partial bar. It must not use up a cluster.
    y, truth = synth
    card = _run(y, 22050, _bars(truth, 33))
    _check_valid(card)
    bounds = [s.start for s in card.sections[1:]]
    for target in (15.5, 46.5):
        assert min(abs(b - target) for b in bounds) <= truth["bar_seconds"]
    assert card.sections[0].label == "intro"


def test_no_downbeats_falls_back_to_blocks(synth):
    y, _ = synth
    card = _run(y, 22050, [])
    _check_valid(card, min_bars=0)
    assert card.sections[-1].end == pytest.approx(card.duration)


def test_short_track_is_one_full_section():
    y, truth = make_track(bpm=124, bars_per_section=(2, 2, 2))
    card = _run(y, 22050, _bars(truth, 6))
    assert [s.label for s in card.sections] == ["full"]
    assert card.sections[0].end == pytest.approx(card.duration)


def test_uses_core_energy_series_when_present(synth):
    y, truth = synth
    t = np.arange(0, 80, 1.0)
    flat = Series(times=list(t), values=[0.5] * len(t))
    card = _run(y, 22050, _bars(truth, 32), series={"energy": flat})
    _check_valid(card)


def test_through_pipeline_with_truth_core(synth_wav, truth_core):
    import soundfile as sf

    from tastr.card import Source

    path, truth = synth_wav
    y, sr = sf.read(str(path), dtype="float32")
    card = run_pipeline(y, sr, TrackCard(source=Source(kind="file", uri=str(path))), stages=["sections"])
    assert "sections" not in card.errors, card.errors
    assert card.stages_run["sections"] == "0.1.0"
    _check_valid(card)
