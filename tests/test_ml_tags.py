"""Optional ML tags (tastr/engine/ml_tags.py, tastr/ml_jobs.py): the tag mapping, storage that survives re-analysis
and copies, the API, the Modal route and the CLI. Essentia never runs here: `ml_tags.analyze` is a stub."""

import time

import pytest
from fastapi.testclient import TestClient

from tastr import cli, db, library_copy, ml_jobs, service
from tastr.engine import ml_tags
from tastr.web.server import create_app
from test_foundation import full_card


def heads(**over):
    h = {"genre_discogs400": {"Electronic---Deep House": 0.41, "Electronic---House": 0.22, "Electronic---Techno": 0.12,
                              "Rock---Punk": 0.001},
         "mood_relaxed": {"relaxed": 0.2, "non_relaxed": 0.8}, "mood_party": {"party": 0.91, "non_party": 0.09},
         "mood_sad": {"sad": 0.1, "non_sad": 0.9}, "mood_happy": {"happy": 0.75, "non_happy": 0.25},
         "mood_aggressive": {"aggressive": 0.1, "not_aggressive": 0.9},
         "mtg_jamendo_moodtheme": {"energetic": 0.3, "happy": 0.2, "film": 0.5, "dream": 0.13, "deep": 0.125,
                                   "dark": 0.01},
         "voice_instrumental": {"instrumental": 0.9, "voice": 0.1}}
    h.update(over)
    return h


RESULT = {"version": ml_tags.VERSION, "heads": heads(), "tags": ml_tags.tags(heads())}


def test_tags_from_outputs():
    t = ml_tags.tags(heads())
    assert [x["tag"] for x in t if x["group"] == "style"] == ["Deep House", "House"]  # Techno is under the cut
    moods = [x["tag"] for x in t if x["group"] == "mood"]
    assert moods == ["happy", "party", "energetic", "dreamy"]  # "film" isn't a mood; "happy" not twice; top 3 only
    assert [x["tag"] for x in t if x["group"] == "voice"] == ["instrumental"]
    t = ml_tags.tags(heads(voice_instrumental={"voice": 0.5}, genre_discogs400={"Jazz---Bop": 0.11}))
    assert not [x for x in t if x["group"] == "voice"]  # undecided
    assert [x["tag"] for x in t if x["group"] == "style"] == ["Bop"]  # the top style counts from 0.1


def test_compact_keeps_the_top_of_big_heads():
    big = {f"Style---{i}": i / 1000 for i in range(400)}
    out = ml_tags.compact({"genre_discogs400": big, "mood_sad": {"sad": 0.4, "non_sad": 0.6}})
    assert len(out["genre_discogs400"]) == 10 and "Style---399" in out["genre_discogs400"]
    assert out["mood_sad"] == {"sad": 0.4, "non_sad": 0.6}


def test_download_command_and_switch(monkeypatch):
    assert ml_tags.download_command("/m").count("curl ") == 2 * len(ml_tags.model_paths())
    monkeypatch.delenv("TASTR_ML_TAGS", raising=False)
    assert not ml_tags.switched_on() and not ml_jobs.available()
    assert "TASTR_ML_TAGS=on" in ml_jobs.why_not()
    monkeypatch.setenv("TASTR_ML_TAGS", "on")
    assert ml_tags.switched_on()


@pytest.fixture
def track(db_path, tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"RIFF" + bytes(100))
    card = full_card()
    card.source.local_path = str(audio)
    with db.connect(db_path) as conn:
        tid = db.save_card(conn, card, owner="a@x")
    return tid


@pytest.fixture
def stub(monkeypatch):
    calls = []

    def analyze(path):
        calls.append(str(path))
        return {**RESULT, "seconds": {"embed": 1.0}}

    monkeypatch.setenv("TASTR_ML_TAGS", "on")
    monkeypatch.delenv("TASTR_ANALYSIS_BACKEND", raising=False)
    monkeypatch.setattr(ml_tags, "installed", lambda: True)
    monkeypatch.setattr(ml_tags, "analyze", analyze)
    return calls


def test_tag_track_stores_and_survives_reanalysis_and_copies(db_path, track, stub, monkeypatch):
    conn = db.connect(db_path)
    result = ml_jobs.tag_track(conn, track)
    assert stub and "seconds" not in result
    stored = db.get_ml_tags(conn, track)
    assert stored["tags"] == RESULT["tags"] and stored["created_at"]
    assert conn.execute("SELECT tags FROM ml_tags WHERE track_id = ?", (track,)).fetchone()[0].startswith("Deep House,")

    def fake_analyze(source, options, on_stage=None, browser=None):
        card = full_card()
        card.source = source
        return card

    monkeypatch.setattr(service, "_analyze", fake_analyze)
    assert service.reanalyze_track(conn, track).error is None
    assert db.get_ml_tags(conn, track)["tags"] == RESULT["tags"]  # re-analysis keeps them
    new = library_copy.copy_tracks(conn, [track], "a@x", "b@x")[track]
    assert db.get_ml_tags(conn, new)["tags"] == RESULT["tags"]  # so does a copy
    assert ml_jobs.tag_track(conn, 9999) is None
    db.delete_track(conn, track)
    assert db.get_ml_tags(conn, track) is None


