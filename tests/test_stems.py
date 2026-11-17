"""Tests for on-demand stem separation (tastr/stems.py): API states, files, delete cleanup, CORS and the CLI.

Demucs never runs here: `stems.separate` is replaced by a stub that writes small files.
"""

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tastr import cli, db, stems
from tastr.web.server import create_app
from test_foundation import full_card
from ui_src import app_js


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def client(db_path, home):
    return TestClient(create_app(db_path))


@pytest.fixture
def track(db_path, tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"RIFF" + bytes(100))
    card = full_card()
    card.source.local_path = str(audio)
    card.source.sha256 = "f00d" * 16
    conn = db.connect(db_path)
    tid = db.save_card(conn, card)
    conn.close()
    return tid


@pytest.fixture
def gate():
    """Holds the stub separator until set, so a test can see the running state."""
    ev = threading.Event()
    ev.set()
    return ev


@pytest.fixture
def stub(monkeypatch, gate):
    calls = []

    def fake_separate(audio_path, out_dir, progress=None):
        calls.append(Path(audio_path))
        if progress:
            progress(0.5)
        assert gate.wait(5)
        out_dir.mkdir(parents=True, exist_ok=True)
        for n in stems.STEM_NAMES:
            stems.stem_path(out_dir, n).write_bytes(b"fLaC" + n.encode() * 20)

    monkeypatch.setattr(stems, "separate", fake_separate)
    monkeypatch.setattr(stems, "available", lambda: True)
    return calls


