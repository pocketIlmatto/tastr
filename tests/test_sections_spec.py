"""Harmonix sections bench on a synthetic mel-spectrogram layout."""

import json

import numpy as np

from tastr.cloud import jobs
from tastr.engine.bench import sections_spec


def _harmonix(tmp_path, n_tracks=4):
    """Tracks at 120 BPM (2 s bars) whose mel timbre changes every 16 bars, labeled there."""
    sr, hop, bands = 22050, 1024, 80
    fps = sr / hop
    rng = np.random.default_rng(1)
    mels = {}
    for t in range(n_tracks):
        sid = f"{t:04d}_song"
        bounds = [0.0, 32.0, 64.0, 96.0, 128.0]
        frames = int(160 * fps)
        mel = np.zeros((bands, frames))
        for i, b in enumerate(bounds):
            a = int(b * fps)
            mel[:, a:] = rng.gamma(2.0, 1.0, size=(bands, 1)) * (i + 1)
        mels[sid] = mel + 0.01 * rng.random(mel.shape)
        seg = tmp_path / "dataset" / "segments" / f"{sid}.txt"
        seg.parent.mkdir(parents=True, exist_ok=True)
        seg.write_text("".join(f"{b} part{i}\n" for i, b in enumerate(bounds)) + "160.0 end\n")
        beats = tmp_path / "dataset" / "beats_and_downbeats" / f"{sid}.txt"
        beats.parent.mkdir(parents=True, exist_ok=True)
        beats.write_text("".join(f"{k * 0.5}\t{k % 4 + 1}\t{k // 4 + 1}\n" for k in range(320)))
    np.savez(tmp_path / "melspecs.npz", **mels)
    (tmp_path / "melspecs-info.json").write_text(json.dumps({"SR": sr, "HOP_LENGTH": hop, "N_MELS": bands, "MEL_FMIN": 0, "MEL_FMAX": None}))


def test_sections_spec_finds_labeled_boundaries(tmp_path):
    _harmonix(tmp_path)
    report = jobs.run_spec_bench("sections", "harmonix", tmp_path)
    assert report["tracks"] == 4 and report["tune_tracks"] == 2
    assert report["info"]["hop"] == 1024 and report["rows"][0]["duration"] == 160.0
    best = report["best_on_tune"]
    assert best["tune"]["f_3"] > 0.9  # the timbre changes are obvious, so some setting finds them all
    from tastr.engine.stages import sections

    assert (report["current"]["seconds"], report["current"]["phrase"]) == (sections.SECONDS_PER_SECTION, sections.PHRASE_BARS)
    assert "best on tune" in sections_spec.format_report(report)


def test_sections_spec_reads_the_annotation_bundle(tmp_path):
    _harmonix(tmp_path)
    from tastr.engine.bench.sections_spec import _annotations

    ids = sorted(p.stem for p in (tmp_path / "dataset" / "segments").glob("*.txt"))
    bundle = {}
    for sid in ids:
        d, b, e = _annotations(tmp_path / "dataset", sid)
        bundle[sid] = {"downbeats": d, "boundaries": b, "end": e}
    import shutil

    shutil.rmtree(tmp_path / "dataset")
    (tmp_path / "annotations.json").write_text(json.dumps(bundle))
    assert jobs.run_spec_bench("sections", "harmonix", tmp_path)["tracks"] == 4