def wait_for(client, tid, state, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        body = client.get(f"/api/tracks/{tid}/ml-tags").json()
        if body["state"] == state:
            return body
        time.sleep(0.02)
    raise AssertionError(f"ML tags for track {tid} never reached {state}: {body}")


def test_api_off_by_default_then_runs_in_the_background(db_path, track, tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TASTR_ML_TAGS", raising=False)
    client = TestClient(create_app(db_path))
    r = client.post(f"/api/tracks/{track}/ml-tags")
    assert r.status_code == 501 and "TASTR_ML_TAGS=on" in r.json()["detail"]
    card = client.get(f"/api/tracks/{track}").json()
    assert card["ml_tags"] is None and card["ml_tags_available"] is False

    calls = []
    monkeypatch.setenv("TASTR_ML_TAGS", "on")
    monkeypatch.setattr(ml_tags, "installed", lambda: True)
    monkeypatch.setattr(ml_tags, "analyze", lambda path: calls.append(path) or dict(RESULT))
    r = client.post(f"/api/tracks/{track}/ml-tags")
    assert r.status_code == 202
    body = wait_for(client, track, "done")
    assert body["result"]["tags"] == RESULT["tags"] and len(calls) == 1
    card = client.get(f"/api/tracks/{track}").json()
    assert card["ml_tags"]["tags"] == RESULT["tags"] and card["ml_tags_available"] and card["ml_tags_state"] == "done"
    assert "ml_tags_error" not in card
    row = client.get("/api/tracks").json()[0]  # the track list has the names, for the Library's vibe filter
    assert row["ml_tags"] == [t["tag"] for t in RESULT["tags"]]
    assert client.get("/api/tracks/9999/ml-tags").status_code == 404

    monkeypatch.setattr(ml_tags, "analyze", lambda path: (_ for _ in ()).throw(RuntimeError("bad file")))
    client.post(f"/api/tracks/{track}/ml-tags")
    assert "bad file" in wait_for(client, track, "error")["error"]
    assert "bad file" in client.get(f"/api/tracks/{track}").json()["ml_tags_error"]  # the card's failed note shows it


def test_track_list_without_ml_tags_says_so(db_path, track):
    row = TestClient(create_app(db_path)).get("/api/tracks").json()[0]
    assert row["ml_tags"] is None  # not in yet: the page keeps the engine's vibes


def test_hosted_server_runs_them_on_modal(db_path, track, monkeypatch):
    from tastr.cloud import remote

    sent = []
    monkeypatch.setenv("TASTR_ML_TAGS", "on")
    monkeypatch.setenv("TASTR_ANALYSIS_BACKEND", "modal")
    monkeypatch.setattr(remote, "available", lambda: True)
    monkeypatch.setattr(remote, "ml_tags", lambda path: sent.append(path) or dict(RESULT))
    assert ml_jobs.available()
    with db.connect(db_path) as conn:
        assert ml_jobs.tag_track(conn, track)["tags"] == RESULT["tags"]
    assert sent and sent[0].endswith("song.wav")


def test_cli(db_path, track, stub, capsys, monkeypatch):
    assert cli.main(["--db", str(db_path), "ml-tags", "run", "--all"]) == 0
    out = capsys.readouterr().out
    assert f"{track}: Deep House, House, happy, party, energetic, dreamy, instrumental" in out and "non-commercial" in out
    assert cli.main(["--db", str(db_path), "ml-tags", "run"]) == 2  # --all finds none left, and no ids given
    monkeypatch.delenv("TASTR_ML_TAGS")
    assert cli.main(["--db", str(db_path), "ml-tags", "status"]) == 0
    assert "TASTR_ML_TAGS=on" in capsys.readouterr().out


def test_auto_tags_new_uploads_and_the_library_once(db_path, track, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TASTR_LOOKUP", "")
    monkeypatch.delenv("TASTR_ANALYSIS_BACKEND", raising=False)
    monkeypatch.setattr(ml_tags, "installed", lambda: True)
    monkeypatch.setattr(ml_tags, "analyze", lambda path: calls.append(path) or dict(RESULT))

    def fake_analyze(source, options, on_stage=None, browser=None):
        card = full_card()
        card.source = source
        return card

    monkeypatch.setattr(service, "_analyze", fake_analyze)

    monkeypatch.setenv("TASTR_ML_TAGS", "on")  # on: offered, but only on request
    assert ml_tags.switched_on() and not ml_tags.automatic() and not ml_jobs.automatic()
    with TestClient(create_app(db_path)) as client:
        r = client.post("/api/analyze", files=[("files", ("one.wav", b"RIFF" + bytes(100), "audio/wav"))])
        first = r.json()["results"][0]["track_id"]
        assert first and client.get(f"/api/tracks/{first}/ml-tags").json()["state"] == "none"
    assert not calls

    monkeypatch.setenv("TASTR_ML_TAGS", "auto")
    with TestClient(create_app(db_path)) as client:  # the start-up pass tags the library once
        wait_for(client, track, "done")
        wait_for(client, first, "done")
        r = client.post("/api/analyze", files=[("files", ("two.wav", b"RIFF" + bytes(101), "audio/wav"))])
        second = r.json()["results"][0]["track_id"]
        wait_for(client, second, "done")  # and every new upload gets them
    with db.connect(db_path) as conn:
        assert all(db.get_ml_tags(conn, t) for t in (track, first, second)) and not ml_jobs.untagged(conn)
        conn.execute("DELETE FROM ml_tags")
    n = len(calls)
    assert n == 3
    with TestClient(create_app(db_path)) as client:  # the pass ran once: a restart doesn't tag the library again
        assert client.get(f"/api/tracks/{track}/ml-tags").json()["state"] == "none"
    assert len(calls) == n
