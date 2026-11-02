"""Lists and playlists without the page (static/app/playlist-core.js, TAS-79), run in node: list names that fit in 80
characters, the Blend track picker's matches, and the player's queue."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1] / "tastr" / "web" / "static" / "app" / "playlist-core.js"
LISTS = CORE.with_name("lists.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run(body: str):
    script = f"import * as P from {json.dumps(CORE.as_uri())};\nlet out;\n{body}\nconsole.log(JSON.stringify(out));"
    r = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_fit_name_drops_brackets_first_then_cuts_at_a_word():
    out = run("""
    const long = "Path: Strobe (Extended Club Mix) [Remastered 2024] to Some Other Very Long Title (Original Mix)";
    const words = "Between the warmest of warm-up sets and the very peak of a long night out dancing until morning comes";
    out = { short: P.fitName("  Warm   up "), br: P.fitName(long, 60), cut: P.fitName(words), tiny: P.fitName(words, 10),
            nospace: P.fitName("x".repeat(100)), max: P.LIST_NAME_MAX };
    """)
    assert out["short"] == "Warm up" and out["max"] == 80
    assert out["br"] == "Path: Strobe to Some Other Very Long Title (Original Mix)"  # dropped only as many brackets as needed
    assert len(out["cut"]) <= 80 and out["cut"].endswith("…") and out["cut"][:-1] in "Between the warmest of warm-up sets and the very peak of a long night out dancing"
    assert not out["cut"][:-1].endswith(" ")
    assert len(out["tiny"]) <= 10 and len(out["nospace"]) == 80


def test_save_list_never_sends_a_name_over_80_characters():
    # lists.js imports core.js (the page's helpers); stub fetch and run saveList as the page would
    script = f"""
    const sent = [];
    globalThis.document = {{ getElementById: () => null }};
    globalThis.fetch = async (url, o) => {{ if (o && o.body) sent.push(JSON.parse(o.body)); const body = url === "/api/lists" && !o ? [] : {{ created: true, name: "x", track_ids: [1] }};
      return {{ ok: true, status: 200, headers: {{ get: () => "application/json" }}, json: async () => body, text: async () => JSON.stringify(body) }}; }};
    const L = await import({json.dumps(LISTS.as_uri())});
    await L.saveList("A".repeat(50) + " (Extended Mix) to " + "B".repeat(60), [1]);
    console.log(JSON.stringify(sent));
    """
    r = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    sent = json.loads(r.stdout)
    assert len(sent[0]["name"]) <= 80 and sent[0]["track_ids"] == [1]


def test_combo_filters_by_title_or_artist_and_lists_everything_when_empty():
    out = run("""
    const L = Array.from({ length: 70 }, (_, i) => ({ id: i + 1, title: `Song ${String(i).padStart(2, "0")}`, artist: i === 5 ? "Moby" : "X" }));
    const all = P.comboHits(L, "", 3), moby = P.comboHits(L, "mOBy", 3), none = P.comboHits(L, "zzz", 3);
    out = { n: all.shown.length, more: all.more, at: all.at, first: all.shown[0].title, moby: moby.hits.map((t) => t.id), mat: moby.at,
            none: [none.hits.length, none.at], name: P.comboName(L[5]) };
    """)
    assert out["n"] == 60 and out["more"] == 10 and out["first"] == "Song 00"
    assert out["at"] == 2  # the picked track is highlighted
    assert out["moby"] == [6] and out["mat"] == 0 and out["none"] == [0, -1]
    assert out["name"] == "Song 05 — Moby"


def test_queue_plays_in_order_and_ends_after_the_last():
    out = run("""
    const q = P.newQueue("L", [10, 20, 30]), seen = [P.queueId(q)];
    let n; while ((n = P.nextStep(q)) != null) { P.markPlayed(q); q.i = n; seen.push(P.queueId(q)); }
    out = { seen, prevLate: P.prevStep(q, 5), prevEarly: P.prevStep(q, 1), first: P.prevStep({ i: 0 }, 1) };
    """)
    assert out["seen"] == [10, 20, 30]
    assert out["prevLate"] == "restart" and out["prevEarly"] == 1 and out["first"] == "restart"


def test_shuffle_keeps_the_played_tracks_and_off_goes_back_to_list_order():
    out = run("""
    let seed = 1; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
    const q = P.newQueue("L", [1, 2, 3, 4, 5, 6]);
    P.markPlayed(q); q.i = 1; P.markPlayed(q); q.i = 2;  // played 1 and 2, now on 3
    P.toggleShuffle(q, rnd);
    const on = { order: q.order.map((k) => q.ids[k]), cur: P.queueId(q), i: q.i };
    P.toggleShuffle(q, rnd);
    const off = { order: q.order.map((k) => q.ids[k]), cur: P.queueId(q) };
    const s = P.newQueue("L", [1, 2, 3, 4], { shuffle: true, rnd });
    out = { on, off, s: { i: s.i, shuffle: s.shuffle, n: new Set(s.order).size } };
    """)
    assert out["on"]["order"][:2] == [1, 2] and out["on"]["cur"] == 3 and out["on"]["i"] == 2
    assert sorted(out["on"]["order"]) == [1, 2, 3, 4, 5, 6]
    assert out["off"]["order"] == [1, 2, 3, 4, 5, 6] and out["off"]["cur"] == 3
    assert out["s"] == {"i": 0, "shuffle": True, "n": 4}  # Shuffle starts at a random track
