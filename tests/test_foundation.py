"""Tests for the skeleton: card round-trip, pipeline mechanics, storage, CLI."""

import json

import numpy as np
import pytest

from tastr import db
from tastr.card import Core, Key, Section, Series, Source, Suggestion, Tag, TrackCard
from tastr.cli import main
from tastr.ingest import IngestError, resolve_inputs
from tastr.pipeline import AnalysisContext, register_stage, resolve_order, run_pipeline, unregister_stage


def full_card() -> TrackCard:
    return TrackCard(
        source=Source(kind="file", uri="/x.wav", local_path="/x.wav", title="x", sha256="abc"),
        duration=10.0,
        sample_rate=22050,
        core=Core(bpm=124.0, key=Key(tonic=9, mode="minor", scale="Dorian", camelot="8A"), energy=0.7),
        sections=[Section(start=0, end=5, label="intro", letter="A"), Section(start=5, end=10, label="drop", letter="B")],
        tags=[Tag("dark", 0.8)],
        suggestions=[Suggestion("drums", "Four on the floor", "...", {"pattern": {"kick": "x...x...x...x..."}})],
        series={"energy": Series(times=[0, 5], values=[0.2, 0.9])},
        features={"brightness": 0.3},
        extras={"core.debug": [1, 2]},
    )


def test_card_roundtrip():
    card = full_card()
    again = TrackCard.from_dict(json.loads(card.to_json()))
    assert again == card
    assert again.core.key.name == "A minor"


def test_stage_order_and_requirements():
    names = [s.name for s in resolve_order()]
    assert names == ["core", "sections", "tags", "suggestions"]
    assert [s.name for s in resolve_order(["tags"])] == ["core", "sections", "tags"]
    with pytest.raises(KeyError):
        resolve_order(["nope"])


def test_failing_stage_is_recorded_and_dependents_skipped():
    class Boom:
        name, version, requires = "boom", "1", ("core",)

        def run(self, ctx):
            raise RuntimeError("kaboom")

    class AfterBoom:
        name, version, requires = "after_boom", "1", ("boom",)

        def run(self, ctx):
            raise AssertionError("should not run")

    register_stage(Boom(), replace=True)
    register_stage(AfterBoom(), replace=True)
    try:
        card = run_pipeline(np.zeros(22050 * 5, dtype=np.float32), 22050, TrackCard(Source("file", "/z")),
                            stages=["after_boom"])
    finally:
        unregister_stage("boom")
        unregister_stage("after_boom")
    assert "core" in card.stages_run
    assert "kaboom" in card.errors["boom"]
    assert card.errors["after_boom"].startswith("skipped")


def test_context_cache_computes_once():
    calls = []
    ctx = AnalysisContext(y=np.zeros(10), sr=22050, card=TrackCard(Source("file", "/z")))
    for _ in range(3):
        ctx.get("thing", lambda c: calls.append(1) or 42)
    assert calls == [1]


def test_db_save_load_replace(db_path):
    conn = db.connect(db_path)
    card = full_card()
    tid = db.save_card(conn, card)
    assert db.load_card(conn, tid) == card
    row = conn.execute("SELECT bpm, key_name, camelot FROM tracks WHERE id=?", (tid,)).fetchone()
    assert tuple(row) == (124.0, "A minor", "8A")
    assert db.query(conn, "SELECT count(*) n FROM series")[0]["n"] == 2

    card.sections = card.sections[:1]
    card.core.bpm = 125.0
    assert db.save_card(conn, card) == tid  # same sha256 -> same row
    assert db.query(conn, "SELECT count(*) n FROM sections")[0]["n"] == 1
    assert db.query(conn, "SELECT count(*) n FROM tracks")[0]["n"] == 1

    assert db.delete_track(conn, tid)
    assert db.query(conn, "SELECT count(*) n FROM sections")[0]["n"] == 0


def test_resolve_inputs(tmp_path, synth_wav):
    path, _ = synth_wav
    (tmp_path / "notes.txt").write_text("hi")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.wav").write_bytes(path.read_bytes())
    sources = resolve_inputs([str(tmp_path)])
    assert [s.title for s in sources] == ["a"]
    with pytest.raises(IngestError):
        resolve_inputs(["https://example.com/track/1"])
    with pytest.raises(IngestError):
        resolve_inputs([str(tmp_path / "missing.wav")])


@pytest.mark.audio
def test_cli_end_to_end(db_path, synth_wav, capsys):
    path, _ = synth_wav
    assert main(["--db", str(db_path), "analyze", str(path)]) == 0
    assert "synth_124_Am" in capsys.readouterr().out
    assert main(["--db", str(db_path), "show", "1"]) == 0
    card = json.loads(capsys.readouterr().out)
    assert set(card["stages_run"]) == {"core", "sections", "tags", "suggestions"}
    assert card["errors"] == {}
    assert main(["--db", str(db_path), "query", "SELECT title FROM tracks"]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == {"title": "synth_124_Am"}


def test_truth_core_fixture(synth_wav, truth_core, db_path):
    from tastr.service import analyze_inputs

    path, truth = synth_wav
    [res] = analyze_inputs(db.connect(db_path), [str(path)])
    assert res.error is None
    assert res.card.core.bpm == truth["bpm"]
    assert res.card.core.key.camelot == "8A"
    assert res.card.suggestions[0].category == "setup"


def test_web_read_api(db_path):
    from fastapi.testclient import TestClient

    from tastr.web.server import create_app

    conn = db.connect(db_path)
    tid = db.save_card(conn, full_card())
    client = TestClient(create_app(db_path))
    assert client.get("/api/tracks").json()[0]["camelot"] == "8A"
    assert client.get(f"/api/tracks/{tid}").json()["core"]["bpm"] == 124.0
    assert client.get("/api/tracks/999").status_code == 404


def test_cli_bad_input_is_clean_error(db_path, capsys):
    assert main(["--db", str(db_path), "analyze", "https://example.com/track/1"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "Traceback" not in err
    assert main(["--db", str(db_path), "analyze", "/no/such/file.wav"]) == 2
