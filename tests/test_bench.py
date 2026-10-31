import json

import pytest

from synth import make_track, write_wav
from tastr.engine.bench import datasets, metrics, runner
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


def test_beatport_key_loader(tmp_path):
    (tmp_path / "keys").mkdir()
    (tmp_path / "audio").mkdir()
    labels = {"201": "Bb minor", "202": "E minor | E major", "203": "X", "204": "F minor phrygian", "205": "D major"}
    for sid, label in labels.items():
        (tmp_path / "keys" / f"{sid} Artist - Title (Original Mix).txt").write_text(label)
        (tmp_path / "audio" / f"{sid} Artist - Title (Original Mix).mp3").write_bytes(b"")
    (tmp_path / "keys" / "._201 Artist - Title (Original Mix).txt").write_bytes(b"\x00")
    (tmp_path / "audio" / "205 Artist - Title (Original Mix).mp3").unlink()  # no audio: skipped
    keys = datasets.beatport_key(tmp_path)
    assert [(i.id, i.truth.tonic, i.truth.mode) for i in keys] == [("bp-key-201", 10, "minor")]


def test_fma_keys_loader(tmp_path):
    (tmp_path / "fma_keys_metadata.csv").write_text(
        "track_id,spotify_uri,key_and_mode\n10,spotify:track:x,F# Major\n141,,F minor\n200,,F# Major\n")
    for name in ("000/000010.mp3", "000/000141.mp3", "000/._000141.mp3"):  # 200 isn't fetched
        (tmp_path / "audio" / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "audio" / name).write_bytes(b"x")
    items = datasets.load("fma-keys", str(tmp_path))
    assert [(i.id, i.truth.tonic, i.truth.mode) for i in items] == [("fmak-10", 6, "major"), ("fmak-141", 5, "minor")]


def test_beatport_tempo_loader(tmp_path):
    (tmp_path / "meta").mkdir()
    (tmp_path / "audio").mkdir()
    metas = {"301": '{"id": 301, "bpm": 174}', "302": '{"id": 302, "bpm": nan, "genres": []}',
             "303": '{"id": 303, "bpm": 0}', "304": '{"id": 304}', "305": '{"id": 305, "bpm": 128}'}
    for sid, text in metas.items():
        (tmp_path / "meta" / f"{sid} Artist - Title (Original Mix).json").write_text(text)
        (tmp_path / "audio" / f"{sid} Artist - Title (Original Mix).mp3").write_bytes(b"")
    (tmp_path / "meta" / "._301 Artist - Title (Original Mix).json").write_bytes(b"\x00")
    (tmp_path / "audio" / "305 Artist - Title (Original Mix).mp3").unlink()  # no audio: skipped
    tempos = datasets.beatport_tempo(tmp_path)
    assert [(i.id, i.truth.bpm) for i in tempos] == [("bp-tempo-301", 174.0)]


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


