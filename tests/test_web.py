"""Tests for the web API and static UI."""

import pytest
from fastapi.testclient import TestClient

from tastr import db
from tastr.engine.card import Source, TrackCard
from tastr.web.server import create_app
from test_foundation import full_card


@pytest.fixture
def client(db_path, tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    return TestClient(create_app(db_path))


@pytest.fixture
def seeded(client, db_path):
    conn = db.connect(db_path)
    tid = db.save_card(conn, full_card())
    conn.close()
    return tid


def test_upload_analyzes_and_lists(client, synth_wav, truth_core):
    path, _ = synth_wav
    with open(path, "rb") as f:
        r = client.post("/api/analyze", files=[("files", (path.name, f, "audio/wav"))])
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["error"] is None and res["track_id"]
    ids = [t["id"] for t in client.get("/api/tracks").json()]
    assert res["track_id"] in ids


def test_get_and_delete_track(client, seeded):
    assert client.get(f"/api/tracks/{seeded}").json()["core"]["bpm"] == 124.0
    assert client.delete(f"/api/tracks/{seeded}").status_code == 204
    assert client.delete(f"/api/tracks/{seeded}").status_code == 404
    assert client.get(f"/api/tracks/{seeded}").status_code == 404


def test_query_is_read_only(client, seeded):
    r = client.post("/api/query", json={"sql": "DELETE FROM tracks"})
    assert r.status_code == 400
    assert len(client.get("/api/tracks").json()) == 1


def test_query_returns_columns_and_rows(client, seeded):
    r = client.post("/api/query", json={"sql": "SELECT title, bpm FROM tracks"})
    assert r.json() == {"columns": ["title", "bpm"], "rows": [["x", 124.0]]}
    assert client.post("/api/query", json={"sql": "SELEKT"}).status_code == 400


def test_analyze_url_ingest_error_is_400(client):
    r = client.post("/api/analyze-url", json={"url": "https://example.invalid/track"})
    assert r.status_code == 400
    assert r.json()["detail"]


def test_index_and_static_assets(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    for ref in ("/static/app.js", "/static/style.css"):
        assert ref in r.text
        assert client.get(ref).status_code == 200


def test_empty_card_serves(client, db_path):
    conn = db.connect(db_path)
    tid = db.save_card(conn, TrackCard(source=Source(kind="file", uri="/e.wav", sha256="e")))
    conn.close()
    assert client.get(f"/api/tracks/{tid}").status_code == 200
