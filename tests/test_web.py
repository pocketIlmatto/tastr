"""Tests for the web API and static UI."""

import pytest
from fastapi.testclient import TestClient

from tastr import db
from tastr.engine.card import Source, Tag, TrackCard
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


def test_track_exposes_metadata_and_album(client, db_path):
    from tastr.filemeta import describe  # noqa: F401

    card = full_card()
    card.source.album = "Night Shift EP"
    card.source.file_tags = {"title": "x", "album": "Night Shift EP", "year": "2024"}
    card.source.audio_info = {"format": "FLAC", "bitrate": 980000, "sample_rate": 44100, "channels": 2, "file_size": 5242880}
    conn = db.connect(db_path)
    tid = db.save_card(conn, card)
    assert conn.execute("SELECT album FROM tracks WHERE id = ?", (tid,)).fetchone()[0] == "Night Shift EP"
    conn.close()
    body = client.get(f"/api/tracks/{tid}").json()
    assert body["source"]["album"] == "Night Shift EP"
    meta = body["metadata"]
    assert meta["File tags"] == {"Title": "x", "Album": "Night Shift EP", "Year": "2024"}
    assert meta["Audio"]["Bitrate"] == "980 kbps" and meta["Audio"]["File size"] == "5.0 MB"
    assert meta["Analysis"]["Analyzed at"]


def test_ui_has_collapsible_sections():
    from pathlib import Path

    js = (Path(__file__).parents[1] / "tastr/web/static/app.js").read_text()
    for key in ("meta", "energy", "vibe", "groove", "try"):
        assert f'section("{key}"' in js


def test_ui_has_one_drums_bass_and_chords_section():
    from pathlib import Path

    js = (Path(__file__).parents[1] / "tastr/web/static/app.js").read_text()
    for needle in ('origin === "template"', '"Drums, bass & chords"', "grooveView", "Pattern ideas (by tempo)", "Use in every section",
                   "drumsrc", "partEdits", "by_section", "bassRoll", "miniKeys", "noteOptions", "midiMulti", "startLoop",
                   "Strong", "beat-group", "STYLE_INFO", "sec-link", "Export MIDI", "Copy as text", "Reset edits", "Outline what changed"):
        assert needle in js
    # Template patterns live in the Pattern picker only: no separate grids or reveal button.
    for gone in ("ideasShown", "Suggest pattern ideas", "drumGrid(", "midiBytes(", "startPlayer("):
        assert gone not in js


def test_upload_keeps_the_file_name_without_a_hash(client, synth_wav):
    path, _ = synth_wav
    with open(path, "rb") as f:
        res = client.post("/api/analyze", files=[("files", (path.name, f, "audio/wav"))]).json()["results"][0]
    assert res["title"] == path.stem
    assert client.get("/api/tracks").json()[0]["title"] == path.stem
    card = client.get(f"/api/tracks/{res['track_id']}").json()
    assert card["source"]["title"] == path.stem
    assert card["metadata"]["Source"]["File"] == path.name


def test_tracks_uploaded_before_the_move_display_without_the_hash(client, db_path, tmp_path):
    card = full_card()
    card.source.uri = str(tmp_path / "uploads" / "d4defa41_Night Shift.wav")
    card.source.title = "d4defa41_Night Shift"
    conn = db.connect(db_path)
    tid = db.save_card(conn, card)
    conn.close()
    assert client.get("/api/tracks").json()[0]["title"] == "Night Shift"
    body = client.get(f"/api/tracks/{tid}").json()
    assert body["source"]["title"] == "Night Shift"
    assert body["metadata"]["Source"]["File"] == "Night Shift.wav"
    # a name that merely looks hashed, outside the uploads folder, is left alone
    card2 = full_card()
    card2.source.uri, card2.source.title = "/music/deadbeef_remix.wav", "deadbeef_remix"
    card2.source.sha256 = "other"
    conn = db.connect(db_path)
    db.save_card(conn, card2)
    conn.close()
    assert "deadbeef_remix" in [t["title"] for t in client.get("/api/tracks").json()]
