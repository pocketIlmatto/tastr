import csv

import pytest
from fastapi.testclient import TestClient

from tastr import db, labels
from tastr.cli import main
from tastr.engine.bench.datasets import manifest
from tastr.web.server import create_app
from test_foundation import full_card
from ui_src import app_js


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
    assert q[0]["reasons"] == ["freqblog lists 87 BPM, not Tastr's 124"] and q[1]["reasons"] == ["BPM 55% likely right"]
    assert (q[0]["catalog_bpm"], q[0]["catalog_key"], q[0]["camelot"], q[0]["mine"]) == (87.0, None, "8A", False)
    assert sure not in [r["track_id"] for r in q]


def test_review_reasons_spell_out_the_catalog(db_path):
    conn = db.connect(db_path)
    tid = db.save_card(conn, card(0.95, 0.9))  # 124 BPM, A minor (8A)
    for provider, bpm, tonic, mode in (("freqblog", 62.0, 0, "major"), ("acousticbrainz", 248.0, 4, "minor"),
                                       ("getsongbpm", 124.0, 2, "minor"), ("other", 124.0, 9, "major")):
        db.save_lookup(conn, tid, {"provider": provider, "status": "found", "bpm": bpm, "tonic": tonic, "mode": mode})
    (row,) = labels.review_queue(conn)
    assert set(row["reasons"]) == {
        "freqblog lists 62 BPM, half Tastr's", "freqblog lists C major (8B), the relative key: same notes, other mode",
        "acousticbrainz lists 248 BPM, double Tastr's", "acousticbrainz lists E minor (9A), a fifth above Tastr's A minor",
        "getsongbpm lists D minor (7A), a fifth below Tastr's A minor",
        "other lists A major (11B), the parallel key of Tastr's A minor: same root, other mode"}
    assert (row["catalog_bpm"], row["catalog_key"]) in ((62.0, "8B"), (248.0, "9A"), (62.0, "9A"), (248.0, "8B"))


def test_review_flags_put_tracks_in_and_out(db_path):
    conn = db.connect(db_path)
    sure = db.save_card(conn, card(0.95, 0.9, sha="a"))
    shaky = db.save_card(conn, card(0.3, 0.9, sha="b"))
    labeled = db.save_card(conn, card(0.95, 0.9, sha="c"))
    db.set_labels(conn, labeled, {"bpm": 124})
    db.set_review_flag(conn, sure, "flagged")
    db.set_review_flag(conn, labeled, "flagged")
    db.set_review_flag(conn, shaky, "skipped")
    q = labels.review_queue(conn)
    assert {r["track_id"] for r in q} == {sure, labeled} and all(r["mine"] for r in q)
    assert {r["track_id"]: r["reasons"][0] for r in q} == {
        sure: "You added this to review", labeled: "You put this back in review (it has a label)"}
    db.set_labels(conn, sure, labels.clean({"bpm": 124, "key": "A minor", "kept": True}))  # saving drops the flag
    assert [r["track_id"] for r in labels.review_queue(conn)] == [labeled]
    db.set_review_flag(conn, shaky, None)
    assert shaky in [r["track_id"] for r in labels.review_queue(conn)]
    with pytest.raises(ValueError):
        db.set_review_flag(conn, shaky, "maybe")


def test_kept_labels_remember_tastrs_reading(db_path):
    conn = db.connect(db_path)
    tid = db.save_card(conn, card())
    got = db.set_labels(conn, tid, labels.clean({"bpm": 124, "key_name": "A minor", "kept": True}))
    assert (got["kept"], got["tastr_bpm"], got["tastr_key"]) == (True, 124.0, "A minor")
    assert labels.clean({"kept": True}) == {}  # nothing to keep
    assert db.set_labels(conn, tid, labels.clean({"bpm": 62}))["kept"] is False
    (row,) = db.list_labels(conn)
    assert (row["bpm"], row["tastr_bpm"], row["kept"]) == (62.0, 124.0, False)


def test_tracks_keep_their_upload_time(db_path):
    conn = db.connect(db_path)
    tid = db.save_card(conn, card())
    first = db.list_tracks(conn)[0]["created_at"]
    assert first
    conn.execute("UPDATE tracks SET created_at = '2026-01-02T03:04:05+00:00' WHERE id = ?", (tid,))
    db.save_card(conn, card())  # re-analysis keeps it
    assert db.list_tracks(conn)[0]["created_at"] == "2026-01-02T03:04:05+00:00"


