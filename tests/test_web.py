"""Tests for the web API and static UI."""

import shutil
import sqlite3

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


@pytest.mark.rust
def test_upload_analyzes_and_lists(client, synth_wav):
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


def test_query_refuses_attach_pragma_and_writes(client, seeded, tmp_path):
    other = tmp_path / "other.db"
    for sql in [f"ATTACH '{other}' AS o", "PRAGMA journal_mode=delete", "CREATE INDEX i ON tracks(title)",
                "SELECT * FROM tracks; DELETE FROM tracks"]:
        r = client.post("/api/query", json={"sql": sql})
        assert r.status_code == 400, sql
    assert not other.exists()  # ATTACH would have created it, even on a read-only connection
    rec = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 5) SELECT count(*) FROM n"
    assert client.post("/api/query", json={"sql": rec}).json()["rows"] == [[5]]


def test_runaway_query_is_stopped(db_path, seeded):
    endless = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n"
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        db.run_query(db_path, endless, 10, seconds=0.2)


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


def test_index_versions_its_assets_so_a_deploy_skips_stale_caches(client):
    """Cloudflare and browsers cache /static/ files; a content hash in the URL makes each deploy a new URL."""
    import hashlib
    import re

    from tastr.web.server import STATIC_DIR

    r = client.get("/")
    assert r.headers["cache-control"] == "no-cache"
    for name in ("app.js", "strudel.js", "style.css"):
        v = re.search(rf'"/static/{re.escape(name)}\?v=(\w+)"', r.text).group(1)
        assert v == hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:10]
        assert client.get(f"/static/{name}?v={v}").status_code == 200


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
                   "Strong", "beat-group", "STYLE_INFO", "sec-link", "Export MIDI ▾", "exp-part", "Reset edits", "Reset this section", "Reset whole track", "Outline Δ from the previous section",
                   "dragNumber(bpmIn)", "smBtns(", "laneCtl", "partOn(", "audible(lane)", "sw playing"):
        assert needle in js
    # Template patterns live in the Pattern picker only: no separate grids or reveal button.
    # Part chips and "Copy as text" gave way to solo/mute and the Export MIDI picker.
    for gone in ("ideasShown", "Suggest pattern ideas", "drumGrid(", "midiBytes(", "startPlayer(", "Copy as text", "part-chip"):
        assert gone not in js


@pytest.mark.rust
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

    root = Path(__file__).parents[1]
    src = (root / "tastr" / "web" / "static" / "app.js").read_text()
    groups = src[src.index("const VIBE_GROUPS"):src.index("const groupOf")]
    mapped = set(re.findall(r'"([^"]+)"', " ".join(re.findall(r"tags: \[([^\]]*)\]", groups))))
    has_tempo_group = "/tempo$/" in groups

    rules = (root / "engine-rs" / "src" / "stages" / "tags.rs").read_text()
    rules = rules[:rules.index("#[cfg(test)]")]
    engine = set(re.findall(r'add\("([^"]+)"', rules)) | set(re.findall(r'\("([^"]+)", [\d.]+, [\d.]+\)', rules))
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


@pytest.mark.rust
def test_reanalyze_endpoint(client, db_path, synth_wav):
    path, _ = synth_wav
    conn = db.connect(db_path)
    card = full_card()  # a stale card: made-up analysis, no views, pointing at a real file
    card.source = Source(kind="file", uri=str(path), local_path=str(path), title="old title", sha256="stale")
    card.extras.pop("views", None)
    tid = db.save_card(conn, card)
    gone = db.save_card(conn, full_card())  # points at /x.wav, which isn't there
    conn.close()

    client.put(f"/api/tracks/{tid}/edits", json={"drums": {"0": {"kick": "x..............."}, "all": {"kick": "x.x............."}}})
    r = client.post(f"/api/tracks/{tid}/reanalyze").json()
    body = r["card"]
    # the stale card's sections had no bars, so the section edit can't be matched; "all" stays
    assert r["edits_dropped"] == 1
    assert client.get(f"/api/tracks/{tid}/edits").json()["drums"] == {"all": {"kick": "x.x............."}}
    assert body["id"] == tid and body["has_views"]
    assert body["source"]["title"] == "synth_124_Am"  # file tags re-read too
    assert body["core"]["bpm"] == pytest.approx(124, abs=2)
    assert [t["id"] for t in client.get("/api/tracks").json()].count(tid) == 1  # replaced in place, not added

    r = client.post(f"/api/tracks/{gone}/reanalyze")
    assert r.status_code == 410 and "no longer on disk" in r.json()["detail"]
    assert client.post("/api/tracks/999/reanalyze").status_code == 404


