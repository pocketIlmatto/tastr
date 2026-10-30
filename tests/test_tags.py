from tastr.engine.card import Core, Key, Source, TrackCard
from tastr.engine.pipeline import AnalysisContext
from tastr.engine.stages.tags import TagsStage

import numpy as np


def run(bpm=None, mode=None, energy=None, **features):
    key = Key(tonic=0, mode=mode) if mode else None
    card = TrackCard(source=Source(kind="file", uri="x"), core=Core(bpm=bpm, key=key, energy=energy), features=features)
    TagsStage().run(AnalysisContext(y=np.zeros(1, dtype=np.float32), sr=22050, card=card))
    return {t.tag: t.score for t in card.tags}, card.tags


def test_bright_major_house():
    tags, _ = run(bpm=126, mode="major", energy=0.9, brightness=0.7)
    for t in ("high energy", "bright", "euphoric", "house tempo", "techno tempo"):
        assert t in tags
    assert "chill" not in tags and "dark" not in tags


def test_dark_minor_downtempo():
    tags, _ = run(bpm=85, mode="minor", energy=0.2, brightness=0.2)
    for t in ("chill", "dark", "melancholic", "downtempo"):
        assert t in tags
    assert "high energy" not in tags


def test_only_bpm_gives_only_tempo_tags():
    tags, _ = run(bpm=126)
    assert set(tags) == {"house tempo", "techno tempo"}


def test_sorted_thresholded_unique():
    _, listed = run(bpm=126, mode="major", energy=0.9, brightness=0.7, pulse_clarity=0.9, percussiveness=0.6)
    scores = [t.score for t in listed]
    assert scores == sorted(scores, reverse=True)
    assert all(0.2 <= s <= 1 for s in scores)
    assert len({t.tag for t in listed}) == len(listed)


def test_swing_needs_confidence():
    assert "swung" not in run(bpm=100, swing_percent=62, swing_confidence=0.1)[0]
    assert "swung" in run(bpm=100, swing_percent=62, swing_confidence=0.6)[0]


def test_scores_under_cutoff_are_kept_but_not_listed():
    from tastr.engine.stages.tags import MIN_SCORE, vibe_scores

    # energy 0.62 gives "high energy" a score of about 0.07: positive, but under the cut-off
    card = TrackCard(source=Source(kind="file", uri="x"), core=Core(energy=0.62), features={})
    TagsStage().run(AnalysisContext(y=np.zeros(1, dtype=np.float32), sr=22050, card=card))
    scores = card.extras["tags.vibe_scores"]
    assert 0 < scores["high energy"] < MIN_SCORE
    assert "high energy" not in {t.tag for t in card.tags}
    assert all(scores[t.tag] == t.score for t in card.tags)
    assert list(scores.values()) == sorted(scores.values(), reverse=True)
    assert scores == vibe_scores(None, None, 0.62, {})


def test_trap_tempo_covers_full_and_half_tempo_reads():
    # 140 sits in trap, dubstep/halftime and techno ranges at once: tempo tags are hints, not a genre call
    assert set(run(bpm=140)[0]) == {"trap tempo", "dubstep/halftime tempo", "techno tempo"}
    assert set(run(bpm=165)[0]) == {"trap tempo", "drum & bass tempo"}
    assert set(run(bpm=132)[0]) == {"trap tempo", "techno tempo"}
    # trap's halftime feel often comes out at half tempo
    assert set(run(bpm=70)[0]) == {"trap tempo", "downtempo"}
    assert "trap tempo" not in run(bpm=110)[0]
    assert "trap tempo" not in run(bpm=172)[0]
    assert run(bpm=140)[0]["trap tempo"] == 0.5
