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


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_drawing_the_same_card_again_keeps_a_newer_listen():
    src = (APP / "lyrics.js").read_text()
    src = re.sub(r"^import .*?;\n", "", src, flags=re.M).replace("export ", "")
    body = """
const c = { id: 5, lyrics: { state: "not_found" }, lyrics_available: true, listen_available: true };
cardLyrics(c);
lyrics.set(5, { state: "found", source: "whisper", synced: [{ t: 1, line: "made up" }] }); // the listen finished
cardLyrics(c); const a = lyricsOf(5).state;
cardLyrics({ ...c, lyrics: { state: "not_found" } }); // a fresh answer from the server wins
out = [a, lyricsOf(5).state];"""
    stubs = "const localStorage = { getItem: () => null, setItem() {} };\n"
    r = subprocess.run(["node", "-e", stubs + src + "\nlet out;\n" + body + "\nconsole.log(JSON.stringify(out));"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == ["found", "not_found"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_editor_times_lrc_and_credit():
    """The lyrics editor (your own lyrics): m:ss.0 times, pasted LRC and the credit's label."""
    src = (APP / "lyrics.js").read_text()
    src = re.sub(r"^import .*?;\n", "", src, flags=re.M).replace("export ", "")
    body = """
out = [fmtLT(null), fmtLT(5), fmtLT(62.46), fmtLT(600), parseLT(""), parseLT("1:02.5"), parseLT("62.5"), Number.isNaN(parseLT("1:2:3")),
       Number.isNaN(parseLT("abc")), parseLRC("[ar: Nobody]\\n[00:01.50] Paper boats\\nno stamp\\n[00:04.20][01:04.26]Hum it back\\n"),
       mineLabel({ base: "lrclib" }), mineLabel({ base: "whisper" }), mineLabel({ base: "none" }), mineLabel({})];"""
    stubs = "const localStorage = { getItem: () => null, setItem() {} };\n"
    r = subprocess.run(["node", "-e", stubs + src + "\nlet out;\n" + body + "\nconsole.log(JSON.stringify(out));"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == ["", "0:05.0", "1:02.5", "10:00.0", None, 62.5, 62.5, True, True,
                                    [{"t": 1.5, "line": "Paper boats"}, {"t": 4.2, "line": "Hum it back"}, {"t": 64.3, "line": "Hum it back"}],
                                    "Edited by you", "Edited by you", "Typed by you", "Typed by you"]


def test_editing_is_wired():
    src, css = (APP / "lyrics.js").read_text(), (APP.parent / "style.css").read_text()
    for text in ("/lyrics/edits${path}", '"PUT", ""', '"DELETE", ""', '"POST", "/keep"', "✎ ", "Edit lyrics", "Type lyrics",
                 "Set to now", "Saving puts them in time order.", "No [mm:ss.xx] lines found.", "add times by hand",
                 "Discard your changes? Click again", "Undo my edits", "Yes, undo my edits", "Keep mine", "Yes, show them",
                 "e.ctrlKey || e.metaKey", "L.edited ? mineLabel(L)", "Fix the times marked in red first."):
        assert text in src, text
    for rule in (".ly-lines.ly-edited", ".ly-newer", ".lye-rows", "#card .lye-time", ".lye-short"):
        assert rule in css, rule


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_heard_lyrics_say_their_languages():
    """Whisper's lyrics say which language(s) were heard (shares in the tooltip); LRCLIB's say nothing."""
    src = (APP / "lyrics.js").read_text()
    src = re.sub(r"^import .*?;\n", "", src, flags=re.M).replace("export ", "")
    body = """out = [heardLangs({ source: "whisper", languages: [{ code: "ja", share: 0.58 }, { code: "fr", share: 0.42 }] }),
       heardLangs({ source: "whisper", languages: [{ code: "en", share: 1 }] }),
       heardLangs({ source: "whisper", language: "es" }),
       heardLangs({ source: "whisper", languages: [{ code: "ja" }, { code: "fr" }, { code: "en" }] }),
       heardLangs({ source: "lrclib", timed_by: "whisper", language: "en" }), heardLangs({ source: "whisper" })];"""
    stubs = "const localStorage = { getItem: () => null, setItem() {} };\n"
    r = subprocess.run(["node", "-e", stubs + src + "\nlet out;\n" + body + "\nconsole.log(JSON.stringify(out));"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [
        {"say": "Japanese and French", "tip": "Japanese 58%, French 42% of the words."},
        {"say": "English", "tip": None}, {"say": "Spanish", "tip": None},
        {"say": "Japanese, French and English", "tip": None}, None, None]
    assert "langLabel(L), h(\"b\", {}, \"Heard by Whisper from the vocals." in src
    assert "Heard by Whisper from the vocals${heardLangs(L) ? `, in ${heardLangs(L).say}` : \"\"}" in src
