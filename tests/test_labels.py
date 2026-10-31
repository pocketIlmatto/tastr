import csv

import pytest
from fastapi.testclient import TestClient

from tastr import db, labels
from tastr.cli import main
from tastr.engine.bench.datasets import manifest
from tastr.web.server import create_app
from test_foundation import full_card


def card(bpm_p=None, key_p=None, path=None, sha="a"):
    c = full_card()  # 124 BPM, A minor
    c.source.sha256, c.source.uri = sha * 64, f"/music/{sha}.wav"
    c.core.bpm_likely_correct = bpm_p
    c.core.key.likely_correct = key_p
    c.source.local_path = str(path) if path else None
    return c


def test_clean_checks_and_normalizes():
    assert labels.clean({"bpm": "174", "key": "Am", "boundaries": "1:04, 0:00;0:32", "note": " dnb "}) == \
        {"bpm": 174.0, "key_name": "Am", "boundaries": "0 32 64", "note": "dnb"}
    assert labels.clean({"bpm": "", "key": "", "boundaries": None}) == {}
    for bad in ({"bpm": "fast"}, {"bpm": 900}, {"key": "H minor"}, {"boundaries": "soon"}):
        with pytest.raises(labels.LabelError):
            labels.clean(bad)


def test_labels_survive_reanalysis_and_export_as_a_manifest(db_path, tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"x")
    conn = db.connect(db_path)
    tid = db.save_card(conn, card(path=audio))
    assert db.get_labels(conn, tid) == {}
    got = db.set_labels(conn, tid, labels.clean({"bpm": 87, "key": "8A", "boundaries": "0:00 1:00"}))
    assert (got["bpm"], got["key_name"], got["boundaries"]) == (87.0, "8A", "0 60")
    db.save_card(conn, card(path=audio))  # re-analysis replaces the card, not the labels
    assert db.get_labels(conn, tid)["bpm"] == 87.0
    out = tmp_path / "labels.csv"
    assert labels.export_manifest(conn, out) == 1
    (item,) = manifest(out)
    assert (item.truth.bpm, item.truth.tonic, item.truth.mode, item.truth.boundaries) == (87.0, 9, "minor", [0.0, 60.0])
    assert list(csv.reader(out.open()))[1][0] == f"track-{tid}"
    db.set_labels(conn, tid, {})
    assert db.list_labels(conn) == []


def test_review_queue_ranks_disagreements_then_low_odds(db_path):
    conn = db.connect(db_path)
    sure = db.save_card(conn, card(0.95, 0.9, sha="a"))
    shaky = db.save_card(conn, card(0.55, 0.9, sha="b"))
    listed = db.save_card(conn, card(0.95, 0.9, sha="c"))
    db.save_lookup(conn, listed, {"provider": "freqblog", "status": "found", "bpm": 87.0, "key": "A minor",
                                  "tonic": 9, "mode": "minor"})
    labeled = db.save_card(conn, card(0.2, 0.2, sha="d"))
    db.set_labels(conn, labeled, {"bpm": 124})
    q = labels.review_queue(conn)
    assert [r["track_id"] for r in q] == [listed, shaky]  # confident and agreeing, or labeled: not listed
    assert q[0]["reasons"] == ["freqblog lists bpm differs"] and q[1]["reasons"] == ["BPM 55% likely right"]
    assert sure not in [r["track_id"] for r in q]


def test_labels_api(db_path, tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path))
    tid = db.save_card(db.connect(db_path), card(0.4, 0.9))
    c = TestClient(create_app(db_path))
    assert c.get(f"/api/tracks/{tid}/labels").json() == {}
    r = c.put(f"/api/tracks/{tid}/labels", json={"bpm": 128, "key_name": "F minor"})
    assert r.status_code == 200 and r.json()["key_name"] == "F minor"
    assert c.put(f"/api/tracks/{tid}/labels", json={"bpm": "x"}).status_code == 400
    assert c.put("/api/tracks/999/labels", json={"bpm": 120}).status_code == 404
    assert c.get("/api/review").json() == []  # now labeled


def test_labels_cli(db_path, tmp_path, capsys):
    tid = db.save_card(db.connect(db_path), card(0.4, 0.9, path=tmp_path / "missing.wav"))
    assert main(["--db", str(db_path), "review"]) == 0
    assert "BPM 40% likely right" in capsys.readouterr().out
    assert main(["--db", str(db_path), "labels", "set", str(tid), "--bpm", "nope"]) == 2
    assert main(["--db", str(db_path), "labels", "set", str(tid), "--bpm", "174", "--key", "Fm"]) == 0
    assert main(["--db", str(db_path), "labels", "export", str(tmp_path / "m.csv")]) == 0
    assert "wrote 0 labeled tracks" in capsys.readouterr().out  # its audio isn't on disk
    assert main(["--db", str(db_path), "labels", "clear", str(tid)]) == 0