def test_parallel_run_warms_up_in_parent(monkeypatch):
    calls = []

    class FakePool:
        def __init__(self, jobs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def map(self, fn, work):
            calls.append([w[0].id for w in work])
            return map(fn, work)

    monkeypatch.setattr(runner, "ProcessPoolExecutor", FakePool)
    monkeypatch.setattr(runner, "_work", lambda w: {"id": w[0].id})
    items = [datasets.Item(id=f"t{i}", truth=datasets.Truth()) for i in range(3)]
    report = runner.run(items, jobs=2)
    assert calls == [["t1", "t2"]]  # t0 ran in the parent first
    assert [r["id"] for r in report["rows"]] == ["t0", "t1", "t2"]


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


def _scale_tracks(rng, n):
    """Noisy major/minor scale profiles in random keys, shaped like cached key features."""
    import numpy as np

    out = []
    for i in range(n):
        mode = "minor" if i % 2 else "major"
        base = np.full(12, 0.02)
        base[[0, 2, 4, 5, 7, 9, 11] if mode == "major" else [0, 2, 3, 5, 7, 8, 10]] = 0.1
        base[[0, 7]] = [0.2, 0.16]
        tonic = int(rng.integers(12))
        p = np.roll(base + rng.random(12) * 0.03, tonic)
        out.append({"profile": p / p.sum(), "tonic": tonic, "mode": mode, "silent": False, "label_conf": -1})
    return out


def test_trained_key_module_reproduces_its_fit(tmp_path):
    import importlib.util

    import numpy as np

    from tastr.engine import music
    from tastr.engine.bench import train_key

    tracks = _scale_tracks(np.random.default_rng(1), 40)
    weights, bias = train_key.fit(tracks)
    assert weights.shape == (24, 12) and bias.shape == (24,)
    assert train_key.score(tracks, weights, bias)["exact"] == 1.0

    path = tmp_path / "key_model.py"
    path.write_text(train_key.module_source(weights, bias, {"synth": len(tracks)}))
    spec = importlib.util.spec_from_file_location("key_model_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert np.array_equal(mod.WEIGHTS, weights) and np.array_equal(mod.BIAS, bias)  # repr round-trips exactly
    p = tracks[0]["profile"]
    assert np.array_equal(music.key_probabilities(p, mod.WEIGHTS, mod.BIAS), music.key_probabilities(p, weights, bias))


def test_numpy_key_model_matches_sklearn():
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    from tastr.engine import music
    from tastr.engine.bench import train_key

    tracks = _scale_tracks(np.random.default_rng(2), 30)
    X = np.array([music.KEY_INPUT_SCALE * np.roll(t["profile"], s) for t in tracks for s in range(12)])
    y = np.array([((t["tonic"] + s) % 12) * 2 + (t["mode"] == "minor") for t in tracks for s in range(12)])
    clf = LogisticRegression(C=train_key.C, max_iter=2000).fit(X, y)
    ours = np.array([music.key_probabilities(x / music.KEY_INPUT_SCALE, clf.coef_, clf.intercept_) for x in X])
    assert np.array_equal(ours.argmax(axis=1), clf.predict(X))
    assert np.allclose(ours, clf.predict_proba(X), atol=1e-12)


def test_unsure_labels_are_left_out_of_training():
    import numpy as np

    from tastr.engine.bench import train_key

    t = {"profile": np.full(12, 1 / 12), "silent": False}
    tracks = [t | {"label_conf": c} for c in (-1, 0, 1, 2)] + [t | {"label_conf": 2, "silent": True}]
    assert [x["label_conf"] for x in train_key.usable(tracks)] == [-1, 2]


def test_calibration_fit_is_monotone_and_pooled():
    import numpy as np

    from tastr.engine.bench import calibrate

    rng = np.random.default_rng(3)
    x = rng.random(2000)
    y = (rng.random(2000) < 0.2 + 0.6 * x).astype(float)  # truly 20% right at 0, 80% at 1
    knots = calibrate.fit(x, y)
    rates = [p for _, p, _ in knots]
    assert rates == sorted(rates)
    assert all(n >= calibrate.MIN_BLOCK for *_, n in knots)
    assert sum(n for *_, n in knots) == 2000
    assert abs(calibrate.predict(knots, np.array([0.1]))[0] - 0.26) < 0.08
    assert abs(calibrate.predict(knots, np.array([0.9]))[0] - 0.74) < 0.08
    h = calibrate.holdout(x, y)
    assert h["brier_calibrated"] < h["brier_flat"]


def test_calibration_smooths_small_certain_blocks():
    import numpy as np

    from tastr.engine.bench import calibrate

    knots = calibrate.fit(np.linspace(0, 1, 10), np.ones(10), min_block=5)
    assert len(knots) == 1 and knots[0][1] == round(10.5 / 11, 4)


def test_calibration_module_round_trips(tmp_path, monkeypatch):
    import importlib.util

    from tastr.engine import calibration
    from tastr.engine.bench import calibrate

    tables = {"bpm": [(0.2, 0.5, 50), (0.8, 0.9, 60)], "key": [(0.1, 0.3, 40), (0.5, 0.7, 45)]}
    path = tmp_path / "calibration_table.py"
    path.write_text(calibrate.module_source(tables, {"bpm": ["bp.json"], "key": ["key.json"]}))
    spec = importlib.util.spec_from_file_location("calibration_table", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.BPM == tuple(tables["bpm"]) and mod.KEY == tuple(tables["key"])

    monkeypatch.setitem(__import__("sys").modules, "tastr.engine.calibration_table", mod)
    import tastr.engine as engine
    monkeypatch.setattr(engine, "calibration_table", mod, raising=False)
    assert calibration.likely_correct("bpm", 0.5) == 0.7
    assert calibration.likely_correct("bpm", 0.0) == 0.5  # flat beyond the fitted range
    assert calibration.likely_correct("key", 0.9) == 0.7
    assert calibration.likely_correct("bpm", None) is None


def test_calibrate_reads_reports_and_key_rows(tmp_path):
    from tastr.engine.bench import calibrate

    (tmp_path / "r.json").write_text(json.dumps({"rows": [
        {"bpm_confidence": 0.9, "acc1": 1.0}, {"bpm_confidence": 0.3, "acc1": 0.0}, {"est_bpm": None}]}))
    (tmp_path / "s.json").write_text(json.dumps({"tracks": [{"bpm_confidence": 0.5, "acc1": 1.0}]}))
    x, y = calibrate.read_pairs([str(tmp_path / "r.json"), str(tmp_path / "s.json")], "bpm")
    assert list(x) == [0.9, 0.3, 0.5] and list(y) == [1.0, 0.0, 1.0]
    with pytest.raises(SystemExit):
        calibrate.main(["--bpm", str(tmp_path / "r.json"), "--write"])


def test_key_holdout_writes_calibration_rows():
    import numpy as np

    from tastr.engine.bench import train_key

    tracks = _scale_tracks(np.random.default_rng(4), 40)
    rows: list = []
    train_key.holdout({"major": tracks[0::2], "minor": tracks[1::2]}, [], rows=rows)  # both modes in each half
    assert len(rows) == 40 and all({"key_confidence", "key_correct", "dataset"} <= r.keys() for r in rows)


def test_record_keeps_full_run_scores(tmp_path):
    from tastr.engine.bench import scores

    path = tmp_path / "scores.json"
    assert scores.load(path) == {"scores": {}}
    report = {"summary": {"tracks": 600, "failed": 1, "key_correct": 0.61234, "key_correct_n": 599}}
    key, entry = scores.entry_from_report("bench", "giantsteps-key", report, run="2026-11-01")
    scores.record(key, entry, path)
    assert scores.load(path)["scores"]["giantsteps-key"] == {"exact": 0.612, "tracks": 599, "what": "tracks, exact key", "run": "2026-11-01"}
    key, entry = scores.entry_from_report("downbeats", "gtzan", {"tracks": 933, "right": 0.66, "half_bar_off": 0.1})
    scores.record(key, entry, path)
    assert set(scores.load(path)["scores"]) == {"giantsteps-key", "downbeat"}
    assert not scores.recordable("bench", "harmonix") and not scores.recordable("downbeats", "hjdb")
    assert scores.recordable("sections", "harmonix")
    sec = {"tracks": 4, "tune_tracks": 2, "current": {"tune": {"f_3": 0.6}, "check": {"f_3": 0.5}}}
    assert scores.entry_from_report("sections", "harmonix", sec, run="2026-11-01") == (
        "sections", {"exact": 0.55, "tracks": 4, "what": "songs, boundary F within 3 s", "run": "2026-11-01"})


def test_record_refuses_subset_runs(capsys):
    assert main(["bench", "giantsteps-key", "/nowhere", "--limit", "20", "--record"]) == 2
    assert "full run" in capsys.readouterr().err
    assert main(["bench", "harmonix", "/nowhere", "--record"]) == 2