def test_every_vibe_the_engine_can_produce_has_a_ui_group():
    """Fails when a vibe is added to the engine but not to VIBE_GROUPS in app.js."""
    import re
    from pathlib import Path

    from tastr.engine.stages import tags

    src = (Path(tags.__file__).parents[2] / "web" / "static" / "app.js").read_text()
    groups = src[src.index("const VIBE_GROUPS"):src.index("const groupOf")]
    mapped = set(re.findall(r'"([^"]+)"', " ".join(re.findall(r"tags: \[([^\]]*)\]", groups))))
    has_tempo_group = "/tempo$/" in groups

    engine = set(re.findall(r'add\("([^"]+)"', Path(tags.__file__).read_text()))
    engine |= {name for name, _, _ in tags.TEMPO_STYLES}
    missing = {t for t in engine if t not in mapped and not (has_tempo_group and t.endswith("tempo"))}
    assert not missing, f"vibes with no group in VIBE_GROUPS: {sorted(missing)}"
    assert len(engine) >= 14


def test_ui_walks_dropped_folders():
    from pathlib import Path

    static = Path(__file__).parents[1] / "tastr" / "web" / "static"
    js = (static / "app.js").read_text()
    assert "webkitGetAsEntry" in js and "createReader" in js
    assert "webkitdirectory" in (static / "index.html").read_text()
def test_track_list_has_artist_and_vibes_for_the_library_filters(client, db_path):
    card = full_card()
    card.source.artist = "Okra Lane"
    card.tags = [Tag("driving", 0.5), Tag("dark", 0.8)]
    conn = db.connect(db_path)
    db.save_card(conn, card)
    conn.close()
    row = client.get("/api/tracks").json()[0]
    assert row["artist"] == "Okra Lane"
    assert row["tags"] == ["dark", "driving"]  # strongest first


def test_retag_endpoint(client, db_path, synth_wav):
    path, _ = synth_wav
    conn = db.connect(db_path)
    card = full_card()
    card.source = Source(kind="file", uri=str(path), local_path=str(path), title="old title", sha256="retag")
    tid = db.save_card(conn, card)
    gone = db.save_card(conn, full_card())  # points at /x.wav, which isn't there
    link = full_card()
    link.source = Source(kind="url", uri="https://example.com/t", title="link", sha256="link")
    lid = db.save_card(conn, link)
    conn.close()

    body = client.post(f"/api/tracks/{tid}/retag").json()
    assert body["changed"] and body["card"]["source"]["title"] == "synth_124_Am"  # no title tag: falls back to the file name
    assert body["card"]["metadata"]["Audio"]
    assert not body["tags_found"]
    assert not client.post(f"/api/tracks/{tid}/retag").json()["changed"]
    assert {t["id"]: t["title"] for t in client.get("/api/tracks").json()}[tid] == "synth_124_Am"

    r = client.post(f"/api/tracks/{gone}/retag")
    assert r.status_code == 410 and "no longer on disk" in r.json()["detail"]
    assert client.post(f"/api/tracks/{lid}/retag").status_code == 400
    assert client.post("/api/tracks/999/retag").status_code == 404


def test_reanalyze_endpoint(client, db_path, synth_wav):
    path, _ = synth_wav
    conn = db.connect(db_path)
    card = full_card()  # a stale card: made-up analysis, no views, pointing at a real file
    card.source = Source(kind="file", uri=str(path), local_path=str(path), title="old title", sha256="stale")
    card.extras.pop("views", None)
    tid = db.save_card(conn, card)
    gone = db.save_card(conn, full_card())  # points at /x.wav, which isn't there
    conn.close()

    body = client.post(f"/api/tracks/{tid}/reanalyze").json()["card"]
    assert body["id"] == tid and body["has_views"]
    assert body["source"]["title"] == "synth_124_Am"  # file tags re-read too
    assert body["core"]["bpm"] == pytest.approx(124, abs=2)
    assert [t["id"] for t in client.get("/api/tracks").json()].count(tid) == 1  # replaced in place, not added

    r = client.post(f"/api/tracks/{gone}/reanalyze")
    assert r.status_code == 410 and "no longer on disk" in r.json()["detail"]
    assert client.post("/api/tracks/999/reanalyze").status_code == 404


def test_ui_has_track_actions_menu():
    from tastr.web.server import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text()
    assert "Track actions" in js and "/retag" in js and "/reanalyze" in js and "Copy track summary" in js
    assert "confirm(" not in js  # deleting confirms inside the menu, not with a browser dialog


