"""Lyrics and karaoke in the page (app/lyrics.js): the wiring into the card, the chart's player and the playlist bar,
and the line that lights up. The lyrics here are made up."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "tastr" / "web" / "static" / "app"


def test_wired_into_the_card_the_chart_and_the_playlist():
    card, chart, play, pl = [(APP / f).read_text() for f in ("card.js", "chart.js", "chart-play.js", "playlist.js")]
    assert 'section("lyrics", "Lyrics", [lyricsBody(c)])' in card and '"energy", "lyrics", "vibe"' in card
    assert "karaCtl(c.card.id" in chart and "isPlaying: () => c.play.isPlaying()" in chart
    assert "karaBuf(c) || c.audio.buf" in play
    assert "setListClock(" in pl and "Karaoke: no stems for this track, so it plays the full mix" in pl


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_current_line_and_the_states():
    src = (APP / "lyrics.js").read_text()
    src = re.sub(r"^import .*?;\n", "", src, flags=re.M).replace("export ", "")
    body = """
const L = { state: "found", synced: [{ t: 2, line: "Paper lanterns" }, { t: 6.5, line: "counting lights" }, { t: 30, line: "late line" }] };
out = [lineAt(L, 0), lineAt(L, 1.9), lineAt(L, 2), lineAt(L, 7), lineAt(L, 99), synced(L), hasLyrics(L),
       hasLyrics({ state: "found", instrumental: true, plain: "x" }), hasLyrics({ state: "found", synced: [], plain: "only words" }),
       synced({ state: "found", synced: [], plain: "only words" }), hasLyrics({ state: "not_found" })];"""
    stubs = "const localStorage = { getItem: () => null, setItem() {} };\n"
    r = subprocess.run(["node", "-e", stubs + src + "\nlet out;\n" + body + "\nconsole.log(JSON.stringify(out));"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [-1, 0, 0, 1, 2, True, True, False, True, False, False]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_playing_card_wins_over_a_paused_playlist_on_the_same_track():
    src = (APP / "lyrics.js").read_text()
    src = re.sub(r"^import .*?;\n", "", src, flags=re.M).replace("export ", "")
    stubs = """const localStorage = { getItem: () => null, setItem() {} };
let cardPlaying = false; const calls = [];
const charts = new Map([[7, { el: { isConnected: true }, now: () => 42, isPlaying: () => cardPlaying, toggle: () => calls.push("card"), seek: () => calls.push("card-seek") }]]);
"""
    body = """setListClock({ id: () => 7, now: () => 10, playing: () => false, seek: () => calls.push("pl-seek"), toggle: () => calls.push("pl") });
const a = lyrClock(7); lyrToggle(7);
cardPlaying = true; const b = lyrClock(7); lyrToggle(7); lyrSeek(7, 5);
out = [a.src, a.t, b.src, b.t, calls];"""
    r = subprocess.run(["node", "-e", stubs + src + "\nlet out;\n" + body + "\nconsole.log(JSON.stringify(out));"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == ["pl", 10, "card", 42, ["pl", "card", "card-seek"]]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_guessed_lines_never_light_up():
    """Lyrics timed by listening (tastr/listen.py): a line it couldn't place has `guess: true` and is skipped."""
    src = (APP / "lyrics.js").read_text()
    src = re.sub(r"^import .*?;\n", "", src, flags=re.M).replace("export ", "")
    body = """
const L = { state: "found", timed_by: "whisper", synced: [{ t: 2, line: "Paper lanterns" }, { t: 6, line: "guessed", guess: true },
            { t: 10, line: "counting lights" }] };
out = [lineAt(L, 5), lineAt(L, 7), lineAt(L, 11), heard(L), heard({ source: "whisper" })];"""
    stubs = "const localStorage = { getItem: () => null, setItem() {} };\n"
    r = subprocess.run(["node", "-e", stubs + src + "\nlet out;\n" + body + "\nconsole.log(JSON.stringify(out));"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [0, 0, 2, False, True]


def test_listening_is_wired():
    src = (APP / "lyrics.js").read_text()
    assert "/lyrics/listen`, { method: \"POST\" }" in src
    for text in ("Listen for lyrics", "Time these lines", "Waiting for another listen to finish…",
                 "Heard by Whisper from the vocals. Words may be wrong.", "Look on LRCLIB again",
                 "Timed by listening to the vocals.", "Lyrics from LRCLIB · timed by listening",
                 "Couldn't listen to this track. "):
        assert text in src, text
