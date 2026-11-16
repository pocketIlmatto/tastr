"""Cards from an older engine: the track list flags them, the card names its stages and the current ones."""

import pytest
from fastapi.testclient import TestClient

from tastr import db
from tastr.engine import rust
from tastr.web.server import create_app
from test_foundation import full_card
from ui_src import app_js


@pytest.fixture
def client(db_path, tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    return TestClient(create_app(db_path))


def _save(db_path, stages, local_path, sha):
    card = full_card()
    card.stages_run = stages
    card.source.local_path, card.source.sha256, card.source.uri = local_path, sha, f"/{sha}.wav"
    conn = db.connect(db_path)
    tid = db.save_card(conn, card)
    conn.close()
    return tid


def test_stale_stages_compares_versions_as_numbers():
    assert rust.stale_stages(dict(rust.STAGES)) == []
    assert rust.stale_stages({**rust.STAGES, "core": "0.5.0"}) == ["core"]
    assert rust.stale_stages({**rust.STAGES, "core": "0.10.0"}) == []  # newer, not older: 10 > 6
    assert rust.stale_stages({**rust.STAGES, "core": rust.STAGES["core"].rsplit(".", 1)[0]}) == []  # "0.6" is 0.6.0
    assert rust.stale_stages({k: v for k, v in rust.STAGES.items() if k != "views"}) == ["views"]  # never ran
    assert rust.stale_stages(None) == list(rust.STAGES)


def test_track_list_flags_older_engine_and_deleted_audio(client, db_path, tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    cur = _save(db_path, dict(rust.STAGES), str(audio), "c1")
    old = _save(db_path, {**rust.STAGES, "core": "0.5.0"}, str(audio), "o1")
    gone = _save(db_path, {**rust.STAGES, "core": "0.5.0"}, str(tmp_path / "missing.wav"), "o2")
    rows = {r["id"]: r for r in client.get("/api/tracks").json()}
    assert (rows[cur]["stale"], rows[cur]["audio_gone"]) == (False, False)
    assert (rows[old]["stale"], rows[old]["audio_gone"]) == (True, False)
    assert (rows[gone]["stale"], rows[gone]["audio_gone"]) == (True, True)
    assert not {"stages_json", "local_path"} & set(rows[old])  # still private


def test_card_has_its_stages_and_the_current_ones(client, db_path, tmp_path):
    old = _save(db_path, {**rust.STAGES, "core": "0.5.0"}, str(tmp_path / "missing.wav"), "o1")
    body = client.get(f"/api/tracks/{old}").json()
    assert body["stages_run"]["core"] == "0.5.0" and body["engine_latest"] == rust.STAGES
    assert body["engine_news"]["core"][rust.STAGES["core"]]
    assert body["has_audio"] is False
    assert body["metadata"]["Analysis"]["Stages"].endswith(f"(current: core {rust.STAGES['core']}, "
                                                           + ", ".join(f"{k} {v}" for k, v in list(rust.STAGES.items())[1:]) + ")")
    cur = _save(db_path, dict(rust.STAGES), str(tmp_path / "missing.wav"), "c1")
    assert "current:" not in client.get(f"/api/tracks/{cur}").json()["metadata"]["Analysis"]["Stages"]


def test_reanalyze_without_audio_leaves_the_stages(client, db_path, tmp_path):
    old = _save(db_path, {**rust.STAGES, "core": "0.5.0"}, str(tmp_path / "missing.wav"), "o1")
    assert client.post(f"/api/tracks/{old}/reanalyze").status_code == 410
    assert client.get(f"/api/tracks/{old}").json()["stages_run"]["core"] == "0.5.0"


def test_ui_flags_older_engine_cards():
    js = app_js()
    for needle in ("engineLine", "Re-analyze with the latest engine", "Can't re-analyze: its audio was deleted",
                   "staleLine", "from an older engine", "lib-eng", "eng-mk", "f.eng"):
        assert needle in js, needle
