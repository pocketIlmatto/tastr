"""Pattern reviews: Right / Close / Way off on the generated drums, bass and chords, per section (tastr/labels.py)."""

import json

import pytest
from fastapi.testclient import TestClient

from tastr import db, labels
from tastr.cli import main
from tastr.engine.card import Section, Suggestion
from tastr.web.server import create_app
from test_foundation import full_card

KICK, KICK2 = "x...x...x...x...", "x.......x......."


def card(drop_kick=KICK, sha="a", chords_by_section=True):
    c = full_card()  # 124 BPM, A minor
    c.source.sha256, c.source.uri = sha * 64, f"/music/{sha}.wav"
    c.core.bpm_likely_correct, c.core.key.likely_correct = 0.95, 0.95  # BPM and key look right: not in review
    c.sections = [Section(0, 8, "intro", "A", start_bar=1, end_bar=4), Section(8, 24, "drop", "B", start_bar=5, end_bar=12)]
    secs = [{"idx": 0, "label": "intro", "letter": "A", "start_bar": 1, "end_bar": 4},
            {"idx": 1, "label": "drop", "letter": "B", "start_bar": 5, "end_bar": 12}]
    c.suggestions = [
        Suggestion("drums", "Detected groove", "...", {"origin": "detected", "pattern": {"kick": KICK},
                   "by_section": [secs[0] | {"pattern": {"kick": KICK}}, secs[1] | {"pattern": {"kick": drop_kick}}]}),
        Suggestion("drums", "Pattern idea: house", "...", {"origin": "template", "pattern": {"kick": KICK}}),
        Suggestion("bass", "Bass loop", "...", {"loop": ["A", "C"], "rhythm": KICK2}),  # an older card: no by_section
        Suggestion("harmony", "Chord loop guess", "...", {"progression": ["Am", "F", "C", "G"],
                   **({"by_section": [s | {"progression": ["Am", None, "C", "G"]} for s in secs]} if chords_by_section else {})}),
    ]
    c.stages_run = {"suggestions": "0.5.0"}
    return c


def test_detected_patterns_follow_the_app():
    found = labels.detected_patterns(card())
    assert set(found) == {(p, a, b) for p in ("drums", "bass", "chords") for a, b in ((1, 4), (5, 12))}
    assert found[("drums", 5, 12)]["pattern"] == {"kick": KICK}
    assert found[("bass", 1, 4)]["pattern"] == {"notes": ["A", "C"], "rhythm": KICK2}  # the one loop, every section
    assert found[("chords", 5, 12)] == {"section": {"idx": 1, "label": "drop", "letter": "B"},
                                        "pattern": {"chords": ["Am", None, "C", "G"]}}
    c = card()
    c.sections, c.suggestions = [], [s for s in c.suggestions if s.category != "drums"]
    for s in c.suggestions:
        s.data.pop("by_section", None)
    assert set(labels.detected_patterns(c)) == {("bass", 0, 0), ("chords", 0, 0)}


def test_reviews_survive_reanalysis_and_go_stale_when_the_pattern_changes(db_path):
    conn = db.connect(db_path)
    tid = db.save_card(conn, card())
    labels.save_pattern_review(conn, tid, {"part": "drums", "section": 1, "verdict": "right"})
    labels.save_pattern_review(conn, tid, {"part": "chords", "section": 0, "verdict": "close", "note": " nearly "})
    (d, ch) = sorted(labels.pattern_reviews(conn, tid), key=lambda r: r["part"])[::-1]
    assert (d["part"], d["start_bar"], d["end_bar"], d["verdict"], d["stale"]) == ("drums", 5, 12, "right", False)
    assert (d["judged"], d["stage_version"], d["section"]["label"]) == ({"kick": KICK}, "0.5.0", "drop")
    assert (ch["verdict"], ch["note"]) == ("close", "nearly")
    db.save_card(conn, card(drop_kick=KICK2))  # a re-analysis that hears the drop's kick differently
    stale = {r["part"]: r["stale"] for r in labels.pattern_reviews(conn, tid)}
    assert stale == {"drums": True, "chords": False}
    labels.save_pattern_review(conn, tid, {"part": "drums", "section": 1})  # no verdict and no pattern: removed
    assert [r["part"] for r in labels.pattern_reviews(conn, tid)] == ["chords"]