@pytest.mark.rust
def test_reanalyze_with_a_time_signature(client, db_path, synth_wav):
    path, _ = synth_wav
    conn = db.connect(db_path)
    card = full_card()
    card.source = Source(kind="file", uri=str(path), local_path=str(path), title="t", sha256="stale")
    tid = db.save_card(conn, card)
    conn.close()

    client.put(f"/api/tracks/{tid}/edits", json={"drums": {"all": {"kick": "x.x............."}}})
    r = client.post(f"/api/tracks/{tid}/reanalyze?meter=3/4").json()
    core = r["card"]["core"]
    assert core["beats_per_bar"] == 3 and core["meter_set"]
    assert r["edits_dropped"] == 1  # 16-step edits don't fit 12-step bars
    assert client.get(f"/api/tracks/{tid}/edits").json()["drums"] == {}
    groove = next(s for s in r["card"]["suggestions"] if s["title"] == "Detected groove")
    assert all(len(p) == 12 for p in groove["data"]["pattern"].values())
    # A plain re-analyze keeps the meter the user picked; "auto" goes back to detecting it.
    assert client.post(f"/api/tracks/{tid}/reanalyze").json()["card"]["core"]["beats_per_bar"] == 3
    core = client.post(f"/api/tracks/{tid}/reanalyze?meter=auto").json()["card"]["core"]
    assert core["beats_per_bar"] == 4 and not core["meter_set"]
    assert client.post(f"/api/tracks/{tid}/reanalyze?meter=5/4").status_code == 400


def test_ui_saves_edits_and_versions():
    from tastr.web.server import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text()
    for needle in ("/edits`", "edit-versions", "Versions ▾", "Discard all edits (back to detected)", "Undo", "sec-tab", '"edited"',
                   "edit-pill", "Revert", "Your edit", "flushEdits", "edits_dropped", "Some edits no longer match the new sections",
                   'new CustomEvent("edits")', "Patterns:", "Detected", "Your edits ("):
        assert needle in js, needle
    css = (STATIC_DIR / "style.css").read_text()
    assert "--edit: #a07c00" in css and "--edit: #f2cf4a" in css and ".sec-tab.edited::after" in css
    html = (STATIC_DIR / "index.html").read_text()
    assert "Save your edits as a version" in html and "Close the Versions popover" in html


def test_ui_has_808_and_technics_looks():
    from tastr.web.server import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text()
    assert 'localStorage.getItem("tastr-look")' in html and 'dataset.look = "808"' in html
    assert 'data-look="808" aria-pressed="true"' in html and 'data-look="technics"' in html
    css = (STATIC_DIR / "style.css").read_text()
    assert ':root[data-look="808"]' in css and ':root[data-look="technics"]' in css
    assert css.count("--edit: #1f6fd1") == 2  # blue edits inside both looks: 808 uses yellow for beats
    js = (STATIC_DIR / "app.js").read_text()
    assert 'const LOOKS = ["808", "technics"]' in js and 'localStorage.setItem("tastr-look"' in js


