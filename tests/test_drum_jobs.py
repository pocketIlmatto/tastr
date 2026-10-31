"""On-demand drum model runs (tastr/drum_jobs.py): the API, the background job, keeping the model on
re-analysis, failing loudly when it can't run, the daily limit and the CLI.

The engine never runs here: `service._analyze` is replaced by a stub that records its options.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from tastr import cli, db, drum_jobs, service
from tastr.engine import adt
from tastr.web.server import create_app
from test_foundation import full_card


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TASTR_DRUM_MODEL", raising=False)
    monkeypatch.delenv("TASTR_ANALYSIS_BACKEND", raising=False)
    return tmp_path / "home"


@pytest.fixture
def track(db_path, tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"RIFF" + bytes(100))
    card = full_card()
    card.source.local_path = str(audio)
    conn = db.connect(db_path)
    tid = db.save_card(conn, card)
    conn.close()
    return tid


@pytest.fixture
def gate():
    ev = threading.Event()
    ev.set()
    return ev


@pytest.fixture
def engine(monkeypatch, gate):
    """The stub engine: options it was called with; it runs the model when asked unless `works` is False."""
    class Calls(list):
        state = {"works": True}

    calls = Calls()
    state = calls.state

    def fake_analyze(source, stages, options, on_stage=None):
        calls.append(dict(options or {}))
        assert gate.wait(5)
        card = full_card()
        card.source = source
        if (options or {}).get("drum_model"):
            if state["works"]:
                card.extras["drum_model"] = "adtof"
            else:
                card.extras["drum_model_error"] = "URLError: no network"
        return card

    monkeypatch.setattr(service, "_analyze", fake_analyze)
    monkeypatch.setattr(drum_jobs, "available", lambda: True)
    return calls


@pytest.fixture
def client(db_path, home):
    return TestClient(create_app(db_path))


def wait_for(client, tid, state, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = client.get(f"/api/tracks/{tid}/drum-model").json()
        if st["state"] == state:
            return st
        time.sleep(0.02)
    raise AssertionError(f"drum model run never reached {state}: {st}")


def test_runs_in_background_and_reanalyze_keeps_it(client, track, engine, gate, db_path):
    body = client.get(f"/api/tracks/{track}").json()
    assert body["drum_model_available"] is True and body["drum_model_state"] == "none"
    assert "drum_model" not in body["extras"]
    gate.clear()
    r = client.post(f"/api/tracks/{track}/drum-model")
    assert r.status_code == 202 and r.json()["state"] in ("queued", "running")
    wait_for(client, track, "running")
    assert client.post(f"/api/tracks/{track}/drum-model").status_code == 202  # already running: no second job
    gate.set()
    wait_for(client, track, "done")
    assert engine == [{"drum_model": True, "drum_model_required": True}]
    assert client.get(f"/api/tracks/{track}").json()["extras"]["drum_model"] == "adtof"
    # A plain re-analysis keeps using the model on this track, without failing where it can't run.
    assert client.post(f"/api/tracks/{track}/reanalyze").status_code == 200
    assert engine[-1] == {"drum_model": "adtof"}


def test_a_run_that_cant_use_the_model_fails_and_keeps_the_card(client, track, engine, db_path):
    engine.state["works"] = False
    client.post(f"/api/tracks/{track}/drum-model")
    st = wait_for(client, track, "error")
    assert "no network" in st["error"]
    conn = db.connect(db_path)
    assert "drum_model" not in db.load_card(conn, track).extras
    conn.close()


def test_everyday_analysis_doesnt_ask_for_the_model(client, track, engine):
    assert client.post(f"/api/tracks/{track}/reanalyze").status_code == 200
    assert engine == [{}]


def test_not_installed_or_no_file(client, track, db_path, monkeypatch):
    monkeypatch.setattr(drum_jobs, "available", lambda: False)
    r = client.post(f"/api/tracks/{track}/drum-model")
    assert r.status_code == 501 and "tastr[drums]" in r.json()["detail"]
    assert client.get(f"/api/tracks/{track}").json()["drum_model_available"] is False
    monkeypatch.setattr(drum_jobs, "available", lambda: True)
    conn = db.connect(db_path)
    gone = db.save_card(conn, full_card())  # its audio file isn't on disk
    conn.close()
    assert client.post(f"/api/tracks/{gone}/drum-model").status_code == 410
    assert client.post("/api/tracks/999/drum-model").status_code == 404


def test_available_follows_the_backend(home, monkeypatch):
    monkeypatch.setattr(adt.importlib.util, "find_spec", lambda name, *a: object())
    assert drum_jobs.available()
    monkeypatch.setenv("TASTR_ANALYSIS_BACKEND", "modal")  # a hosted server: runs on Modal, no PyTorch here
    monkeypatch.setattr(adt.importlib.util, "find_spec", lambda name, *a: object() if name == "modal" else None)
    assert drum_jobs.available()
    monkeypatch.setenv("TASTR_DRUM_MODEL", "off")
    assert not drum_jobs.available()


def test_remote_analysis_picks_the_drum_function(monkeypatch, tmp_path):
    from tastr.cloud import remote

    names = []
    monkeypatch.setattr(remote, "_call", lambda name, *a: names.append(name) or full_card().to_dict())
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    remote.analyze(audio)
    remote.analyze(audio, options={"drum_model": True})
    assert names == ["analyze", "analyze_drums"]


def test_unfinished_runs_resume(home):
    ran = []
    done = threading.Event()
    jobs = drum_jobs.Jobs(lambda tid: (ran.append(tid), done.set()))
    drum_jobs.jobs_root().mkdir(parents=True)
    (drum_jobs.jobs_root() / "7.job").touch()
    assert jobs.resume() == 1
    assert done.wait(5)
    end = time.monotonic() + 5
    while (drum_jobs.jobs_root() / "7.job").exists() and time.monotonic() < end:
        time.sleep(0.02)
    assert ran == [7] and not (drum_jobs.jobs_root() / "7.job").exists()


def test_hosted_daily_limit(db_path, home, tmp_path, monkeypatch):
    from test_hosted import GOOD, make_client

    audio = tmp_path / "x.wav"
    audio.write_bytes(b"RIFF")
    card = full_card()
    card.source.local_path = str(audio)
    conn = db.connect(db_path)
    tid = db.save_card(conn, card, "friend@example.com")
    conn.close()
    started = []
    monkeypatch.setattr(drum_jobs, "available", lambda: True)
    monkeypatch.setattr(drum_jobs.Jobs, "start", lambda self, track_id: started.append(track_id))
    c = make_client(db_path, daily_drums=1)
    assert c.post(f"/api/tracks/{tid}/drum-model", headers=GOOD).status_code == 202
    assert c.post(f"/api/tracks/{tid}/drum-model", headers=GOOD).status_code == 429 and started == [tid]


def test_cli_run(db_path, track, home, engine, capsys):
    assert cli.main(["--db", str(db_path), "drum-model", "run", str(track)]) == 0
    assert engine == [{"drum_model": True, "drum_model_required": True}]
    assert cli.main(["--db", str(db_path), "drum-model", "run", "999"]) == 1
    assert cli.main(["--db", str(db_path), "drum-model", "run"]) == 2


def test_ui_has_the_menu_item_and_bulk_action():
    from tastr.web.server import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text()
    for needle in ('"Detect drums with the model"', "/drum-model`", "drumModelOn", "pip install 'tastr[drums]'"):
        assert needle in js