def wait_for(client, tid, state, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = client.get(f"/api/tracks/{tid}/stems").json()
        if st["state"] == state:
            return st
        time.sleep(0.02)
    raise AssertionError(f"stems never reached {state}: {st}")


def test_not_installed_is_501_with_install_hint(client, track, monkeypatch):
    monkeypatch.setattr(stems, "available", lambda: False)
    r = client.post(f"/api/tracks/{track}/stems")
    assert r.status_code == 501 and "pip install 'tastr[stems]'" in r.json()["detail"]
    assert client.get(f"/api/tracks/{track}/stems").json() == {"state": "none", "available": False}
    body = client.get(f"/api/tracks/{track}").json()
    assert body["stems_available"] is False and body["stems_state"] == "none"


def test_no_file_or_no_track_is_404(client, db_path, stub):
    conn = db.connect(db_path)
    tid = db.save_card(conn, full_card())  # local_path /x.wav does not exist
    conn.close()
    assert client.post(f"/api/tracks/{tid}/stems").status_code == 404
    assert client.post("/api/tracks/999/stems").status_code == 404
    assert client.get("/api/tracks/999/stems").status_code == 404
    assert stub == []


def test_separation_runs_in_background_then_serves_stems(client, track, stub, gate, home):
    gate.clear()
    r = client.post(f"/api/tracks/{track}/stems")
    assert r.status_code == 202 and r.json()["state"] in ("queued", "running")
    st = wait_for(client, track, "running")
    assert st["available"] is True
    end = time.monotonic() + 5
    while client.get(f"/api/tracks/{track}/stems").json().get("progress") != 0.5 and time.monotonic() < end:
        time.sleep(0.02)
    assert client.get(f"/api/tracks/{track}/stems").json()["progress"] == 0.5
    assert client.get(f"/api/tracks/{track}").json()["stems_state"] == "running"
    assert client.post(f"/api/tracks/{track}/stems").status_code == 202  # already running: no second job
    assert client.get(f"/api/tracks/{track}/stems/vocals").status_code == 404
    gate.set()

    st = wait_for(client, track, "ready")
    assert st["stems"] == ["vocals", "bass", "other", "drums"]
    assert len(stub) == 1
    folder = home / "stems" / ("f00d" * 4)
    assert sorted(p.name for p in folder.iterdir()) == ["bass.flac", "drums.flac", "other.flac", "stems.json", "vocals.flac"]
    assert st["made_with"]["model"] == stems.MODEL and st["made_with"]["recorded"] and not st["made_with"]["outdated"]
    assert not folder.with_name(folder.name + ".partial").exists()
    assert client.get(f"/api/tracks/{track}").json()["stems_state"] == "ready"

    res = client.get(f"/api/tracks/{track}/stems/drums")
    assert res.status_code == 200 and res.headers["content-type"] == "audio/flac"
    assert res.content == b"fLaC" + b"drums" * 20
    part = client.get(f"/api/tracks/{track}/stems/drums", headers={"Range": "bytes=0-3"})
    assert part.status_code == 206 and part.content == b"fLaC"
    assert client.get(f"/api/tracks/{track}/stems/piano").status_code == 404
    # ready already: starting again doesn't separate again
    assert client.post(f"/api/tracks/{track}/stems").json()["state"] == "ready" and len(stub) == 1


def test_failed_separation_reports_error_and_can_retry(client, track, monkeypatch, stub, caplog):
    def broken(audio_path, out_dir, progress=None):
        out_dir.mkdir(parents=True)
        stems.stem_path(out_dir, "vocals").write_bytes(b"x")  # half done, then it fails
        raise MemoryError("out of memory")

    monkeypatch.setattr(stems, "separate", broken)
    with caplog.at_level("INFO", logger="tastr.stems"):
        client.post(f"/api/tracks/{track}/stems")
        st = wait_for(client, track, "error")
    assert "out of memory" in st["error"]
    failed = [r for r in caplog.records if r.getMessage().startswith("Stem separation failed for")]
    assert failed and failed[0].exc_info and "song.wav" in failed[0].getMessage()  # with the traceback, for the terminal
    assert client.get(f"/api/tracks/{track}/stems/vocals").status_code == 404  # nothing half-done is served
    assert client.get(f"/api/tracks/{track}").json()["stems_state"] == "error"


def test_delete_track_deletes_stems(client, track, stub, home):
    client.post(f"/api/tracks/{track}/stems")
    wait_for(client, track, "ready")
    folder = home / "stems" / ("f00d" * 4)
    assert folder.is_dir()
    assert client.delete(f"/api/tracks/{track}").status_code == 204
    assert not folder.exists()


def test_stems_allow_strudel_cc_and_no_other_origin(client, track, stub):
    client.post(f"/api/tracks/{track}/stems")
    wait_for(client, track, "ready")
    url = f"/api/tracks/{track}/stems/vocals"
    ok = client.get(url, headers={"Origin": "https://strudel.cc", "Range": "bytes=0-9"})
    assert ok.status_code == 206 and ok.headers["access-control-allow-origin"] == "https://strudel.cc"
    pre = client.options(url, headers={"Origin": "https://strudel.cc", "Access-Control-Request-Method": "GET",
                                       "Access-Control-Request-Private-Network": "true"})
    assert pre.status_code == 204 and pre.headers["access-control-allow-private-network"] == "true"
    for origin in ("https://evil.example", None):
        headers = {"Origin": origin} if origin else {}
        assert "access-control-allow-origin" not in client.get(url, headers=headers).headers
        assert client.options(url, headers=headers).status_code == 403
    # the status and start endpoints stay same-origin only
    assert "access-control-allow-origin" not in client.get(f"/api/tracks/{track}/stems", headers={"Origin": "https://strudel.cc"}).headers


def test_stems_folder_falls_back_to_track_id_without_a_hash(home):
    assert stems.stems_dir(None, 7) == home / "stems" / "track-7"
    assert stems.stems_dir("ab" * 32, 7) == home / "stems" / ("ab" * 8)


def test_cli_separates_in_the_foreground(db_path, track, stub, home, capsys):
    assert cli.main(["--db", str(db_path), "stems", str(track)]) == 0
    assert "Stems in" in capsys.readouterr().out
    assert stems.is_ready(home / "stems" / ("f00d" * 4)) and len(stub) == 1
    assert cli.main(["--db", str(db_path), "stems", str(track)]) == 0
    assert "already" in capsys.readouterr().out and len(stub) == 1
    assert cli.main(["--db", str(db_path), "stems", str(track), "--force"]) == 0 and len(stub) == 2
    assert cli.main(["--db", str(db_path), "stems", "999"]) == 1


def test_each_stem_set_records_the_model_that_made_it(home, monkeypatch):
    folder = home / "stems" / "abcd"

    def fake_separate(audio_path, out_dir, progress=None):
        out_dir.mkdir(parents=True, exist_ok=True)
        for n in stems.STEM_NAMES:
            stems.stem_path(out_dir, n).write_bytes(b"fLaC")
        return stems.settings("htdemucs_ft", 2, 0.5, "mps")

    monkeypatch.setattr(stems, "separate", fake_separate)
    stems.separate_track(Path("song.wav"), folder)
    saved = json.loads(stems.info_path(folder).read_text())
    assert saved["model"] == "htdemucs_ft" and saved["shifts"] == 2 and saved["overlap"] == 0.5
    assert saved["device"] == "mps" and saved["made_at"].endswith("Z")
    made = stems.made_with(folder)
    assert made["model"] == stems.MODEL and made["recorded"] and not made["outdated"]


def test_stems_without_a_record_count_as_old_htdemucs(client, track, home):
    folder = home / "stems" / ("f00d" * 4)  # stems from before the record existed
    folder.mkdir(parents=True)
    for n in stems.STEM_NAMES:
        stems.stem_path(folder, n).write_bytes(b"fLaC")
    st = client.get(f"/api/tracks/{track}/stems").json()
    assert st["state"] == "ready"
    assert st["made_with"] == {"model": "htdemucs", "shifts": 1, "overlap": 0.25, "recorded": False, "outdated": True,
                            "latest": stems.MODEL}


def old_stems(home):
    """Stems from before the record existed (htdemucs), as most sets made before it are."""
    folder = home / "stems" / ("f00d" * 4)
    folder.mkdir(parents=True)
    for n in stems.STEM_NAMES:
        stems.stem_path(folder, n).write_bytes(b"OLD " + n.encode())
    return folder


def wait_again_done(client, tid, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = client.get(f"/api/tracks/{tid}/stems").json()
        if not st.get("again") or st["again"]["state"] == "error":
            return st
        time.sleep(0.02)
    raise AssertionError(f"the second separation never ended: {st}")


def test_separating_again_keeps_the_old_stems_until_the_new_ones_are_ready(client, track, stub, gate, home):
    old_stems(home)
    assert client.post(f"/api/tracks/{track}/stems").json()["state"] == "ready" and stub == []  # no flag: nothing to do
    gate.clear()
    r = client.post(f"/api/tracks/{track}/stems?again=1")
    assert r.status_code == 202
    st = r.json()
    assert st["state"] == "ready" and st["again"]["state"] in ("queued", "running")
    end = time.monotonic() + 5
    while client.get(f"/api/tracks/{track}/stems").json()["again"]["state"] != "running" and time.monotonic() < end:
        time.sleep(0.02)
    st = client.get(f"/api/tracks/{track}/stems").json()
    assert st["state"] == "ready" and st["again"]["state"] == "running" and st["made_with"]["outdated"]
    assert client.get(f"/api/tracks/{track}").json()["stems_state"] == "ready"
    assert client.get(f"/api/tracks/{track}/stems/drums").content == b"OLD drums"  # still playable meanwhile
    assert client.get(f"/api/tracks/{track}/stems.zip").status_code == 200  # and downloadable
    assert client.post(f"/api/tracks/{track}/stems?again=1").status_code == 202 and len(stub) == 1  # no second job
    assert client.delete(f"/api/tracks/{track}/stems").status_code == 409  # not while they're being replaced
    files = client.get("/api/files").json()["tracks"][0]["stems"]
    assert files["state"] == "ready" and files["again"]["state"] == "running" and files["made_with"]["outdated"]
    assert files["bytes"]["drums"] == len(b"OLD drums")
    gate.set()

    st = wait_again_done(client, track)
    assert st["state"] == "ready" and "again" not in st
    assert st["made_with"]["model"] == stems.MODEL and not st["made_with"]["outdated"]
    assert client.get(f"/api/tracks/{track}/stems/drums").content == b"fLaC" + b"drums" * 20
    assert not client.get("/api/files").json()["tracks"][0]["stems"]["made_with"]["outdated"]


def test_a_failed_second_separation_leaves_the_old_stems(client, track, monkeypatch, stub, home):
    folder = old_stems(home)

    def broken(audio_path, out_dir, progress=None):
        out_dir.mkdir(parents=True)
        stems.stem_path(out_dir, "vocals").write_bytes(b"x")
        raise MemoryError("out of memory")

    monkeypatch.setattr(stems, "separate", broken)
    client.post(f"/api/tracks/{track}/stems?again=1")
    st = wait_again_done(client, track)
    assert st["state"] == "ready" and st["again"]["state"] == "error" and "out of memory" in st["again"]["error"]
    assert st["made_with"]["outdated"] and client.get(f"/api/tracks/{track}/stems/vocals").content == b"OLD vocals"
    assert not folder.with_name(folder.name + ".partial").exists()


def test_a_second_separation_resumes_after_a_restart(db_path, track, stub, home, tmp_path):
    folder = old_stems(home)
    markers = stems.Jobs(db_path).markers
    markers.put(folder.name, {"audio": str(tmp_path / "song.wav"), "again": True})  # a machine stopped mid-job
    with TestClient(create_app(db_path)) as c:
        st = wait_again_done(c, track)
    assert len(stub) == 1 and markers.items() == [] and not st["made_with"]["outdated"]


def test_an_unreadable_record_counts_as_old_htdemucs(home):
    folder = home / "stems" / "bad"
    folder.mkdir(parents=True)
    stems.info_path(folder).write_text("{not json")
    assert stems.made_with(folder)["model"] == "htdemucs" and stems.made_with(folder)["outdated"]


def test_separating_again_replaces_the_record(home, monkeypatch):
    folder = home / "stems" / "again"
    model = ["htdemucs"]

    def fake_separate(audio_path, out_dir, progress=None):
        out_dir.mkdir(parents=True, exist_ok=True)
        for n in stems.STEM_NAMES:
            stems.stem_path(out_dir, n).write_bytes(b"fLaC")
        return stems.settings(model[0])

    monkeypatch.setattr(stems, "separate", fake_separate)
    stems.separate_track(Path("song.wav"), folder)
    assert stems.made_with(folder)["outdated"]
    model[0] = stems.MODEL
    stems.separate_track(Path("song.wav"), folder)
    assert stems.made_with(folder)["model"] == stems.MODEL and not stems.made_with(folder)["outdated"]
    stems.remove(folder)
    assert not stems.info_path(folder).exists()


def test_modal_stems_keep_the_settings_modal_sent(home, monkeypatch):
    from tastr.cloud import remote

    monkeypatch.setenv("TASTR_STEMS_BACKEND", "modal")
    sent = {"model": "htdemucs_ft", "shifts": 1, "overlap": 0.25, "demucs": "4.1.0", "device": "cuda"}
    monkeypatch.setattr(remote, "stems", lambda path, put_urls=None: {**{n: b"fLaC" for n in stems.STEM_NAMES}, "settings": sent})
    folder = home / "stems" / "modal"
    stems.separate_track(Path("song.wav"), folder)
    assert json.loads(stems.info_path(folder).read_text()) == sent
    # an older deployed function sends no settings: the record says what this server's default is
    monkeypatch.setattr(remote, "stems", lambda path, put_urls=None: {n: b"fLaC" for n in stems.STEM_NAMES})
    stems.separate_track(Path("song.wav"), folder)
    assert json.loads(stems.info_path(folder).read_text())["model"] == stems.MODEL


def test_modal_job_returns_the_settings_with_the_stems(monkeypatch, tmp_path):
    from tastr.cloud import jobs

    def fake_separate(src, out, progress=None):
        out.mkdir(parents=True)
        for n in stems.STEM_NAMES:
            stems.stem_path(out, n).write_bytes(b"fLaC")
        return stems.settings(device="cuda")

    monkeypatch.setattr(stems, "separate", fake_separate)
    got = jobs.run_stems(b"RIFF", ".wav")
    assert set(got) == {*stems.STEM_NAMES, "settings"}
    assert got["settings"]["model"] == stems.MODEL and got["settings"]["device"] == "cuda"


def test_cli_without_demucs_prints_install_hint(db_path, track, home, monkeypatch, capsys):
    monkeypatch.setattr(stems, "available", lambda: False)
    assert cli.main(["--db", str(db_path), "stems", str(track)]) == 2
    assert "pip install 'tastr[stems]'" in capsys.readouterr().err


def test_ui_has_stems_menu_item_and_strudel_picker():

    js = app_js()
    for needle in ('"Separate stems"', '"Stems ready"', "/stems`", '"Full mix"', '"Stems"', "STRUDEL_STEMS", "pip install 'tastr[stems]'"):
        assert needle in js
    assert 'new Set(["vocals", "bass", "other"])' in js  # drums off by default


def test_ui_offers_to_separate_old_stems_again():
    """The Files line and filter, and the line with ↻ under old stems (Files and the Strudel stems picker)."""
    js = app_js()
    for needle in ("?again=1", "made before Tastr kept a record of the model", "stems from the older model",
                   "Separate ${n === 1 ? \"it\" : \"them\"} again", "Separating again", "stemModelNote(c, st",
                   "stemModelNote(f, st", 'flState.show !== "oldstems"', "Your current stems stay until the new ones are ready."):
        assert needle in js, needle
    html = (Path(stems.__file__).parent / "web" / "static" / "index.html").read_text()
    assert 'data-v="oldstems"' in html and 'id="fl-oldstems"' in html


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_stereo_decode_reads_m4a_through_ffmpeg(tmp_path):
    """Separation needs all channels; libsndfile can't open m4a, so it must fall back to ffmpeg like analysis does."""
    import subprocess

    from tastr.engine.audio import load_audio

    m4a = tmp_path / "tone.m4a"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=2", "-ac", "2", str(m4a)], check=True)
    y = load_audio(m4a, 44100, mono=False)
    assert y.shape[0] == 2 and abs(y.shape[1] - 2 * 44100) < 4096
    assert load_audio(m4a, 22050).ndim == 1


def _fake_hub_cache(root: Path, files: dict[str, str]) -> None:
    """Lay out `files` the way huggingface_hub caches HF_REPO at revision main."""
    repo = root / ("models--" + stems.HF_REPO.replace("/", "--"))
    (repo / "refs").mkdir(parents=True)
    (repo / "refs" / "main").write_text("abc123")
    snap = repo / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    for name, text in files.items():
        (snap / name).write_text(text)


@pytest.mark.parametrize("cached", [True, False])
def test_hub_offline_only_while_loading_a_cached_model(tmp_path, monkeypatch, cached):
    pytest.importorskip("yaml")
    constants = pytest.importorskip("huggingface_hub.constants")
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path))
    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", False)
    files = {f"{stems.MODEL}.yaml": "models: [a1, b2]\n", "a1.safetensors": ""}
    if cached:
        files["b2.safetensors"] = ""
    _fake_hub_cache(tmp_path, files)

    with stems._hub_offline_if_cached():
        assert constants.HF_HUB_OFFLINE is cached
    assert constants.HF_HUB_OFFLINE is False