def test_old_databases_get_upload_times_and_label_columns(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    old = sqlite3.connect(p)
    schema = db.SCHEMA.replace("    created_at      TEXT,\n", "")
    labels_ddl = schema[schema.index("CREATE TABLE IF NOT EXISTS track_labels"):]
    labels_ddl = labels_ddl[:labels_ddl.index(");") + 2]
    schema = schema.replace(labels_ddl, "CREATE TABLE track_labels (track_id INTEGER PRIMARY KEY, bpm REAL, key_name TEXT, "
                                        "boundaries TEXT, note TEXT, updated_at TEXT NOT NULL);")
    assert "created_at      TEXT" not in schema and "tastr_bpm" not in schema
    old.executescript(schema + """
        INSERT INTO tracks (id, source_kind, source_uri, analyzed_at) VALUES (1, 'file', '/a.wav', '2026-05-01T00:00:00+00:00');
        INSERT INTO track_labels VALUES (1, 120, NULL, NULL, NULL, '2026-05-02T00:00:00+00:00');
    """)
    old.commit()
    old.close()
    conn = db.connect(p)
    assert conn.execute("SELECT created_at FROM tracks").fetchone()[0] == "2026-05-01T00:00:00+00:00"
    assert db.get_labels(conn, 1)["kept"] is False


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
    (row,) = c.get("/api/labels").json()
    assert (row["track_id"], row["bpm"], row["tastr_bpm"], row["kept"]) == (tid, 128.0, 124.0, False) and "path" not in row
    assert c.put(f"/api/tracks/{tid}/review", json={"state": "flagged"}).json() == {"state": "flagged"}
    assert c.get("/api/review").json()[0]["mine"] is True
    assert c.put(f"/api/tracks/{tid}/review", json={"state": "later"}).status_code == 400
    assert c.put("/api/tracks/999/review", json={"state": None}).status_code == 404
    assert c.put(f"/api/tracks/{tid}/labels", json={"bpm": 124, "kept": True}).json()["kept"] is True
    assert c.get("/api/review").json() == []  # saving a label took it off the list you put it on
    assert c.get("/api/tracks").json()[0]["created_at"]


def test_labels_cli(db_path, tmp_path, capsys):
    tid = db.save_card(db.connect(db_path), card(0.4, 0.9, path=tmp_path / "missing.wav"))
    assert main(["--db", str(db_path), "review"]) == 0
    assert "BPM 40% likely right" in capsys.readouterr().out
    assert main(["--db", str(db_path), "labels", "set", str(tid), "--bpm", "nope"]) == 2
    assert main(["--db", str(db_path), "labels", "set", str(tid), "--bpm", "174", "--key", "Fm"]) == 0
    assert main(["--db", str(db_path), "labels", "export", str(tmp_path / "m.csv")]) == 0
    assert "wrote 0 labeled tracks" in capsys.readouterr().out  # its audio isn't on disk
    assert main(["--db", str(db_path), "labels", "clear", str(tid)]) == 0


def test_ui_key_names_match_the_server():
    """The label panel's Camelot <-> key name helpers agree with the server's parse_key and the review reasons."""
    import json
    import re
    import subprocess

    from tastr.engine.bench.datasets import parse_key
    from tastr.engine.music import camelot

    js = re.sub(r"^export ", "", app_js(), flags=re.M)
    grab = lambda n: re.search(rf"^(const {n} = .*?;|function {n}\(.*?\n}})$", js, re.M | re.S).group(0)
    defs = "\n".join(grab(n) for n in ("MAJ_ASCII", "camPc", "CAM_NAME", "ALL_CAMS", "PC_OF", "camOf", "nameToCam"))
    script = defs + """
console.log(JSON.stringify({ names: ALL_CAMS.map((c) => [c, CAM_NAME(c)]),
  parsed: ["A minor", "Am", "C# major", "Db major", "f#m", "Bb minor", "8a", "E♭ major", "H minor"].map(nameToCam) }));
"""
    got = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    for cam, name in got["names"]:
        assert camelot(*parse_key(name)) == cam and labels.key_label(*parse_key(name)) == name
    assert got["parsed"] == ["8A", "8A", "3B", "3B", "11A", "3A", "8A", "5B", None]