def test_ui_has_track_actions_menu():
    from tastr.web.server import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text()
    assert "Track actions" in js and "/retag" in js and "/reanalyze" in js and "Copy track summary" in js
    assert "confirm(" not in js  # deleting confirms inside the menu, not with a browser dialog


@pytest.mark.rust
def test_audio_streams_with_range_and_views_load_separately(client, synth_wav):
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


EDITS = {"drums": {"1": {"kick": "x...x...x...x..."}}, "bass": {"0": {"notes": ["A", None, "C", "G"], "rhythm": "x.......x......."}},
         "chords": {}, "drumsrc": {"1": "0"}}


def test_edits_autosave_round_trip(client, seeded):
    assert client.get(f"/api/tracks/{seeded}/edits").json() == {}
    assert client.put(f"/api/tracks/{seeded}/edits", json=EDITS).status_code == 204
    assert client.get(f"/api/tracks/{seeded}/edits").json() == EDITS
    changed = {**EDITS, "chords": {"1": {"chords": ["Am", "F", "C", "G"]}}}
    client.put(f"/api/tracks/{seeded}/edits", json=changed)
    assert client.get(f"/api/tracks/{seeded}/edits").json() == changed
    client.put(f"/api/tracks/{seeded}/edits", json={"drums": {}, "bass": {}})  # nothing edited: cleared
    assert client.get(f"/api/tracks/{seeded}/edits").json() == {}


def test_edit_versions_create_list_get_delete(client, seeded):
    base = f"/api/tracks/{seeded}/edit-versions"
    assert client.get(base).json() == []
    r = client.post(base, json={"name": "Darker drop", "edits": EDITS})
    assert r.status_code == 201
    first = r.json()
    assert first["name"] == "Darker drop" and first["sections"] == 2 and first["edits"] == EDITS
    second = client.post(base, json={"name": "Version 2", "edits": {"drums": {"0": {"kick": "x..............."}}}}).json()
    listed = client.get(base).json()
    assert [v["name"] for v in listed] == ["Version 2", "Darker drop"]  # newest first
    assert set(listed[0]) == {"id", "name", "created_at", "sections"}  # no edits in the list
    assert client.get(f"{base}/{first['id']}").json()["edits"] == EDITS
    assert client.get(f"/api/tracks/{seeded}/edits").json() == {}  # versions are not the current edits
    assert client.delete(f"{base}/{first['id']}").status_code == 204
    assert [v["id"] for v in client.get(base).json()] == [second["id"]]
    assert client.get(f"{base}/{first['id']}").status_code == 404
    assert client.delete(f"{base}/{first['id']}").status_code == 404
    assert client.post(base, json={"name": "", "edits": {}}).status_code == 422


def test_deleting_a_track_deletes_its_edits(client, seeded, db_path):
    client.put(f"/api/tracks/{seeded}/edits", json=EDITS)
    client.post(f"/api/tracks/{seeded}/edit-versions", json={"name": "v1", "edits": EDITS})
    assert client.delete(f"/api/tracks/{seeded}").status_code == 204
    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM track_edits").fetchone()[0] == 0
    conn.close()


def test_edits_of_unknown_track_are_404(client):
    assert client.get("/api/tracks/999/edits").status_code == 404
    assert client.put("/api/tracks/999/edits", json=EDITS).status_code == 404
    assert client.get("/api/tracks/999/edit-versions").status_code == 404
    assert client.post("/api/tracks/999/edit-versions", json={"name": "v", "edits": EDITS}).status_code == 404
    assert client.get("/api/tracks/999/edit-versions/1").status_code == 404


