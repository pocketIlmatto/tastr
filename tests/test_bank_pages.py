"""The bank's own pages (static/bank.html, static/app/bank.js) at /bank and /bank/<id>, beside the app: the list of
deposits and one deposit, drawn from /api/deposits. The summaries are checked in node; the pages were checked in a
browser (every state, both looks, 1280 and 390 px) for the pull request."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tastr.web.server import create_app

STATIC = Path(__file__).resolve().parents[1] / "tastr" / "web" / "static"
BANK = STATIC / "app" / "bank.js"


@pytest.fixture
def client(db_path, tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    return TestClient(create_app(db_path))


def test_the_bank_has_its_own_address(client):
    for path in ("/bank", "/bank/12"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
        assert 'id="bk"' in r.text
        # the module comes from the hashed folder, like the app's, so a deploy is seen at once
        assert re.search(r'src="/static/app-[0-9a-f]{10}/bank\.js"', r.text)
        assert re.search(r'href="/static/style\.css\?v=[0-9a-f]{10}"', r.text)
    assert client.get("/bank/not-a-number").status_code == 404


def test_the_app_is_left_as_it_was():
    html = (STATIC / "index.html").read_text()
    assert 'data-view="bank"' not in html and "/bank" not in html  # no Bank button: the bank is reached at /bank
    # bank.js loads only the shared helpers, so none of the app's views start on the bank's page
    imports = re.findall(r'^import .* from "(.+)";$', BANK.read_text(), re.M)
    assert imports == ["./core.js"]


node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run(body: str):
    script = ("globalThis.document = { title: '', getElementById: () => null };\n"
              f"const B = await import({json.dumps(BANK.as_uri())});\nlet out;\n{body}\nconsole.log(JSON.stringify(out));")
    r = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


MOMENT = {"id": 5, "size": "moment", "title": "Sunset set 15:38-16:30", "note": "the handover", "source_kind": "link",
          "source_uri": "https://www.youtube.com/watch?v=x", "track_id": None, "start_s": 938, "end_s": 990,
          "status": "ready", "tags": [{"tag": "groovy", "origin": "user"}], "context": {}, "touches_count": 2,
          "holds_count": 0, "track": None, "created_at": "2026-11-20T03:00:00+00:00",
          "extracted": {"analysis": {"bpm": 122.0, "camelot": "8A", "key": "A minor", "duration": 52.0, "beats_per_bar": 4,
                                     "tags": [{"tag": "deep house", "score": 0.9}, {"tag": "faint", "score": 0.05}]},
                        "stems": {"names": ["vocals", "bass", "other", "drums"]}}}


@node
def test_a_ready_moment_sums_up_what_was_found():
    out = run(f"const m = {json.dumps(MOMENT)}; out = [B.foundLine(m).t, B.titleOf(m), B.siteOf(m), B.rangeOf(m)];")
    assert out == ["122 BPM · 8A · 26 bars · stems · 2 tracks named · groovy · deep house",
                   "Sunset set", "YouTube", "15:38–16:30"]


@node
def test_each_state_says_where_it_is():
    out = run(f"""
    const m = {json.dumps(MOMENT)};
    const st = (status, extra) => B.foundLine({{ ...m, status, ...extra }});
    out = [st("working", {{ extracted: {{ stage: "analyze" }} }}), st("needs-source", {{}}),
           st("needs-source", {{ source_uri: null, source_kind: "capture" }}),
           st("needs-source", {{ size: "track", source_kind: "track", source_uri: null }}),
           st("failed", {{ extracted: {{ error: "The video is private." }} }})];
    """)
    assert [o["cls"] for o in out] == ["bk-work", "bk-need", "bk-need", "bk-need", "bk-fail"]
    assert out[0]["t"] == "Breaking down: analyzing…"
    assert "has the link" in out[1]["t"] and "knows the track and the spot" in out[2]["t"]
    assert "deleted from your library" in out[3]["t"]
    assert out[4]["t"] == "Couldn't get the audio. The video is private."


@node
def test_tracks_collections_and_captures():
    out = run("""
    const track = { id: 1, size: "track", title: "Rust Shift", source_kind: "track", track_id: 7, status: "ready",
      tags: [], context: {}, extracted: {}, track: { id: 7, title: "Rust Shift", artist: "Night Ferry", bpm: 124.4, camelot: "8A", key: "A minor", duration: 150 } };
    const set = { id: 2, size: "collection", title: "Pier set", source_kind: "link", source_uri: "https://soundcloud.com/a/b",
      status: "ready", tags: [], context: { site: "soundcloud" }, holds_count: 1, extracted: { audio: { duration: 4360 } } };
    const cap = { id: 3, size: "moment", title: "Lumen Park", source_kind: "capture", status: "needs-source", tags: [],
      context: { via: "iPhone · Spotify", artist: "Glass Arcade", at_s: 102 }, extracted: {} };
    out = [B.foundLine(track).t, B.foundLine(set).t, B.siteOf(set), B.rangeOf(cap), B.foundOf(track).bars];
    """)
    assert out == ["124 BPM · 8A · 2:30", "72:40 long · 1 moment cut from it", "SoundCloud", "around 1:42", None]
