"""Drum lanes on synthetic loops with known patterns (tastr.engine.drums and its step grid)."""

import json

import pytest

from tastr.engine import drums
from tastr.engine.bench.drums import _synth_ctx, run_synth
from tastr.engine.bench.synth import make_drum_track

pytestmark = pytest.mark.audio


@pytest.fixture(autouse=True)
def built_in_lanes(monkeypatch):
    """These tests check the built-in detector; the optional drum model has its own (test_drum_model.py)."""
    monkeypatch.setenv("TASTR_DRUM_MODEL", "off")


def grid_for(patterns, bpm=120):
    y, _ = make_drum_track(patterns, bpm=bpm)
    ctx = _synth_ctx(y, bpm)
    return drums.drum_grid(ctx, drums.step_grid(ctx, 1 + len(y) // ctx.hop))


def hits(g):
    return g["pattern"].replace("o", "x") if g["detected"] else "." * 16


def test_hats_do_not_leak_into_snare():
    # The old band split read off-beat hats as snare hits.
    g = grid_for({"kick": "x...x...x...x...", "hat": "..x...x...x...x."})
    assert hits(g["kick"]) == "x...x...x...x..."
    assert hits(g["hat"]) == "..x...x...x...x."
    assert not g["snare"]["detected"] and not g["clap"]["detected"]


def test_snare_and_clap_told_apart():
    snare = grid_for({"kick": "x.......x.......", "snare": "....x.......x...", "hat": "x.x.x.x.x.x.x.x."})
    assert hits(snare["snare"]) == "....x.......x..."
    assert not snare["clap"]["detected"]
    clap = grid_for({"kick": "x.......x.......", "clap": "....x.......x...", "hat": "x.x.x.x.x.x.x.x."})
    assert hits(clap["clap"]) == "....x.......x..."


def test_open_and_closed_hats():
    g = grid_for({"kick": "x...x...x...x...", "hat": "x...x...x...x...", "open_hat": "..x...x...x...x."})
    assert hits(g["open_hat"]) == "..x...x...x...x."
    assert hits(g["hat"]) == "x...x...x...x..."


def test_toms_are_not_kicks():
    g = grid_for({"kick": "x.......x.......", "snare": "....x.......x...", "tom": "..........x...x."})
    assert hits(g["kick"]) == "x.......x......."
    assert g["tom"]["detected"] and hits(g["tom"])[10] == "x"


def test_lane_help_covers_every_lane():
    assert set(drums.LANE_HELP) == set(drums.LANES)


def test_synth_suite_floor():
    """Guards against regressions; the numbers in docs/bench.md are the current scores."""
    s = run_synth(bpms=(120,))["summary"]
    json.dumps(s)
    assert s["kick"]["f"] >= 0.9
    assert s["clap"]["f"] >= 0.8
    assert s["hat"]["f"] >= 0.7
    for lane in ("snare", "open_hat", "tom", "perc"):
        assert s[lane]["f"] >= 0.4, (lane, s[lane])


# --- onset suites: loaders, bundles and scoring (fake dataset folders) -----------------------------

from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

from tastr.engine.bench import drums as bench_drums  # noqa: E402
from tastr.engine.bench.synth import drum_sounds, write_wav  # noqa: E402


def test_score_track_excuses_cymbals_and_splits_toms():
    hop, sr = 512, 22050
    acts = {lane: np.zeros(400) for lane in drums.LANES}
    frame = lambda t: int(round(t * sr / hop))  # noqa: E731
    for t in (1.0, 2.0, 3.0):
        acts["kick"][frame(t)] = 1.0
    acts["tom"][frame(1.0)] = acts["tom"][frame(4.0)] = 1.0  # one tom on a kick, one alone
    acts["tom"][frame(6.0)] = 1.0  # a tom where none was played
    acts["hat"][frame(5.0)] = 1.0  # on a ride hit: not counted against the hat
    ref = {"kick": [1.0, 2.0, 3.0], "tom": [1.0, 4.0, 7.0], "cymbal": [5.0]}
    s = bench_drums.score_track(acts, ref, bench_drums.LANE_LABELS, bench_drums.EXCUSED, hop=hop, sr=sr)
    assert s["counts"]["kick"] == [3, 0, 0]
    assert s["counts"]["tom"] == [2, 1, 1]
    assert s["split"]["tom"] == {"alone": [1, 2], "with": [1, 1]}
    assert s["counts"]["hat"] == [0, 0, 0] and s["excused"] == 1
    assert s["fp_at"]["tom"] == {"none": 1}
    assert "clap" in s["unlabeled"]


def idmt_folder(root: Path) -> Path:
    (root / "audio").mkdir(parents=True)
    (root / "annotation_xml").mkdir()
    y, _ = make_drum_track({"kick": "x...x...x...x...", "snare": "....x.......x..."}, bpm=120, bars=2)
    write_wav(root / "audio" / "Loop01#MIX.wav", y)
    events = "".join(f"<event><onsetSec>{t}</onsetSec><instrument>{i}</instrument></event>"
                     for i, ts in (("KD", (0.0, 0.5)), ("SD", (0.5,)), ("HH", (0.25,))) for t in ts)
    (root / "annotation_xml" / "Loop01#MIX.xml").write_text(
        f"<instrumentRecording><globalParameter><audioFileName>Loop01#MIX.wav</audioFileName></globalParameter>"
        f"<transcription>{events}</transcription></instrumentRecording>")
    return root


def test_idmt_bundle_replaces_the_xml(tmp_path):
    root = idmt_folder(tmp_path / "idmt")
    before = bench_drums.idmt_items(root)
    assert before[0][2] == {"KD": [0.0, 0.5], "SD": [0.5], "HH": [0.25]}
    bench_drums.write_bundle("idmt", root)
    for f in (root / "annotation_xml").iterdir():
        f.unlink()
    assert bench_drums.idmt_items(root) == before


def mdb_folder(root: Path) -> Path:
    for sub in ("audio/full_mix", "audio/drum_only", "annotations/subclass"):
        (root / sub).mkdir(parents=True)
    y, _ = make_drum_track({"kick": "x.......x.......", "tom": "....x.......x..."}, bpm=120, bars=2)
    write_wav(root / "audio/full_mix/Song_MIX.wav", y)
    write_wav(root / "audio/drum_only/Song_Drum.wav", y)
    (root / "annotations/subclass/Song_subclass.txt").write_text(
        "0.000000 \t KD \n0.500000 \t HFT \n0.500000 \t RDC \n0.750000 \t SDG \n1.000000 \t OHH \n1.250000 \t TMB \n")
    return root


def test_mdb_items_map_subclasses_and_bundle(tmp_path):
    root = mdb_folder(tmp_path / "mdb")
    (item,) = bench_drums.mdb_items(root)
    assert item[1].name == "Song_MIX.wav"
    assert item[2] == {"kick": [0.0], "tom": [0.5], "cymbal": [0.5], "snare": [0.75], "open_hat": [1.0], "perc": [1.25]}
    assert bench_drums.mdb_items(root, drums_only=True)[0][1].name == "Song_Drum.wav"
    bench_drums.write_bundle("mdb", root)
    (root / "annotations/subclass/Song_subclass.txt").unlink()
    assert bench_drums.mdb_items(root) == [item]


def stemgmd_folder(root: Path, kits=("kit_a", "kit_b")) -> Path:
    """StemGMD_single_hits/<kit>/<class>-<i>.wav, from the synthetic kit at 44.1 kHz stereo."""
    import soundfile as sf

    sounds = drum_sounds(44100)
    like = {"kick": "kick", "snare": "snare", "hihat_closed": "hat", "hihat_open": "open_hat", "hi_tom": "tom",
            "mid_tom": "tom", "low_tom": "tom", "ride": "open_hat", "crash_left": "open_hat"}
    for kit in kits:
        (root / "StemGMD_single_hits" / kit).mkdir(parents=True)
        for cls, src in like.items():
            for i, gain in enumerate((0.5, 1.0)):
                sig = np.concatenate([np.zeros(400), gain * sounds[src]])  # leading silence, as in the release
                sf.write(str(root / "StemGMD_single_hits" / kit / f"{cls}-{i}.wav"), np.stack([sig, sig], 1), 44100)
    return root


def test_stemgmd_grooves_are_exact_and_bundle_matches(tmp_path):
    root = stemgmd_folder(tmp_path / "sg")
    direct = bench_drums.stemgmd_items(root, limit=6)
    assert len(direct) == 6 and len({i for i, _, _ in direct}) == 6
    assert {i.split("/")[1] for i, _, _ in direct[:5]} == set(bench_drums.GROOVES)  # a limit spreads over grooves
    by_id = {i: (y, ref) for i, y, ref in direct}
    y, ref = by_id["kit_a/rock-fill/96"]
    step = 60 / 96 / 4
    # Two passes of 3 main bars + a fill bar; the fill's toms on steps 8-15, alone.
    assert len(ref["tom"]) == 2 * 8 and ref["tom"][0] == round((3 * 16 + 8) * step, 4)
    assert len(ref["kick"]) == 2 * (3 * 3 + 1)
    assert "perc" in by_id[next(i for i in by_id if "perc-layer" in i)][1]
    bench_drums.write_bundle("stemgmd-mix", root)
    import shutil

    shutil.rmtree(root / "StemGMD_single_hits")
    again = bench_drums.stemgmd_items(root, limit=6)
    assert [i for i, _, _ in again] == [i for i, _, _ in direct]
    assert all(np.array_equal(a[1], b[1]) and a[2] == b[2] for a, b in zip(again, direct))


def test_onset_suites_score_fake_folders(tmp_path):
    report = bench_drums.run("mdb", mdb_folder(tmp_path / "mdb"))
    assert report["tracks"] == 1 and report["summary"]["kick"]["n"] == 1
    assert set(report["summary"]["tom"]) >= {"r_alone", "r_with", "lenient"}
    assert "rows" in report and json.dumps(report)
    assert bench_drums.run("idmt", idmt_folder(tmp_path / "idmt"))["summary"]["kick"]["n"] == 2
    assert "tom" in bench_drums.run("idmt", tmp_path / "idmt")["unlabeled_onsets_per_ref"]


def test_kit_split_holds_out_half_the_kits(tmp_path):
    names = [f"k{i}" for i in range(10)]
    train, test = bench_drums.kit_split(names, "train"), bench_drums.kit_split(names, "test")
    assert len(train) == len(test) == 5 and not set(train) & set(test)
    root = stemgmd_folder(tmp_path / "sg")  # kits kit_a (train) and kit_b (test)
    full = {i: y for i, y, _ in bench_drums.stemgmd_items(root)}
    held = bench_drums.stemgmd_items(root, kits="test")
    assert {i.split("/")[0] for i, _, _ in held} == {"kit_b"}
    assert all(np.array_equal(y, full[i]) for i, y, _ in held)  # same audio as in the full set


def test_bench_pool_spawns_its_workers(monkeypatch):
    """Forked workers hang once the first track has started PyTorch (the drum model) on Linux."""
    seen = {}

    class Pool:
        def __init__(self, jobs, mp_context=None):
            seen["method"] = mp_context.get_start_method() if mp_context else None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def map(self, fn, work):
            return [fn(w) for w in work]

    monkeypatch.setattr(bench_drums, "ProcessPoolExecutor", Pool)
    monkeypatch.setattr(bench_drums, "_work", lambda job: {"id": job[0], "seconds": 1.0, "counts": {}, "fp_at": {},
                                                           "split": {}, "excused": 0, "unlabeled": {}})
    bench_drums.run_onsets("x", [("a", None, {}), ("b", None, {})], {}, jobs=2)
    assert seen["method"] == "spawn"