def test_reanalysis_moves_edits_to_the_section_with_the_same_bars(db_path):
    conn = db.connect(db_path)
    tid = db.save_card(conn, full_card())
    edits = {"drums": {"0": "intro", "1": "drop", "all": "x"}, "drumsrc": {"1": "2"}, "bass": {"2": "gone"}}
    db.set_edits(conn, tid, edits)
    db.add_edit_version(conn, tid, "v1", {"chords": {"1": "drop chords"}})
    old = {0: (1, 8), 1: (9, 24), 2: (25, 32)}
    new = {0: (1, 4), 1: (5, 8), 2: (9, 24)}  # the intro split in two; the drop kept its bars; bars 25-32 are gone
    assert db.remap_edits(conn, tid, old, new) == 2  # the intro drums and the bass in bars 25-32
    assert db.get_edits(conn, tid) == {"drums": {"2": "drop", "all": "x"}, "drumsrc": {"2": "2"}, "bass": {}}
    vid = db.list_edit_versions(conn, tid)[0]["id"]
    assert db.get_edit_version(conn, tid, vid)["edits"] == {"chords": {"2": "drop chords"}}
    conn.close()


def test_bench_scores_are_served(client, monkeypatch, tmp_path):
    from tastr.engine.bench import scores

    data = client.get("/api/bench").json()
    for key in ("giantsteps-key", "beatport-key", "beatport-tempo", "giantsteps-tempo", "downbeat"):
        r = data["scores"][key]
        assert 0 < r["exact"] <= 1 and r["tracks"] > 0 and r["run"]
    # A missing file serves no scores, so the Glossary keeps the numbers in its HTML.
    monkeypatch.setattr(scores, "SCORES_PATH", tmp_path / "missing.json")
    assert client.get("/api/bench").json() == {"scores": {}}


def test_pattern_ideas_come_from_the_engine(client):
    from tastr.engine.patterns import VARIATIONS

    pats = client.get("/api/patterns").json()
    assert len(pats) == sum(len(v) for v in VARIATIONS.values())
    assert {"style": "four on the floor", "name": "Classic house/techno", "pattern": VARIATIONS["four_floor"][0][1]} in pats


def test_ui_has_the_glossary():
    from pathlib import Path

    static = Path(__file__).parents[1] / "tastr/web/static"
    html, js = (static / "index.html").read_text(), (static / "app.js").read_text()
    assert 'data-view="glossary"' in html and 'id="view-glossary"' in html
    for key in ("giantsteps-key", "beatport-key", "beatport-tempo", "giantsteps-tempo", "downbeat"):
        assert f'data-bench="{key}"' in html
    for needle in ("openGlossary", "camelotWheel", "glPlay", "/api/bench", "/api/patterns", '"What the vibes mean"'):
        assert needle in js


@pytest.mark.parametrize("headers,status", [
    ({}, 404),  # curl / scripts: no browser headers
    ({"Sec-Fetch-Site": "same-origin"}, 404),
    ({"Sec-Fetch-Site": "cross-site"}, 403),
    ({"Sec-Fetch-Site": "same-site"}, 403),
    ({"Origin": "http://testserver"}, 404),
    ({"Origin": "https://evil.example"}, 403),
    ({"Origin": "null"}, 403),
])
def test_cross_site_writes_are_refused(client, headers, status):
    assert client.delete("/api/tracks/999", headers=headers).status_code == status
    assert client.get("/api/tracks", headers=headers).status_code == 200  # reads are unaffected


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_ui_roman_numerals_match_the_engine_for_in_key_chords():
    """romanOf in app.js counts from the key's own scale, like music.roman_numeral: in A minor, F is VI."""
    import json
    import re
    import subprocess

    from tastr.engine import music
    from tastr.web.server import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text()
    defs = [re.search(rf"^const {n} = .*?;$", js, re.M | re.S).group(0) for n in ("NOTES", "ROMAN", "pcOf", "parseChord", "romanOf")]
    cases = []
    for mode, scale in (("major", "Major"), ("minor", "Minor")):
        for tonic in (0, 9, 4):
            for deg, iv in enumerate(music.MODES[scale]):
                root = (tonic + iv) % 12
                quality = "min" if (mode, deg) in {("major", 1), ("major", 2), ("major", 5), ("minor", 0), ("minor", 3), ("minor", 4)} else "maj"
                if (mode, deg) in {("major", 6), ("minor", 1)}:
                    continue  # diminished: the chord picker leaves it out
                name = music.NOTE_NAMES[root] + ("m" if quality == "min" else "")
                cases.append([name, tonic, mode, music.roman_numeral(root, quality, tonic, scale)])
    script = "\n".join(defs) + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(([n, t, m]) => romanOf(n, t, m))));"
    got = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert got == [c[3] for c in cases]
    assert got[[c[:3] for c in cases].index(["F", 9, "minor"])] == "VI"


