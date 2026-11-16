"""A track's earlier cards: a re-analysis keeps the card it replaces in card_history (the newest few per track)."""

import json

import pytest
from fastapi.testclient import TestClient

from tastr import db
from tastr.engine import rust
from tastr.web.server import create_app
from test_foundation import full_card


@pytest.fixture
def client(db_path, tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    return TestClient(create_app(db_path))


def _card(bpm, core="0.6.0", sha="h1"):
    card = full_card()
    card.core.bpm = bpm
    card.stages_run = {**rust.STAGES, "core": core}
    card.source.sha256, card.source.uri = sha, f"/{sha}.wav"
    card.extras["views"] = {"waveform": [0.1] * 100}
    return card


def test_reanalysis_keeps_the_card_it_replaces(db_path):
    conn = db.connect(db_path)
    tid = db.save_card(conn, _card(117.45, core="0.5.0"))
    assert db.card_history(conn, tid) == []  # a first analysis replaces nothing
    assert db.save_card(conn, _card(118.79)) == tid
    (h,) = db.card_history(conn, tid, full=True)
    assert (h["bpm"], h["core_version"], h["stages"]["core"]) == (117.45, "0.5.0", "0.5.0")
    assert h["key_name"] == full_card().core.key.name and h["camelot"] and h["energy"] is not None
    assert h["analyzed_at"] and h["replaced_at"] >= h["analyzed_at"]
    assert h["card"]["core"]["bpm"] == 117.45 and "views" not in h["card"]["extras"]  # the waveform stays out
    assert conn.execute("SELECT bpm FROM tracks WHERE id = ?", (tid,)).fetchone()[0] == 118.79  # the card is the new one


def test_keeps_only_the_newest_few_per_track(db_path):
    conn = db.connect(db_path)
    tid = db.save_card(conn, _card(100.0))
    other = db.save_card(conn, _card(90.0, sha="h2"))
    for i in range(1, db.CARD_HISTORY_KEEP + 3):
        db.save_card(conn, _card(100.0 + i))
    db.save_card(conn, _card(91.0, sha="h2"))
    bpms = [h["bpm"] for h in db.card_history(conn, tid)]
    assert bpms == [100.0 + i for i in range(db.CARD_HISTORY_KEEP + 1, 1, -1)]  # newest first, the oldest dropped
    assert [h["bpm"] for h in db.card_history(conn, other)] == [90.0]  # each track keeps its own


def test_deleting_a_track_deletes_its_history(db_path):
    conn = db.connect(db_path)
    tid = db.save_card(conn, _card(120.0))
    db.save_card(conn, _card(121.0))
    assert db.delete_track(conn, tid)
    assert conn.execute("SELECT COUNT(*) FROM card_history").fetchone()[0] == 0


def test_history_api(client, db_path):
    conn = db.connect(db_path)
    tid = db.save_card(conn, _card(117.45, core="0.5.0"))
    db.save_card(conn, _card(118.79))
    conn.close()
    rows = client.get(f"/api/tracks/{tid}/history").json()
    assert len(rows) == 1 and rows[0]["bpm"] == 117.45 and rows[0]["stages"]["core"] == "0.5.0"
    assert "card" not in rows[0] and "card_json" not in rows[0] and "stages_json" not in rows[0]
    full = client.get(f"/api/tracks/{tid}/history?full=1").json()
    assert full[0]["card"]["core"]["bpm"] == 117.45
    assert client.get("/api/tracks/999/history").status_code == 404


def test_history_is_in_the_sql_search(db_path):
    """Find › SQL: "which tracks moved more than 1 BPM after 0.6.0?" can be asked."""
    conn = db.connect(db_path)
    tid = db.save_card(conn, _card(117.45, core="0.5.0"))
    db.save_card(conn, _card(118.79))
    still = db.save_card(conn, _card(124.0, core="0.5.0", sha="h2"))
    db.save_card(conn, _card(124.2, sha="h2"))
    conn.close()
    cols, rows = db.run_query(db_path, "SELECT t.id, h.bpm, t.bpm FROM card_history h JOIN tracks t ON t.id = h.track_id "
                              "WHERE h.core_version = '0.5.0' AND abs(t.bpm - h.bpm) > 1", 10)
    assert rows == [[tid, 117.45, 118.79]] and still not in [r[0] for r in rows]


def test_snapshot_has_history_without_private_columns(db_path):
    conn = db.connect(db_path)
    mine = _card(117.45, core="0.5.0")
    tid = db.save_card(conn, mine, owner="me@example.com")
    db.save_card(conn, _card(118.79), owner="me@example.com")
    theirs = db.save_card(conn, _card(90.0, sha="h2"), owner="them@example.com")
    db.save_card(conn, _card(91.0, sha="h2"), owner="them@example.com")
    conn.close()
    import sqlite3
    snap = sqlite3.connect(":memory:")
    snap.deserialize(db.library_snapshot(db_path, "me@example.com"))
    cols = {r[1] for r in snap.execute("PRAGMA table_info(card_history)")}
    assert {"track_id", "bpm", "core_version", "analyzed_at"} <= cols and not cols & {"stages_json", "card_json"}
    assert snap.execute("SELECT track_id FROM card_history").fetchall() == [(tid,)]  # not theirs
    admin = sqlite3.connect(":memory:")
    admin.deserialize(db.library_snapshot(db_path, None))
    cols = {r[1] for r in admin.execute("PRAGMA table_info(card_history)")}
    assert "stages_json" in cols and "card_json" not in cols  # an admin's copy stays small too
    assert {r[0] for r in admin.execute("SELECT track_id FROM card_history")} == {tid, theirs}
    json.loads(admin.execute("SELECT stages_json FROM card_history LIMIT 1").fetchone()[0])


def test_find_lists_the_table_and_its_example_runs(db_path):
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "tastr/web/static/app/find.js").read_text()
    assert '["card_history", "A track\'s earlier cards' in src
    line = next(x for x in src.splitlines() if x.lstrip().startswith('["BPM changed on re-analysis"'))
    sql = json.loads("[" + line.strip().rstrip(",") + "]")[0][1]
    conn = db.connect(db_path)
    db.save_card(conn, _card(110.0, core="0.4.0"))
    db.save_card(conn, _card(117.45, core="0.5.0"))
    db.save_card(conn, _card(118.79))
    db.save_card(conn, _card(124.0, core="0.5.0", sha="h2"))
    db.save_card(conn, _card(124.2, sha="h2"))  # moved less than 1 BPM
    conn.close()
    cols, rows = db.run_query(db_path, sql, 10)
    assert cols == ["title", "was_engine", "was_bpm", "now_bpm"]
    assert [r[1:] for r in rows] == [["0.5.0", 117.45, 118.79]]  # only the latest earlier card is compared