def test_unfinished_jobs_resume_after_a_restart(db_path, track, stub, gate, home, tmp_path):
    gate.clear()
    first = TestClient(create_app(db_path))
    first.post(f"/api/tracks/{track}/stems")
    wait_for(first, track, "running")
    folder = home / "stems" / ("f00d" * 4)
    markers = stems.Jobs(db_path).markers  # kept in the database, so a server on a fresh disk still finds them
    assert markers.get(folder.name) == {"audio": str(tmp_path / "song.wav")}
    gate.set()
    wait_for(first, track, "ready")
    assert markers.items() == []

    # What a machine stopped mid-job leaves behind: a marker and no stems.
    stems.remove(folder)
    markers.put(folder.name, {"audio": str(tmp_path / "song.wav")})
    with TestClient(create_app(db_path)) as again:  # startup picks the job up again
        wait_for(again, track, "ready")
    assert len(stub) == 2 and markers.items() == []


def test_marker_files_from_an_older_server_are_read_once(home, db_path, tmp_path, stub):
    root = home / "stems"
    root.mkdir(parents=True)
    (root / "gone.job").write_text(str(home / "missing.wav"))  # its audio is gone: dropped
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"RIFF")
    (root / "kept.job").write_text(str(audio))
    jobs = stems.Jobs(db_path)
    assert jobs.resume() == 1 and not (root / "gone.job").exists() and not (root / "kept.job").exists()
    for _ in range(250):  # the separation runs in the background; its marker goes when it ends
        if not jobs.markers.items():
            break
        time.sleep(0.02)
    assert stub == [audio] and jobs.markers.items() == []


def test_queued_jobs_say_how_many_are_ahead(home, tmp_path, stub, gate):
    """For the page's "N ahead" and time left: a queued job's position, a running job's elapsed seconds."""
    gate.clear()
    jobs = stems.Jobs()
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    for p in (a, b):
        p.write_bytes(b"RIFF")
    fa, fb = stems.stems_root() / "a", stems.stems_root() / "b"
    jobs.start(a, fa)
    second = jobs.start(b, fb)
    assert second["state"] == "queued" and second["position"] == 1
    for _ in range(100):
        if jobs.status(fa)["state"] == "running":
            break
        time.sleep(0.02)
    first = jobs.status(fa)
    assert first["state"] == "running" and first["elapsed"] >= 0 and "_seq" not in first
    gate.set()