def test_saved_searches_save_replace_and_delete(client):
    put = lambda **b: client.put("/api/searches", json=b)
    a = put(mode="simple", name="Warm-up", data={"groups": [], "join": "and", "sort": ["bpm", "asc"]}).json()
    put(mode="sql", name="warm-up", data={"sql": "SELECT 1"})  # same name, other mode: a separate search
    b = put(mode="simple", name="WARM-UP", data={"groups": [], "join": "or", "sort": ["energy", "desc"]}).json()
    assert b["id"] == a["id"] and b["name"] == "WARM-UP"  # same name, any case, replaces it in place
    got = client.get("/api/searches").json()
    assert [(s["mode"], s["name"]) for s in got] == [("simple", "WARM-UP"), ("sql", "warm-up")]
    assert got[0]["data"]["join"] == "or"
    assert put(mode="other", name="x", data={}).status_code == 422
    assert put(mode="sql", name="  ", data={"sql": "x"}).status_code == 422
    assert client.delete(f"/api/searches/{a['id']}").status_code == 204
    assert client.delete(f"/api/searches/{a['id']}").status_code == 404
    assert [s["mode"] for s in client.get("/api/searches").json()] == ["sql"]


def wait_job(client, job_id, until=("done", "error"), headers=None):
    import time
    for _ in range(200):
        job = client.get(f"/api/analyze/jobs/{job_id}", headers=headers).json()
        if job["state"] in until:
            return job
        time.sleep(0.02)
    raise AssertionError(job)


def test_background_analysis_reports_each_step(client, monkeypatch):
    """?background=1 answers with a job at once; polling it shows the step, then the results."""
    import threading

    from tastr import service

    go, seen = threading.Event(), []

    def fake(source, options, on_stage=None, signals=None):
        for name in ("decode", "core", "sections"):
            on_stage(name, 0, 1)
            seen.append(name)
            if name == "core":
                assert go.wait(5)
        card = full_card()
        card.source = source
        return card

    monkeypatch.setattr(service, "_analyze", fake)
    monkeypatch.setenv("TASTR_LOOKUP", "")
    r = client.post("/api/analyze?background=1", files=[("files", ("song.wav", b"RIFF", "audio/wav"))])
    assert r.status_code == 202 and r.json()["file_count"] == 1
    job = wait_job(client, r.json()["id"], until=("running",))
    while job["stage"] != "core":
        job = wait_job(client, job["id"], until=("running",))
    assert job["file_index"] == 0 and "results" not in job
    go.set()
    job = wait_job(client, job["id"])
    assert job["state"] == "done" and job["results"][0]["track_id"] and job["results"][0]["error"] is None
    assert seen == ["decode", "core", "sections"]
    assert client.get("/api/analyze/jobs/nosuchjob").status_code == 404


def test_background_analysis_names_the_step_it_failed_at(client, monkeypatch):
    r = client.post("/api/analyze?background=1", files=[("files", ("broken.wav", b"not audio at all", "audio/wav"))])
    job = wait_job(client, r.json()["id"])
    res = job["results"][0]
    assert job["state"] == "done" and res["track_id"] is None and res["error"] and res["stage"] == "decode"


