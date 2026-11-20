"""A moment from a link (tastr/moments.py): the whole source fetched once, the slice cut and broken down."""

import json
import shutil
import time

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from tastr import cli, db, deposits, moments, stems
from tastr.web.server import create_app

URL = "https://www.youtube.com/watch?v=abc123"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def source_wav(tmp_path):
    rate = 22050
    t = np.arange(rate * 90) / rate  # 90 s: the second half an octave up, so the slice can be told apart
    y = np.where(t < 45, np.sin(2 * np.pi * 220 * t), np.sin(2 * np.pi * 440 * t)) * 0.3
    path = tmp_path / "set.wav"
    sf.write(path, np.stack([y, y], axis=1), rate)
    return path


@pytest.fixture
def fetcher(source_wav):
    calls = []

    def fetch(url, folder, known):
        info = {"id": "abc123", "extractor_key": "Youtube", "title": "Live set", "duration": 90.0,
                "webpage_url": URL, "uploader": "A DJ"}
        if known(info):
            return None, info
        calls.append(url)
        dest = folder / "source.wav"
        shutil.copy(source_wav, dest)
        return dest, info

    fetch.calls = calls
    return fetch


def fake_card(path):
    return {"duration": sf.info(str(path)).duration,
            "core": {"bpm": 124.0, "bpm_confidence": 0.8, "energy": 0.6, "beats_per_bar": 4,
                     "key": {"tonic": 9, "mode": "minor", "camelot": "8A", "confidence": 0.4}},
            "sections": [{"start": 0.0, "end": 10.0, "label": "drop", "letter": "A", "energy": 0.7}],
            "tags": [{"tag": "dark", "score": 0.9}]}


@pytest.fixture
def fake_stems(monkeypatch):
    def separate(audio_path, out_dir, progress=None):
        out_dir.mkdir(parents=True, exist_ok=True)
        for n in stems.STEM_NAMES:
            stems.stem_path(out_dir, n).write_bytes(b"fLaC" + n.encode())

    monkeypatch.setattr(stems, "separate", separate)
    monkeypatch.setattr(stems, "available", lambda: True)


def test_moment_from_a_link_keeps_the_source_and_breaks_down_the_slice(db_path, home, fetcher, fake_stems):
    conn = db.connect(db_path)
    m = moments.add(conn, None, URL, 40, 50, note="the key change")
    assert m["status"] == "needs-source" and m["held_by"] == []
    seen = []
    got = moments.run(conn, None, m["id"], fetch=fetcher, analyze=fake_card, on_stage=seen.append)
    assert seen == ["fetch", "slice", "analyze", "stems"]
    assert got["status"] == "ready" and got["note"] == "the key change" and got["title"] == "Live set 0:40-0:50"
    ex = got["extracted"]
    assert ex["analysis"]["bpm"] == 124.0 and ex["analysis"]["key"] == "A minor" and ex["analysis"]["offset_s"] == 40
    assert ex["stems"]["names"] == list(stems.STEM_NAMES) and "error" not in ex and "stage" not in ex
    # the slice is the right ten seconds of the source: low tone, then high
    y, rate = sf.read(home / ex["slice"]["path"])
    assert abs(len(y) / rate - 10) < 0.05
    def pitch(part):
        return np.argmax(np.abs(np.fft.rfft(part))) * rate / len(part)

    assert abs(pitch(y[: 4 * rate, 0]) - 220) < 5 and abs(pitch(y[6 * rate:, 0]) - 440) < 5
    # the whole source is a deposit of its own, holding the moment at its times
    src = deposits.get(conn, None, ex["source_deposit"])
    assert src["size"] == "track" and src["track_id"] is None and src["status"] == "ready"
    assert src["context"]["source_key"] == "youtube:abc123" and src["context"]["artist"] == "A DJ"
    assert (home / src["extracted"]["audio"]["path"]).is_file() and src["extracted"]["audio"]["duration"] == 90.0
    assert [(h["id"], h["start_s"], h["end_s"]) for h in src["holds"]] == [(m["id"], 40, 50)]


