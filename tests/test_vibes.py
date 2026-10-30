import io
import json
import random
import subprocess
import tarfile
import zipfile

import pytest

from tastr.cli import main
from tastr.cloud import fetch, jobs
from tastr.engine.bench import vibes

CLEAN_HEAD = "TRACK_ID\tARTIST_ID\tALBUM_ID\tPATH\tDURATION\tANNOTATIONS\n"


def mtg_tsvs(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "annotations-clean.tsv").write_text(
        CLEAN_HEAD
        + "track_0000001\ta\tb\t00/1.mp3\t200.0\tdanceability---danceable,danceable,danceable\tmood_sad---not_sad,not_sad,not_sad\n"
        + "track_0000002\ta\tb\t00/2.mp3\t20.0\tdanceability---not_danceable,not_danceable,not_danceable\tgenre_dortmund---rock,rock,rock\n"
        + "track_0000003\ta\tb\t01/3.mp3\t90.0\tgenre_dortmund---rock,rock,rock\n")
    (root / "moodtheme.tsv").write_text(CLEAN_HEAD.replace("ANNOTATIONS", "TAGS")
                                        + "track_0000002\ta\tb\t00/2.mp3\t20.0\tmood/theme---dark\tmood/theme---film\n")
    return root


def test_mtg_labels_from_both_files(tmp_path):
    labels = vibes.mtg_labels(mtg_tsvs(tmp_path))
    assert set(labels) == {"track_0000001", "track_0000002"}  # track 3 has no label we use
    assert labels["track_0000001"]["labels"] == {"danceable": 1, "sad": 0}
    two = labels["track_0000002"]["labels"]
    assert two["danceable"] == 0 and two["mood:dark"] == 1 and two["mood:energetic"] == 0
    assert labels["track_0000002"]["duration"] == 20.0


def test_deam_loader(tmp_path):
    ann = tmp_path / "DEAM_Annotations" / "annotations averaged per song" / "song_level"
    ann.mkdir(parents=True)
    (ann / "static_annotations_averaged_songs_1_2000.csv").write_text(
        "song_id, valence_mean, valence_std, arousal_mean, arousal_std\n2, 3.1, 0.9, 6.5, 1.0\n3, 5, 1, 2, 1\n")
    (tmp_path / "MEMD_audio").mkdir()
    (tmp_path / "MEMD_audio" / "2.mp3").write_bytes(b"x")
    items = vibes.deam(tmp_path)
    assert [it.id for it in items] == ["deam-2"]  # song 3 has no audio
    assert items[0].meta["labels"] == {"arousal": 6.5, "valence": 3.1}


def fake_rows(n=300, seed=0):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        pulse, perc = rng.random(), rng.random() * 0.5
        loud, onset = rng.uniform(-28, -8), rng.uniform(0, 6)
        rows.append({"id": f"t{i}", "errors": {}, "bpm": rng.uniform(80, 170), "loudness_db": loud, "energy": 0.5,
                     "labels": {"danceable": int(perc / 0.5 + rng.gauss(0, 0.2) > 0.5),
                                "arousal": (loud + 30) / 22 + rng.gauss(0, 0.1)},
                     "features": {"pulse_clarity": pulse, "percussiveness": perc, "onset_rate": onset,
                                  "brightness": rng.random(), "danceability": 0.55 * pulse},
                     "tags": {"danceable": 0.3} if perc > 0.25 else {}})
    return rows


def test_auc_and_simplex():
    assert vibes.auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == 1.0
    assert vibes.auc([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]) == 0.0
    assert vibes.auc([0.5, 0.5], [0, 1]) == 0.5
    assert vibes.auc([0.5], [1]) is None
    ws = list(vibes.simplex(3, 0.25))
    assert len(ws) == 15 and all(abs(sum(w) - 1) < 1e-9 and min(w) >= 0 for w in ws)


def test_score_reports_agreement_and_shown_precision():
    report = vibes.score(fake_rows())
    by = {(c["measure"], c["label"]): c for c in report["checks"]}
    tag = by[("tag:danceable", "danceable")]
    assert tag["auc"] > 0.8 and tag["precision"] > tag["base_rate"]
    assert by[("danceability", "danceable")]["auc"] < 0.65  # pulse clarity carries no signal in these rows
    assert "AUC" in vibes.format_report(report)


def test_fit_finds_the_signal_and_holds_out_tracks():
    results = {(r["measure"], r["label"]): r for r in vibes.fit(fake_rows())}
    dance = results[("danceability", "danceable")]
    assert dance["fitted"]["weights"][2] >= 0.7  # percussiveness made the labels
    assert dance["fitted"]["test"] > dance["baseline"]["test"]
    assert dance["n_train"] + dance["n_test"] == 300 and 150 < dance["n_train"] < 250
    energy = results[("energy", "arousal")]
    assert energy["fitted"]["weights"][0] >= 0.7  # loudness made arousal
    assert "held out" in vibes.format_fit(list(results.values()))
    assert vibes.fit(fake_rows(10)) == [] and "not enough" in vibes.format_fit([])


