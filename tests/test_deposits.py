"""The bank: deposits beside the library (tastr/deposits.py, /api/deposits, `tastr deposit`)."""

import json

import pytest
from fastapi.testclient import TestClient
from test_foundation import full_card

from tastr import cli, db, deposits
from tastr.web.server import create_app


@pytest.fixture
def client(db_path, tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    return TestClient(create_app(db_path))


def _tracks(db_path, n: int, owner=None) -> list[int]:
    conn = db.connect(db_path)
    ids = []
    for i in range(n):
        card = full_card()
        card.source.sha256, card.source.uri, card.source.local_path = f"sha{owner}{i}", f"/x{i}.wav", f"/x{i}.wav"
        card.source.title = f"Track {i}"
        ids.append(db.save_card(conn, card, owner=owner))
    conn.close()
    return ids


def test_library_tracks_join_the_bank_once(db_path):
    a, b = _tracks(db_path, 2)
    conn = db.connect(db_path)
    assert deposits.sync_tracks(conn) == 2
    assert deposits.sync_tracks(conn) == 0  # nothing new: nothing added twice
    found = deposits.find(conn, None, size="track")
    assert found["total"] == 2
    assert {d["track_id"] for d in found["deposits"]} == {a, b}
    one = found["deposits"][0]
    assert one["size"] == "track" and one["track"]["title"].startswith("Track ") and one["status"] == "ready"
    assert db.load_card(conn, a) is not None  # the library's own rows are untouched


def test_deleted_track_drops_a_bare_deposit_and_keeps_an_annotated_one(db_path):
    a, b = _tracks(db_path, 2)
    conn = db.connect(db_path)
    deposits.sync_tracks(conn)
    by_track = {d["track_id"]: d["id"] for d in deposits.find(conn, None)["deposits"]}
    deposits.update(conn, None, by_track[b], {"note": "the bass drop at 2:10"})
    assert db.delete_track(conn, a) and db.delete_track(conn, b)
    deposits.sync_tracks(conn)
    left = deposits.find(conn, None)["deposits"]
    assert [d["id"] for d in left] == [by_track[b]]
    assert left[0]["track_id"] is None and left[0]["status"] == "needs-source" and left[0]["note"]


def test_moment_in_a_collection_touching_two_tracks(db_path):
    a, b = _tracks(db_path, 2)
    conn = db.connect(db_path)
    deposits.sync_tracks(conn)
    by_track = {d["track_id"]: d["id"] for d in deposits.find(conn, None)["deposits"]}
    dj_set = deposits.add(conn, None, "collection", title="Live set", source_kind="link",
                          source_uri="https://example.com/set", tags=["live", {"tag": "Peak", "origin": "rekordbox"}])
    assert deposits.hold(conn, None, dj_set["id"], [by_track[a], by_track[b], by_track[a], 999]) == 2
    moment = deposits.add(conn, None, "moment", note="the handover", start_s=938, end_s=990, held_by=dj_set["id"],
                          touches=[by_track[a], by_track[b]])
    assert [t["id"] for t in moment["touches"]] == [by_track[a], by_track[b]]
    listed = {d["id"]: d for d in deposits.find(conn, None)["deposits"]}
    assert listed[moment["id"]]["touches_count"] == 2 and listed[dj_set["id"]]["holds_count"] == 3
    assert moment["held_by"][0]["id"] == dj_set["id"] and moment["held_by"][0]["start_s"] == 938
    got = deposits.get(conn, None, dj_set["id"])
    assert [h["id"] for h in got["holds"]] == [by_track[a], by_track[b], moment["id"]]
    assert got["tags"] == [{"tag": "Peak", "origin": "rekordbox"}, {"tag": "live", "origin": "user"}]
    assert deposits.get(conn, None, by_track[a])["touched_by"][0]["id"] == moment["id"]
    # deleting the collection keeps what it held
    assert deposits.delete(conn, None, dj_set["id"])
    assert deposits.get(conn, None, moment["id"])["held_by"] == []
    assert deposits.find(conn, None)["total"] == 3


@pytest.mark.parametrize("kwargs, message", [
    ({"size": "moment", "start_s": 10}, "both a start and an end"),
    ({"size": "moment", "start_s": 10, "end_s": 5}, "after the start"),
    ({"size": "collection", "start_s": 1, "end_s": 5}, "Only a moment"),
    ({"size": "track"}, "by being analyzed"),
    ({"size": "album"}, "size is one of"),
    ({"size": "moment", "held_by": 12345}, "No deposit 12345"),
    ({"size": "moment", "tags": ["x" * 61]}, "at most 60"),
])
def test_what_cannot_be_deposited(db_path, kwargs, message):
    conn = db.connect(db_path)
    size = kwargs.pop("size")
    with pytest.raises(deposits.DepositError, match=message):
        deposits.add(conn, None, size, **kwargs)
    assert deposits.find(conn, None)["total"] == 0


def test_update_merges_and_replaces_tags_by_origin(db_path):
    conn = db.connect(db_path)
    d = deposits.add(conn, None, "moment", note="first", tags=["a", {"tag": "Dark", "origin": "rekordbox"}],
                     context={"heard_on": "spotify"})
    got = deposits.update(conn, None, d["id"], {"note": " second ", "tags": ["b"], "context": {"device": "phone"},
                                               "extracted": {"key": "Am"}, "status": "working"})
    assert got["note"] == "second" and got["status"] == "working"
    assert got["tags"] == [{"tag": "Dark", "origin": "rekordbox"}, {"tag": "b", "origin": "user"}]  # imports stay
    assert got["context"] == {"heard_on": "spotify", "device": "phone"} and got["extracted"] == {"key": "Am"}
    assert deposits.update(conn, None, d["id"], {"note": ""})["note"] is None
    assert deposits.update(conn, None, 999, {"note": "x"}) is None
    with pytest.raises(deposits.DepositError):
        deposits.update(conn, None, d["id"], {"status": "nope"})


def test_find_filters_and_each_person_sees_their_own(db_path):
    _tracks(db_path, 1, owner="a@example.com")
    conn = db.connect(db_path)
    deposits.add(conn, "a@example.com", "moment", note="filter sweep 100%", tags=["transition"])
    deposits.add(conn, "a@example.com", "collection", title="Warm-up")
    deposits.add(conn, "b@example.com", "moment", note="someone else's")
    deposits.sync_tracks(conn)
    mine = lambda **kw: deposits.find(conn, "a@example.com", **kw)  # noqa: E731
    assert mine()["total"] == 3 and deposits.find(conn, "b@example.com")["total"] == 1
    assert [d["size"] for d in mine(tag="transition")["deposits"]] == ["moment"]
    assert [d["size"] for d in mine(q="100%")["deposits"]] == ["moment"]  # % is text, not a wildcard
    assert mine(q="1__%")["total"] == 0
    assert [d["size"] for d in mine(q="track 0")["deposits"]] == ["track"]
    assert len(mine(limit=1)["deposits"]) == 1 and mine(limit=1)["total"] == 3
    other = deposits.find(conn, "b@example.com")["deposits"][0]["id"]
    assert deposits.get(conn, "a@example.com", other) is None and not deposits.delete(conn, "a@example.com", other)


@pytest.mark.parametrize("text, seconds", [("938", 938), ("15:38", 938), ("1:02:03.5", 3723.5), (" 0:07 ", 7)])
def test_parse_time(text, seconds):
    assert deposits.parse_time(text) == seconds


@pytest.mark.parametrize("text", ["", "abc", "1:75", "-3", "1:2:3:4"])
def test_parse_time_refuses(text):
    with pytest.raises(deposits.DepositError):
        deposits.parse_time(text)


def test_api_round_trip(client, db_path):
    (a,) = _tracks(db_path, 1)
    listed = client.get("/api/deposits").json()
    assert listed["total"] == 1 and listed["deposits"][0]["track_id"] == a
    track_deposit = listed["deposits"][0]["id"]
    r = client.post("/api/deposits", json={"size": "collection", "title": "Cercle set",
                                           "source_uri": "https://example.com/watch?v=1"})
    assert r.status_code == 201 and r.json()["status"] == "needs-source" and r.json()["source_kind"] == "link"
    coll = r.json()["id"]
    assert client.post(f"/api/deposits/{coll}/holds", json={"deposit_ids": [track_deposit]}).json()["added"] == 1
    m = client.post("/api/deposits", json={"size": "moment", "note": "the handover", "start_s": 938, "end_s": 990,
                                           "held_by": coll, "touches": [track_deposit], "tags": ["transition"]})
    assert m.status_code == 201 and m.json()["status"] == "ready"
    mid = m.json()["id"]
    got = client.get(f"/api/deposits/{coll}").json()
    assert [h["id"] for h in got["holds"]] == [track_deposit, mid]
    edited = client.patch(f"/api/deposits/{mid}", json={"note": "low-cut sweep over 16 bars"}).json()
    assert edited["note"].startswith("low-cut") and edited["tags"] == [{"tag": "transition", "origin": "user"}]
    assert client.get("/api/deposits", params={"size": "moment", "q": "sweep"}).json()["total"] == 1
    assert client.delete(f"/api/deposits/{track_deposit}").status_code == 409  # it is a track in the library
    assert client.delete(f"/api/deposits/{mid}").status_code == 204
    assert client.delete(f"/api/deposits/{mid}").status_code == 404
    assert client.get(f"/api/deposits/{mid}").status_code == 404
    assert len(client.get("/api/tracks").json()) == 1  # the library is as it was


@pytest.mark.parametrize("body", [
    {"size": "track"},
    {"size": "moment", "start_s": 5, "end_s": 1},
    {"size": "moment", "source_uri": "/etc/passwd"},
    {"size": "moment", "held_by": 4242},
    {"size": "collection", "note": "x" * 4001},
])
def test_api_refuses(client, body):
    assert client.post("/api/deposits", json=body).status_code == 422
    assert client.get("/api/deposits").json()["total"] == 0


def test_cli_add_list_show(db_path, capsys):
    _tracks(db_path, 1)
    assert cli.main(["--db", str(db_path), "deposit", "add", "moment", "--source", "https://example.com/set",
                     "--start", "15:38", "--end", "16:30", "--note", "the handover", "--tag", "transition", "--no-fetch"]) == 0
    added = json.loads(capsys.readouterr().out)
    assert added["start_s"] == 938 and added["end_s"] == 990 and added["status"] == "needs-source"
    assert cli.main(["--db", str(db_path), "deposit", "list"]) == 0
    out = capsys.readouterr().out
    assert "15:38-16:30" in out and "[transition]" in out and "Track 0" in out
    assert cli.main(["--db", str(db_path), "deposit", "show", str(added["id"])]) == 0
    assert json.loads(capsys.readouterr().out)["note"] == "the handover"
    assert cli.main(["--db", str(db_path), "deposit", "show", "999"]) == 1
    assert cli.main(["--db", str(db_path), "deposit", "add", "moment", "--start", "9", "--end", "3"]) == 2


def test_importing_tags_keeps_the_persons_own(db_path):
    conn = db.connect(db_path)
    d = deposits.add(conn, None, "moment", tags=["mine", {"tag": "Old", "origin": "rekordbox"}])
    got = deposits.update(conn, None, d["id"], {"tags": [{"tag": "Dark", "origin": "rekordbox"}]})
    assert got["tags"] == [{"tag": "Dark", "origin": "rekordbox"}, {"tag": "mine", "origin": "user"}]
    assert deposits.update(conn, None, d["id"], {"tags": []})["tags"] == [{"tag": "Dark", "origin": "rekordbox"}]


def test_track_deposit_follows_the_card_title_unless_renamed(db_path):
    a, b = _tracks(db_path, 2)
    conn = db.connect(db_path)
    deposits.sync_tracks(conn)
    by_track = {d["track_id"]: d["id"] for d in deposits.find(conn, None)["deposits"]}
    deposits.update(conn, None, by_track[b], {"title": "My name for it"})
    with conn:
        conn.execute("UPDATE tracks SET title = 'Corrected'")
    deposits.sync_tracks(conn)
    assert deposits.get(conn, None, by_track[a])["title"] == "Corrected"
    assert deposits.get(conn, None, by_track[b])["title"] == "My name for it"
    assert deposits.find(conn, None, q="corrected")["total"] == 2  # the card's title still finds both


def test_set_owner_takes_the_bank_along(db_path):
    (a,) = _tracks(db_path, 1)
    conn = db.connect(db_path)
    deposits.sync_tracks(conn)
    moment = deposits.add(conn, None, "moment", note="mine")
    assert db.set_owner(conn, "a@example.com") == 1
    deposits.sync_tracks(conn, "a@example.com")
    found = deposits.find(conn, "a@example.com")
    assert found["total"] == 2 and {d["track_id"] for d in found["deposits"]} == {a, None}
    assert deposits.get(conn, "a@example.com", moment["id"])["note"] == "mine"
    assert deposits.find(conn, None)["total"] == 0
    # a track whose owner changed some other way still brings its deposit
    with conn:
        conn.execute("UPDATE tracks SET owner = 'b@example.com'")
    deposits.sync_tracks(conn, "b@example.com")
    assert [d["track_id"] for d in deposits.find(conn, "b@example.com")["deposits"]] == [a]