def test_bad_reviews_are_refused(db_path):
    conn = db.connect(db_path)
    tid = db.save_card(conn, card())
    for bad in ({"part": "vocals", "section": 0, "verdict": "right"}, {"part": "drums", "section": 0, "verdict": "meh"},
                {"part": "drums", "section": 7, "verdict": "right"}, {"part": "drums", "section": "all", "verdict": "right"},
                {"part": "drums", "section": 0, "truth": {"kick": "boom"}}, {"part": "bass", "section": 0, "truth": {"notes": "A"}},
                {"part": "bass", "section": 0, "truth": {"notes": ["A"], "rhythm": 4}}):
        with pytest.raises(labels.LabelError):
            labels.save_pattern_review(conn, tid, bad)


def test_way_off_and_stale_marks_put_tracks_in_review(db_path):
    conn = db.connect(db_path)
    fine = db.save_card(conn, card(sha="a"))
    off = db.save_card(conn, card(sha="b"))
    db.set_labels(conn, off, {"bpm": 124, "kept": True})  # BPM and key labeled: only its patterns can list it
    shaky = db.save_card(conn, card(sha="c"))
    db.set_review_flag(conn, shaky, "flagged")
    assert [r["track_id"] for r in labels.review_queue(conn)] == [shaky]
    labels.save_pattern_review(conn, fine, {"part": "bass", "section": 0, "verdict": "right"})
    labels.save_pattern_review(conn, off, {"part": "bass", "section": 1, "verdict": "way_off"})
    q = labels.review_queue(conn)
    assert [r["track_id"] for r in q] == [shaky, off]  # yours first, then Way off
    assert (q[1]["reasons"], q[1]["way_off"]) == (["Bass way off in Drop B"], 1)
    # Saving the right pattern settles it.
    labels.save_pattern_review(conn, off, {"part": "bass", "section": 1, "verdict": "way_off",
                                           "truth": {"notes": ["D", "F"], "rhythm": KICK}})
    assert [r["track_id"] for r in labels.review_queue(conn)] == [shaky]
    c = card(sha="a", drop_kick=KICK2)
    c.suggestions[2].data["loop"] = ["E", "G"]
    db.save_card(conn, c)  # re-analysis of `fine` changes its bass
    assert labels.review_queue(conn)[1]["reasons"] == ["Bass in Intro A changed since you marked it"]


def test_pattern_reviews_api_cli_and_export(db_path, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path))
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"x")
    c0 = card()
    c0.source.local_path = str(audio)
    tid = db.save_card(db.connect(db_path), c0)
    c = TestClient(create_app(db_path))
    assert c.get(f"/api/tracks/{tid}/pattern-reviews").json() == []
    r = c.put(f"/api/tracks/{tid}/pattern-reviews", json={"part": "chords", "section": 1, "verdict": "way_off",
                                                           "truth": {"chords": ["Dm", "", "C", "G"], "roman": ["iv"]}})
    assert r.status_code == 200 and r.json()[0]["truth"] == {"chords": ["Dm", None, "C", "G"]}
    assert c.put(f"/api/tracks/{tid}/pattern-reviews", json={"part": "chords", "verdict": "nah"}).status_code == 400
    assert c.put("/api/tracks/999/pattern-reviews", json={"part": "chords", "verdict": "right"}).status_code == 404
    (row,) = c.get("/api/pattern-reviews").json()
    assert (row["track_id"], row["verdict"], row["stale"]) == (tid, "way_off", False) and "path" not in row
    assert main(["--db", str(db_path), "labels", "patterns"]) == 0
    assert "chords bars 5-12: way off, right pattern saved" in capsys.readouterr().out
    out = tmp_path / "patterns.json"
    assert main(["--db", str(db_path), "labels", "export", "--patterns", str(out)]) == 0
    (t,) = json.loads(out.read_text())
    assert (t["id"], t["path"], t["reviews"][0]["truth"]) == (f"track-{tid}", str(audio.resolve()), {"chords": ["Dm", None, "C", "G"]})