def test_unfinished_jobs_read_as_failed_after_a_restart(db_path, tmp_path, monkeypatch):
    import json

    from tastr.web.analysis_jobs import RESTARTED

    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    folder = tmp_path / "home" / "analysis-jobs"
    folder.mkdir(parents=True)
    (folder / "abc123.json").write_text(json.dumps({"id": "abc123", "owner": None, "state": "running", "stage": "core"}))
    with TestClient(create_app(db_path)) as c:  # `with` runs the startup, which marks it failed
        job = c.get("/api/analyze/jobs/abc123").json()
    assert job["state"] == "error" and job["error"] == RESTARTED


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_ui_card_filters_vibes_keys_and_bpm():
    """The card's vibe chips narrow the track list (AND), its key shows compatible keys (wrapping 12<->1), its BPM toggles ±5."""
    import json
    import re
    import subprocess

    from tastr.web.server import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text()
    grab = lambda n: re.search(rf"^(const {n} = .*?;|function {n}\(.*?\n}})$", js, re.M | re.S).group(0)
    defs = "\n".join(grab(n) for n in ("isNum", "LIB_CLEAR", "lib", "DAY", "upMatch", "keyRank", "SORTS", "visibleTracks", "camelotNear", "keyMatch", "vibeFilter", "keyFilter", "bpmFilter"))
    script = defs + """
const labels = new Map([[4, {}]]), reviewRows = [{ track_id: 2 }];
const now = Date.now();
let tracks = [
  { id: 1, title: "a", tags: ["dark", "driving"], camelot: "12A", bpm: 124, uploaded: now - 40 * DAY },
  { id: 2, title: "b", tags: ["dark"], camelot: "1B", bpm: 128.4, uploaded: now - 2 * DAY },
  { id: 3, title: "c", tags: ["driving"], camelot: "2A", bpm: 140, uploaded: now - 3 * DAY },
  { id: 4, title: "d", tags: [], camelot: "5A", bpm: 118, uploaded: now },
];
const showFiltered = () => {};
const ids = () => visibleTracks().map((t) => t.id).sort();
const out = { near1A: camelotNear("1A"), near12B: camelotNear("12B") };
vibeFilter("dark"); out.dark = ids();
vibeFilter("driving"); out.darkDriving = ids();
vibeFilter("dark"); out.driving = ids(); vibeFilter("driving");
keyFilter("1A"); out.key1A = ids(); out.keyState = lib.key;
keyFilter("1A"); out.keyOff = lib.key;
bpmFilter(123.6); out.bpm = [lib.bmin, lib.bmax, ids()];
bpmFilter(123.6); out.bpmOff = [lib.bmin, lib.bmax];
const order = () => visibleTracks().map((t) => t.id);
out.newest = order(); lib.sort = "old"; out.oldest = order(); lib.sort = "new";
lib.up = "7"; out.week = ids(); lib.up = "old"; out.older = ids(); lib.up = "";
lib.lab = "need"; out.need = ids(); lib.lab = "yes"; out.labeled = ids(); lib.lab = "no"; out.unlabeled = ids(); lib.lab = "";
console.log(JSON.stringify(out));
"""
    got = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert got["near1A"] == ["1A", "12A", "2A", "1B"]
    assert got["near12B"] == ["12B", "11B", "1B", "12A"]
    assert got["dark"] == [1, 2] and got["darkDriving"] == [1] and got["driving"] == [1, 3]
    assert got["key1A"] == [1, 2, 3] and got["keyState"] == "1A~" and got["keyOff"] == ""
    assert got["bpm"] == ["119", "129", [1, 2]] and got["bpmOff"] == ["", ""]
    assert got["newest"] == [4, 2, 3, 1] and got["oldest"] == [1, 3, 2, 4]  # by upload time
    assert got["week"] == [2, 3, 4] and got["older"] == [1]
    assert got["need"] == [2] and got["labeled"] == [4] and got["unlabeled"] == [1, 2, 3]