def test_a_second_moment_reuses_the_source(db_path, home, fetcher, fake_stems):
    conn = db.connect(db_path)
    first = moments.run(conn, None, moments.add(conn, None, URL, 10, 20)["id"], fetch=fetcher, analyze=fake_card)
    second = moments.run(conn, None, moments.add(conn, None, URL, 60, 70)["id"], fetch=fetcher, analyze=fake_card)
    other_link = "https://youtu.be/abc123?t=5"  # the same video: read, not downloaded
    third = moments.run(conn, None, moments.add(conn, None, other_link, 1, 2)["id"], fetch=fetcher, analyze=fake_card)
    assert fetcher.calls == [URL]
    assert first["extracted"]["source_deposit"] == second["extracted"]["source_deposit"] == \
        third["extracted"]["source_deposit"]
    assert deposits.find(conn, None, size="track")["total"] == 1


def test_a_long_source_is_a_collection_and_stems_can_wait(db_path, home, fetcher, monkeypatch):
    monkeypatch.setattr(moments, "LONG_SOURCE_S", 60)
    monkeypatch.setattr(stems, "available", lambda: False)
    conn = db.connect(db_path)
    got = moments.run(conn, None, moments.add(conn, None, URL, 10, 20)["id"], fetch=fetcher, analyze=fake_card)
    assert deposits.get(conn, None, got["extracted"]["source_deposit"])["size"] == "collection"
    assert got["status"] == "ready" and "skipped" in got["extracted"]["stems"]


def test_a_moment_past_the_end_fails_and_says_why(db_path, home, fetcher):
    conn = db.connect(db_path)
    m = moments.add(conn, None, URL, 80, 120)
    with pytest.raises(moments.MomentError, match="after the source's end"):
        moments.run(conn, None, m["id"], fetch=fetcher, analyze=fake_card, separate=False)
    got = deposits.get(conn, None, m["id"])
    assert got["status"] == "failed" and "1:30" in got["extracted"]["error"]


def test_a_failed_step_keeps_what_was_done(db_path, home, fetcher):
    conn = db.connect(db_path)
    m = moments.add(conn, None, URL, 10, 20)

    def broken(path):
        raise RuntimeError("engine fell over")

    with pytest.raises(moments.MomentError, match="engine fell over"):
        moments.run(conn, None, m["id"], fetch=fetcher, analyze=broken, separate=False)
    assert moments.slice_path(m["id"]).is_file()
    got = moments.run(conn, None, m["id"], fetch=fetcher, analyze=fake_card, separate=False)
    assert got["status"] == "ready" and fetcher.calls == [URL]


def test_refuses_what_it_cannot_cut(db_path, home):
    conn = db.connect(db_path)
    with pytest.raises(deposits.DepositError):
        moments.add(conn, None, "/etc/passwd", 1, 2)
    with pytest.raises(deposits.DepositError):
        moments.add(conn, None, URL, 0, moments.MAX_MOMENT_S + 1)


def test_deleting_a_moment_removes_its_files(db_path, home, fetcher, fake_stems):
    conn = db.connect(db_path)
    got = moments.run(conn, None, moments.add(conn, None, URL, 10, 20)["id"], fetch=fetcher, analyze=fake_card)
    folder = deposits.files_dir(got["id"])
    assert (folder / "stems").is_dir()
    assert deposits.delete(conn, None, got["id"])
    assert not folder.exists()
    assert (home / deposits.get(conn, None, got["extracted"]["source_deposit"])["extracted"]["audio"]["path"]).is_file()


