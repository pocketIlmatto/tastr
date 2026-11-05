"""The page's ML tags (static/app/core.js, find.js), run in node: one list of vibes per card and per track-list row,
with the engine's weak vibes swapped out once a track has ML tags, and the Find vibe search matching the same tracks."""

import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from tastr import db
from ui_src import app_js

CORE = Path(__file__).resolve().parents[1] / "tastr" / "web" / "static" / "app" / "core.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

ENGINE = [{"tag": "dark", "score": 0.8}, {"tag": "driving", "score": 0.6}, {"tag": "chill", "score": 0.3},
          {"tag": "house tempo", "score": 0.5}]
ML = {"tags": [{"tag": "Deep House", "group": "style", "score": 0.31}, {"tag": "energetic", "group": "mood", "score": 0.2},
               {"tag": "dark", "group": "mood", "score": 0.15}, {"tag": "instrumental", "group": "voice", "score": 0.9}]}


def run(body: str):
    script = (f"globalThis.document = {{ title: \"Tastr\", getElementById: () => null, addEventListener() {{}} }};\n"
              f"const C = await import({json.dumps(CORE.as_uri())});\nlet out;\n{body}\nconsole.log(JSON.stringify(out));")
    r = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_done_ml_tags_replace_the_weak_engine_vibes_and_keep_dark():
    out = run(f"""
    const c = {{ tags: {json.dumps(ENGINE)}, ml_tags: {json.dumps(ML)}, ml_tags_state: "none" }};
    const v = C.vibeTags(c);
    out = {{ tags: v.map((t) => [t.tag, t.src]), state: C.mlState(c),
             groups: Object.fromEntries(v.map((t) => [t.tag, C.groupOf(t.tag).key])) }};
    """)
    assert out["state"] == "done"  # stored tags count even when the server forgot the run (a restart)
    assert out["tags"] == [["dark", "engine"], ["house tempo", "engine"], ["Deep House", "ml"], ["energetic", "ml"],
                           ["instrumental", "ml"]]  # driving and chill gone; the engine's dark stays, once
    assert out["groups"] == {"dark": "mood", "house tempo": "tempo", "Deep House": "style", "energetic": "mood",
                             "instrumental": "sound"}


@pytest.mark.parametrize("state,available,want", [
    ("queued", True, "queued"), ("running", True, "running"), ("error", True, "error"),
    ("none", True, "queued"), ("none", False, None), (None, False, None)])
def test_without_ml_tags_the_engine_vibes_stay_as_they_are(state, available, want):
    out = run(f"""
    const c = {{ tags: {json.dumps(ENGINE)}, ml_tags: null, ml_tags_state: {json.dumps(state)}, ml_tags_available: {json.dumps(available)} }};
    out = {{ tags: C.vibeTags(c).map((t) => t.tag), state: C.mlState(c) }};
    """)
    assert out == {"tags": [t["tag"] for t in ENGINE], "state": want}


def test_track_list_rows_use_the_same_list():
    out = run("""
    out = { tagged: C.rowVibes({ tags: ["dark", "driving", "bright"], ml_tags: ["Tech House", "happy", "dark", "vocal"] }),
            untagged: C.rowVibes({ tags: ["dark", "driving"], ml_tags: null }), old: C.rowVibes({ tags: ["chill"] }),
            style: C.groupOf("Tech House").key, mood: C.groupOf("happy").key };
    """)
    assert out["tagged"] == ["dark", "Tech House", "happy", "vocal"]
    assert out["untagged"] == ["dark", "driving"] and out["old"] == ["chill"]
    assert out["style"] == "style" and out["mood"] == "mood"


def test_find_vibe_search_matches_what_the_card_shows(tmp_path):
    """The SQL Find builds for a vibe: ML tags count, and a replaced engine vibe only for tracks without ML tags."""
    src = re.sub(r"^export ", "", app_js(), flags=re.M)
    fn = re.search(r"^const qVibeIds = .*?;$", src, re.M | re.S).group(0)
    script = ("const ML_REPLACES = { driving: 1, euphoric: 1, hypnotic: 1, chill: 1, bright: 1 };\n"
              "const qEsc = (v) => String(v).replace(/'/g, \"''\");\n" + fn +
              "\nconsole.log(JSON.stringify(Object.fromEntries(['driving', 'energetic', 'dark', 'Deep House'].map((t) => [t, qVibeIds(t)]))));")
    r = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    sql = json.loads(r.stdout)

    path = tmp_path / "t.db"
    db.connect(path).close()  # the real schema
    conn = sqlite3.connect(path)
    for tid in (1, 2, 3):
        conn.execute("INSERT INTO tracks (id, source_kind, source_uri, analyzed_at) VALUES (?, 'file', ?, '')", (tid, f"/{tid}.wav"))
    conn.executemany("INSERT INTO tags VALUES (?, ?, ?)", [(1, "driving", 0.5), (1, "dark", 0.7), (2, "driving", 0.5), (3, "dark", 0.4)])
    conn.execute("INSERT INTO ml_tags VALUES (1, 1, 'Deep House,energetic', '', '{}')")
    conn.execute("INSERT INTO ml_tags VALUES (3, 1, 'dark,Deep House', '', '{}')")
    ids = {t: sorted(r[0] for r in conn.execute(q)) for t, q in sql.items()}
    assert ids == {"driving": [2], "energetic": [1], "dark": [1, 3], "Deep House": [1, 3]}