def test_vibe_fit_cli(tmp_path, capsys):
    f = tmp_path / "rows.json"
    f.write_text(json.dumps({"rows": fake_rows()}))
    out = tmp_path / "fit.json"
    assert main(["vibe-fit", str(f), "--out", str(out)]) == 0
    assert "danceability vs danceable" in capsys.readouterr().out
    assert json.loads(out.read_text())[0]["measure"] == "danceability"


def mp3(path, seconds):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=220:duration={seconds}", "-c:a", "libmp3lame", "-b:a", "64k", str(path)], check=True)


@pytest.mark.audio
def test_fetch_mtg_keeps_only_labeled_tracks_clipped(tmp_path):
    import hashlib

    from tastr.engine.audio import load_audio

    repo = tmp_path / "repo"
    labels = mtg_tsvs(tmp_path / "labels")
    for name, src in fetch.MTG_LABEL_FILES.items():
        (repo / src).parent.mkdir(parents=True, exist_ok=True)
        (repo / src).write_bytes((labels / name).read_bytes())
    tar_path = tmp_path / "audio-00.tar"
    mp3(tmp_path / "src" / "1.mp3", 200)
    mp3(tmp_path / "src" / "9.mp3", 5)
    with tarfile.open(tar_path, "w") as tar:
        tar.add(tmp_path / "src" / "1.mp3", "00/1.low.mp3")
        tar.add(tmp_path / "src" / "9.mp3", "00/9.low.mp3")  # unlabeled: skipped
    sums = repo / fetch.MTG_TAR_SHA256
    sums.parent.mkdir(parents=True, exist_ok=True)
    sums.write_text(f"{hashlib.sha256(tar_path.read_bytes()).hexdigest()} audio-00.tar\n")
    dest = tmp_path / "mtg"
    result = fetch.fetch_mtg(dest, [0], audio_url=f"file://{tmp_path}/audio-{{:02d}}.tar", repo=f"file://{repo}",
                             progress=lambda *_: None)
    assert result["kept"] == 1 and result["checksum_mismatch"] == []
    assert sorted(p.name for p in (dest / "audio").rglob("*.mp3")) == ["1.mp3"]
    assert abs(load_audio(str(dest / "audio/00/1.mp3"), 22050).size / 22050 - fetch.CLIP_SECONDS) < 1.5
    again = fetch.fetch_mtg(dest, [0], audio_url=f"file://{tmp_path}/audio-{{:02d}}.tar", repo=f"file://{repo}",
                            progress=lambda *_: None)
    assert again["kept"] == 0 and again["already_there"] == 1


def test_fetch_deam_unpacks_zips(tmp_path):
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("MEMD_audio/2.mp3", b"x")
        zf.writestr("__MACOSX/MEMD_audio/._2.mp3", b"junk")
    result = fetch.fetch_deam(tmp_path / "deam", urls=[f"file://{z}"], progress=lambda *_: None)
    assert result["files"] == 1 and (tmp_path / "deam/MEMD_audio/2.mp3").read_bytes() == b"x"


@pytest.mark.audio
def test_vibe_bench_end_to_end_on_a_folder(tmp_path, synth_wav, capsys):
    root = mtg_tsvs(tmp_path / "mtg")
    for p in ("00/1.mp3", "00/2.mp3"):
        (root / "audio" / p).parent.mkdir(parents=True, exist_ok=True)
        (root / "audio" / p).write_bytes(synth_wav[0].read_bytes())  # a wav under an mp3 name decodes fine
    jobs.write_index("mtg-moods", root)
    assert len(jobs.load_items("mtg-moods", root)) == 2
    out = tmp_path / "rows.json"
    assert main(["vibe-bench", "mtg-moods", str(root), "--out", str(out)]) == 0
    assert "danceability       vs danceable" in capsys.readouterr().out
    rows = json.loads(out.read_text())["rows"]
    assert {r["id"] for r in rows} == {"mtg-track_0000001", "mtg-track_0000002"}
    assert all(r["bpm"] and "pulse_clarity" in r["features"] and r["tags"] for r in rows)


def test_vibe_bench_and_fetch_remote(monkeypatch, capsys):
    from test_cloud import fake_modal

    calls = fake_modal(monkeypatch, {"report": {"tracks": 1, "failed": 0, "checks": []}, "rows": [], "timing": {}})
    assert main(["vibe-bench", "deam", "deam", "--limit", "5", "--remote"]) == 0
    assert calls[-1] == ("vibe_bench", ("deam", "deam", 5), {})
    calls = fake_modal(monkeypatch, {"dataset": "mtg-moods", "kept": 3, "checksum_mismatch": []})
    assert main(["fetch", "mtg-moods", "mtg-moods", "--archives", "0-2,7", "--remote"]) == 0
    assert calls[-1] == ("fetch", ("mtg-moods", "mtg-moods", [0, 1, 2, 7]), {})
