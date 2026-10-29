import json

import pytest

from synth import make_track, write_wav
from tastr.bench import datasets, metrics, runner
from tastr.cli import main


def test_tempo_scores():
    assert metrics.tempo_scores(124.5, 124) == {"acc1": 1.0, "acc2": 1.0, "octave": None}
    assert metrics.tempo_scores(87, 174) == {"acc1": 0.0, "acc2": 1.0, "octave": "/2"}
    assert metrics.tempo_scores(140, 70) == {"acc1": 0.0, "acc2": 1.0, "octave": "x2"}
    assert metrics.tempo_scores(100, 124)["acc2"] == 0.0
    assert metrics.tempo_scores(None, 124)["acc1"] == 0.0


@pytest.mark.parametrize("est,ref,cat", [
    ((9, "minor"), (9, "minor"), "correct"),
    ((0, "major"), (9, "minor"), "relative"),  # C major for A minor: the known core bug
    ((9, "minor"), (0, "major"), "relative"),
    ((4, "minor"), (9, "minor"), "fifth"),
    ((2, "minor"), (9, "minor"), "fifth"),
    ((9, "major"), (9, "minor"), "parallel"),
    ((1, "major"), (9, "minor"), "other"),
])
def test_key_category(est, ref, cat):
    assert metrics.key_category(*est, *ref) == cat


def test_boundary_scores_ignore_track_edges():
    s = metrics.boundary_scores([0.0, 31.0, 64.5], [0.0, 32.0, 64.0], duration=96.0)
    assert s["boundary_f_3"] == 1.0
    assert s["boundary_f_0.5"] == 0.5
    assert metrics.boundary_scores([0.0], [0.0, 32.0], duration=64.0)["boundary_f_3"] == 0.0


def test_beat_scores_window():
    ref = [5 + i * 0.5 for i in range(20)]
    assert metrics.beat_scores([t + 0.05 for t in ref], ref)["beat_f"] == 1.0
    assert metrics.beat_scores([t + 0.25 for t in ref], ref)["beat_f"] == 0.0


@pytest.mark.parametrize("text,key", [
    ("A minor", (9, "minor")), ("Ebm", (3, "minor")), ("f# maj", (6, "major")), ("C#:min", (1, "minor")),
    ("Bb", (10, "major")), ("8A", (9, "minor")), ("8B", (0, "major")), ("12A", (1, "minor")), ("nope", None),
])
def test_parse_key(text, key):
    assert datasets.parse_key(text) == key


def test_parse_time():
    assert datasets.parse_time("83.5") == 83.5
    assert datasets.parse_time("1:23.5") == 83.5
    assert datasets.parse_time("1:00:00") == 3600


def test_giantsteps_loaders(tmp_path):
    (tmp_path / "annotations" / "key").mkdir(parents=True)
    (tmp_path / "annotations" / "tempo").mkdir(parents=True)
    (tmp_path / "audio").mkdir()
    (tmp_path / "annotations" / "key" / "101.LOFI.key").write_text("Eb minor\n")
    (tmp_path / "annotations" / "key" / "102.LOFI.key").write_text("G major")  # no audio: skipped
    (tmp_path / "annotations" / "tempo" / "101.LOFI.bpm").write_text("126.0")
    (tmp_path / "audio" / "101.LOFI.mp3").write_bytes(b"")
    keys = datasets.giantsteps_key(tmp_path)
    assert [(i.id, i.truth.tonic, i.truth.mode) for i in keys] == [("gs-key-101", 3, "minor")]
    tempos = datasets.giantsteps_tempo(tmp_path)
    assert tempos[0].truth.bpm == 126.0


def test_harmonix_loader(tmp_path):
    ds = tmp_path / "dataset"
    (ds / "beats_and_downbeats").mkdir(parents=True)
    (ds / "segments").mkdir()
    (tmp_path / "audio").mkdir()
    (ds / "metadata.csv").write_text("File,BPM\n0001_song,120\n")
    (ds / "beats_and_downbeats" / "0001_song.txt").write_text("0.5\t1\t1\n1.0\t2\t1\n1.5\t3\t1\n2.0\t4\t1\n2.5\t1\t2\n")
    (ds / "segments" / "0001_song.txt").write_text("0.0 intro\n16.5 verse\n40.0 end\n")
    (tmp_path / "audio" / "0001_song.mp3").write_bytes(b"")
    (item,) = datasets.harmonix(tmp_path)
    assert item.truth.bpm == 120
    assert item.truth.downbeats == [0.5, 2.5]
    assert item.truth.boundaries == [0.0, 16.5]
    assert item.truth.labels == ["intro", "verse"]


def test_manifest_csv(tmp_path):
    (tmp_path / "a.wav").write_bytes(b"")
    m = tmp_path / "mine.csv"
    m.write_text('path,bpm,key,boundaries,labels\na.wav,124,Am,"0:00 0:32 1:04",intro build drop\n')
    (item,) = datasets.manifest(m)
    assert item.path == str(tmp_path / "a.wav")
    assert (item.truth.bpm, item.truth.tonic, item.truth.mode) == (124.0, 9, "minor")
    assert item.truth.boundaries == [0.0, 32.0, 64.0]


def test_review_queue_puts_confident_mistakes_first():
    rows = [
        {"id": "ok-sure", "key_correct": 1.0, "key_confidence": 0.9},
        {"id": "ok-unsure", "key_correct": 1.0, "key_confidence": 0.1},
        {"id": "wrong-unsure", "key_correct": 0.0, "key_confidence": 0.2},
        {"id": "wrong-sure", "key_correct": 0.0, "key_confidence": 0.8},
    ]
    assert [r["id"] for r in runner.review_queue(rows)] == ["wrong-sure", "wrong-unsure", "ok-unsure", "ok-sure"]


@pytest.mark.audio
def test_bench_cli_on_manifest(tmp_path, capsys):
    y, truth = make_track(bpm=124, tonic=9, mode="minor", bars_per_section=(2, 4, 2))
    write_wav(tmp_path / "t.wav", y)
    (tmp_path / "m.json").write_text(json.dumps([{"path": "t.wav", "bpm": 124, "key": "A minor",
                                                  "boundaries": [s["start"] for s in truth["sections"]]}]))
    out = tmp_path / "report.json"
    assert main(["bench", "manifest", str(tmp_path / "m.json"), "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    s = report["summary"]
    assert s["tracks"] == 1 and s["failed"] == 0
    assert s["acc1"] == 1.0
    assert {"key_weighted", "boundary_f_3"} <= s.keys()
    assert "BPM within 4%" in capsys.readouterr().out
    assert main(["bench", "manifest", str(tmp_path / "m.json"), "--baseline", str(out)]) == 0
    assert "vs baseline" in capsys.readouterr().out


def test_bench_cli_errors(tmp_path):
    assert main(["bench", "nope"]) == 2
    assert main(["bench", "giantsteps-key"]) == 2
    assert main(["bench", "giantsteps-key", str(tmp_path)]) == 2  # nothing found