def test_api_moment_from_a_link(db_path, home, fetcher, fake_stems, monkeypatch):
    monkeypatch.setattr(moments, "fetch_link", fetcher)
    monkeypatch.setattr(moments, "analyze_slice", fake_card)
    client = TestClient(create_app(db_path))
    r = client.post("/api/deposits/moments", json={"source_uri": URL, "start_s": 10, "end_s": 20,
                                                     "note": "the handover"})
    assert r.status_code == 202, r.text
    mid = r.json()["id"]
    end = time.monotonic() + 5
    while (got := client.get(f"/api/deposits/{mid}").json())["status"] not in ("ready", "failed"):
        assert time.monotonic() < end, got
        time.sleep(0.02)
    assert got["status"] == "ready", got["extracted"]
    assert got["held_by"][0]["id"] == got["extracted"]["source_deposit"]
    audio = client.get(f"/api/deposits/{mid}/audio")
    assert audio.status_code == 200 and audio.content[:4] == b"fLaC"
    assert client.get(f"/api/deposits/{mid}/stems/vocals").content == b"fLaCvocals"
    assert client.get(f"/api/deposits/{mid}/card").json()["core"]["bpm"] == 124.0
    assert client.get(f"/api/deposits/{got['extracted']['source_deposit']}/audio").status_code == 200
    assert client.get(f"/api/deposits/{mid}/stems/nope").status_code == 404
    assert client.post("/api/deposits/moments", json={"source_uri": "file:///x", "start_s": 1, "end_s": 2}).status_code == 422
    assert client.post(f"/api/deposits/{got['extracted']['source_deposit']}/fetch").status_code == 422


def test_cli_add_moment_does_the_whole_thing(db_path, home, fetcher, fake_stems, monkeypatch, capsys):
    monkeypatch.setattr(moments, "fetch_link", fetcher)
    monkeypatch.setattr(moments, "analyze_slice", fake_card)
    assert cli.main(["--db", str(db_path), "deposit", "add", "moment", "--source", URL, "--start", "0:10",
                     "--end", "0:20", "--note", "the drop"]) == 0
    out = capsys.readouterr()
    got = json.loads(out.out)
    assert got["status"] == "ready" and got["start_s"] == 10 and "fetch..." in out.err
    assert cli.main(["--db", str(db_path), "deposit", "add", "moment", "--source", URL, "--start", "30",
                     "--end", "40", "--no-fetch"]) == 0
    waiting = json.loads(capsys.readouterr().out)
    assert waiting["status"] == "needs-source"
    assert cli.main(["--db", str(db_path), "deposit", "fetch", str(waiting["id"]), "--no-stems"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"


@pytest.mark.rust
def test_the_engine_analyzes_a_slice(synth_wav, tmp_path):
    path, truth = synth_wav
    cut = tmp_path / "slice.flac"
    moments.slice_audio(path, cut, 2, 14)
    card = moments.analyze_slice(cut)
    assert abs(card["duration"] - 12) < 0.1 and card["core"]["bpm"]


def test_cli_keeps_the_collection_asked_for(db_path, home, fetcher, monkeypatch, capsys):
    monkeypatch.setattr(moments, "fetch_link", fetcher)
    monkeypatch.setattr(moments, "analyze_slice", fake_card)
    conn = db.connect(db_path)
    crate = deposits.add(conn, None, "collection", title="Transitions")
    assert cli.main(["--db", str(db_path), "deposit", "add", "moment", "--source", URL, "--start", "10",
                     "--end", "20", "--in", str(crate["id"]), "--no-stems"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert {h["id"] for h in got["held_by"]} == {crate["id"], got["extracted"]["source_deposit"]}


def test_the_card_is_written_when_only_the_bucket_has_the_slice(db_path, home, fetcher):
    conn = db.connect(db_path)
    m = moments.add(conn, None, URL, 10, 20)

    def analyze_then_lose_the_folder(path):
        card = fake_card(path)
        shutil.rmtree(deposits.files_dir(m["id"]))  # as an evicted cache would, with Modal reading the bucket's copy
        return card

    got = moments.run(conn, None, m["id"], fetch=fetcher, analyze=analyze_then_lose_the_folder, separate=False)
    assert got["status"] == "ready" and moments.card_path(m["id"]).is_file()
