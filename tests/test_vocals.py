"""The Vocals filter's one answer per track (tastr/vocals.py): the vocal stem's share where stems recorded it, else
Essentia's voice score; the API rows and cards that carry it; and the page's chip and filter (static/app), in node."""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tastr import db, stems, vocals
from tastr.web.server import create_app
from test_foundation import full_card
from ui_src import app_js

SR = 1000
CORE = Path(__file__).resolve().parents[1] / "tastr" / "web" / "static" / "app" / "core.js"


def tone(seconds: float, amp: float) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return amp * np.sin(2 * np.pi * 50 * t)


def test_share_counts_windows_where_the_vocal_stem_is_heard():
    music = tone(10, 0.3)
    voice = np.concatenate([tone(4, 0.3), np.zeros(6 * SR)])  # sung in the first 4 of 10 s
    assert vocals.vocal_share(voice, music + voice, SR) == 0.4


def test_bleed_far_under_the_mix_and_a_quiet_master_are_handled():
    music = tone(10, 0.5)
    assert vocals.vocal_share(tone(10, 0.5 * 10 ** (-30 / 20)), music, SR) == 0.0  # Demucs bleed, 30 dB under
    quiet = 10 ** (-35 / 20)  # a quiet master: the vocals are as loud as the music, both far under full scale
    assert vocals.vocal_share(tone(10, quiet), tone(10, quiet) * 2, SR) == 1.0


def test_silent_windows_are_skipped_and_all_silence_has_no_share():
    voice = np.concatenate([tone(2, 0.3), np.zeros(8 * SR)])
    assert vocals.vocal_share(voice, voice, SR) == 1.0  # 8 s of silence don't count either way
    assert vocals.vocal_share(np.zeros(5 * SR), np.zeros(5 * SR), SR) is None
    assert vocals.vocal_share(np.zeros(10), np.zeros(10), SR) is None  # shorter than a window
    stereo = np.stack([tone(4, 0.3), tone(4, 0.3)])
    assert vocals.vocal_share(stereo, stereo, SR) == 1.0


@pytest.mark.parametrize("voice,share,want", [
    (0.9, None, ("yes", "ml")), (0.7, None, ("yes", "ml")), (0.5, None, ("unsure", "ml")), (0.3, None, ("no", "ml")),
    (None, None, ("unsure", None)), (0.9, 0.01, ("no", "stems")), (0.1, 0.4, ("yes", "stems")),
    (0.9, 0.05, ("unsure", "stems")), (None, 0.10, ("yes", "stems")), (None, 0.02, ("no", "stems"))])
def test_the_vocal_stem_decides_when_it_was_measured_else_the_voice_score(voice, share, want):
    got = vocals.verdict(voice, share)
    assert (got["is"], got["by"]) == want


def test_voice_score_comes_from_the_stored_head():
    assert vocals.voice_score({"heads": {"voice_instrumental": {"voice": 0.4567, "instrumental": 0.54}}}) == 0.457
    assert vocals.voice_score({"heads": {}}) is None and vocals.voice_score(None) is None


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_track_rows_and_cards_carry_one_answer(db_path, home, tmp_path):
    conn = db.connect(db_path)
    ids = []
    for i, voice in enumerate((0.9, 0.5, None, 0.95)):
        card = full_card()
        card.source.sha256 = f"{i:x}" * 64
        card.source.uri = f"/song{i}.wav"
        tid = db.save_card(conn, card)
        ids.append(tid)
        if voice is not None:
            db.save_ml_tags(conn, tid, {"version": 1, "heads": {"voice_instrumental": {"voice": voice}}, "tags": []})
    conn.close()
    folder = stems.stems_dir("3" * 64, ids[3])  # the fourth has stems, almost no vocals heard in them
    folder.mkdir(parents=True)
    for n in stems.STEM_NAMES:
        stems.stem_path(folder, n).write_bytes(b"fLaC")
    stems.info_path(folder).write_text(json.dumps({"model": stems.MODEL, "vocal_share": 0.01}))

    client = TestClient(create_app(db_path))
    rows = {r["id"]: r["voc"] for r in client.get("/api/tracks").json()}
    assert [rows[i]["is"] for i in ids] == ["yes", "unsure", "unsure", "no"]
    assert [rows[i]["by"] for i in ids] == ["ml", "ml", None, "stems"]
    assert rows[ids[1]]["score"] == 0.5 and rows[ids[3]]["share"] == 0.01
    assert client.get(f"/api/tracks/{ids[3]}").json()["voc"] == rows[ids[3]]
    assert client.get(f"/api/tracks/{ids[0]}").json()["voc"] == rows[ids[0]]


node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run(body: str):
    script = (f"globalThis.document = {{ title: \"Tastr\", getElementById: () => null, addEventListener() {{}} }};\n"
              f"const C = await import({json.dumps(CORE.as_uri())});\nlet out;\n{body}\nconsole.log(JSON.stringify(out));")
    r = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@node
def test_the_chip_follows_the_answer_and_not_sure_gets_none():
    ml = {"tags": [{"tag": "vocal", "group": "voice", "score": 0.75}]}
    out = run(f"""
    const ml = {json.dumps(ml)};
    const chips = (voc) => C.vibeTags({{ tags: [], ml_tags: ml, voc }}).map((t) => [t.tag, t.src]);
    out = {{ ml: chips({{ is: "yes", by: "ml", score: 0.75 }}), stem: chips({{ is: "no", by: "stems", share: 0.01 }}),
             unsure: chips({{ is: "unsure", by: "ml", score: 0.5 }}), none: chips(undefined),
             row: C.rowVibes({{ tags: [], ml_tags: ["vocal"], voc: {{ is: "no", by: "stems", share: 0 }} }}),
             why: C.vocWhy({{ is: "yes", by: "stems", share: 0.38 }}), line: C.vocUnsureText({{ is: "unsure", by: "ml", score: 0.47 }}),
             stemLine: C.vocUnsureText({{ is: "unsure", by: "stems", share: 0.06 }}), yet: C.vocUnsureText(null),
             is: [C.vocIs({{ is: "no" }}), C.vocIs(undefined)] }};
    """)
    assert out["ml"] == [["vocal", "ml"]] and out["stem"] == [["instrumental", "stems"]]  # the stem overrules ML's "vocal"
    assert out["unsure"] == [] and out["none"] == [] and out["row"] == ["instrumental"]
    assert out["why"] == "Decided by the vocal stem: vocals are heard in 38% of the track."
    assert out["line"] == "Vocals: not sure (voice score 0.47)."
    assert out["stemLine"] == "Vocals: not sure (the vocal stem is heard in only 6% of the track)."
    assert out["yet"] == "Vocals: not sure yet." and out["is"] == ["no", "unsure"]


def test_the_filter_is_wired_in_library_and_files():
    js = app_js()
    html = (CORE.parents[1] / "index.html").read_text()
    for el in ("lib-voc", "fl-voc"):
        assert f'id="{el}"' in html and f'$("{el}")' in js
    assert 'value="unsure">Not sure' in html
    assert 'voc: "" })' in js and "f.voc)" in js and "vocIs(t.voc) === f.voc" in js
    assert "Show tracks like this" in js and 'vocFilter("unsure")' in js
    # the answer follows a first separation (running -> ready) and ML tags arriving on an open card
    assert "if (was && stemSet(st) && stemSet(st) !== stemSet(was))" in js
    assert "renderList(); vocChanged(c.id); }" in js