def test_audio_streams_with_range_and_views_load_separately(client, synth_wav, truth_core):
    path, _ = synth_wav
    with open(path, "rb") as f:
        tid = client.post("/api/analyze", files=[("files", (path.name, f, "audio/wav"))]).json()["results"][0]["track_id"]
    body = client.get(f"/api/tracks/{tid}").json()
    assert body["has_audio"] and body["has_views"] and "views" not in body["extras"]
    full = client.get(f"/api/tracks/{tid}/audio")
    assert full.status_code == 200 and full.headers["content-type"] == "audio/wav"
    assert full.content == path.read_bytes()
    part = client.get(f"/api/tracks/{tid}/audio", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    views = client.get(f"/api/tracks/{tid}/views").json()
    assert set(views) == {"waveform", "spectrogram"} and views["spectrogram"]["png"].startswith("data:image/png;base64,")


def test_tracks_without_audio_or_views_say_so(client, seeded):
    body = client.get(f"/api/tracks/{seeded}").json()
    assert body["has_audio"] is False and body["has_views"] is False
    assert client.get(f"/api/tracks/{seeded}/audio").status_code == 404
    assert client.get(f"/api/tracks/{seeded}/views").status_code == 404
    assert client.get("/api/tracks/999/audio").status_code == 404


def test_ui_has_audio_views_and_transport():
    from pathlib import Path

    js = (Path(__file__).parents[1] / "tastr/web/static/app.js").read_text()
    for needle in ("/audio`", "/views`", "decodeAudioData", '"Waveform"', '"Spectrogram"', "chartMode", "scrubStart", "spectrogramLevels",
                   "downbeat_times", 'aria-label": "Seek"', "Preparing audio preview", "player = me"):
        assert needle in js


def test_audio_allows_strudel_cc_and_no_other_origin(client, db_path, tmp_path):
    """The Strudel sketch loads the original audio from strudel.cc, so that origin (only) gets CORS."""
    audio = tmp_path / "x.wav"
    audio.write_bytes(b"RIFF" + bytes(100))
    card = full_card()
    card.source.local_path = str(audio)
    conn = db.connect(db_path)
    tid = db.save_card(conn, card)
    conn.close()
    url = f"/api/tracks/{tid}/audio"

    ok = client.get(url, headers={"Origin": "https://strudel.cc"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "https://strudel.cc"
    ranged = client.get(url, headers={"Origin": "https://strudel.cc", "Range": "bytes=0-9"})
    assert ranged.status_code == 206 and ranged.headers["access-control-allow-origin"] == "https://strudel.cc"
    pre = client.options(url, headers={"Origin": "https://strudel.cc", "Access-Control-Request-Method": "GET",
                                       "Access-Control-Request-Private-Network": "true"})
    assert pre.status_code == 204
    assert pre.headers["access-control-allow-origin"] == "https://strudel.cc"
    assert pre.headers["access-control-allow-private-network"] == "true"

    for origin in ("https://evil.example", "https://strudel.cc.evil.example", None):
        headers = {"Origin": origin} if origin else {}
        res = client.get(url, headers=headers)
        assert res.status_code == 200 and "access-control-allow-origin" not in res.headers
        assert client.options(url, headers=headers).status_code == 403
    # other endpoints stay same-origin only
    assert "access-control-allow-origin" not in client.get(f"/api/tracks/{tid}", headers={"Origin": "https://strudel.cc"}).headers


def test_serve_log_level_shows_tastr_debug_lines(capsys):
    import logging

    from tastr.web.server import configure_logging

    try:
        configure_logging("debug")
        logging.getLogger("tastr.engine.pipeline").debug("stage traceback here")
        configure_logging("info")
        logging.getLogger("tastr.engine.pipeline").debug("hidden at info")
        logging.getLogger("tastr.service").warning("failed to analyze x")
    finally:
        logging.getLogger("tastr").handlers[:] = []
        logging.getLogger("tastr").propagate = True
        logging.getLogger("tastr").setLevel(logging.NOTSET)
    err = capsys.readouterr().err
    assert "DEBUG:    tastr.engine.pipeline: stage traceback here" in err
    assert "hidden at info" not in err and "WARNING:  tastr.service: failed to analyze x" in err
