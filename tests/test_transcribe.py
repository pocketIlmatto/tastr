"""Whisper lyrics (tastr/transcribe.py): grouping heard words into lines, timing known lines, scoring. The model
itself isn't run here; the words are made up, like every lyric in the tests."""

from tastr import transcribe as tr


def _words(text: str, start: float = 10.0, step: float = 0.4, pauses: dict[int, float] | None = None) -> list[dict]:
    out, t = [], start
    for i, w in enumerate(text.split()):
        t += (pauses or {}).get(i, 0.0)
        out.append({"t": round(t, 2), "end": round(t + 0.3, 2), "w": w, "p": 0.9})
        t += step
    return out


def test_lines_split_at_pauses():
    words = _words("paper lanterns drift away over the quiet harbour", pauses={4: 2.0})
    lines = tr.lines_from_words(words)
    assert [x["line"] for x in lines] == ["paper lanterns drift away", "over the quiet harbour"]
    assert lines[0]["t"] == 10.0 and lines[1]["t"] == words[4]["t"]


def test_long_lines_split_at_their_widest_gap():
    words = _words(" ".join(f"w{i}" for i in range(20)), pauses={7: 0.3, 13: 0.5})
    lines = tr.lines_from_words(words)
    assert [x["line"].split()[0] for x in lines] == ["w0", "w7", "w13"]  # widest gap first, then the next


def test_norm_words_ignores_case_and_punctuation():
    assert tr.norm_words("Hey! Don’t stop, (the) Mighty-Moon") == ["hey", "don't", "stop", "the", "mighty", "moon"]


def test_align_lines_times_known_lyrics():
    lines = ["Paper lanterns drift away", "Over the quiet harbour", "Nobody heard this line"]
    heard = _words("paper lanterns drift away", 10) + _words("over the quiet harbor", 20)  # one word misheard
    placed = tr.align_lines(lines, heard)
    assert [p["t"] for p in placed] == [10.0, 20.0, None]


def test_score_counts_word_errors_and_line_timing():
    synced = [{"t": 10.0, "line": "Paper lanterns drift away"}, {"t": 21.0, "line": "Over the quiet harbour"},
              {"t": 30.0, "line": ""}]
    heard = {"words": _words("paper lanterns drift away", 10.2) + _words("over the quiet harbor", 20.0)}
    s = tr.score(heard, None, synced)
    assert s["ref_words"] == 8 and s["heard_words"] == 8
    assert s["wer"] == 0.125
    assert s["timing"] == {"lines": 2, "placed": 2, "median_error": 1.0, "within_1s": 1.0}


def test_score_without_timed_lyrics():
    s = tr.score({"words": _words("la la la")}, "La la la la", None)
    assert s["wer"] == 0.25 and "timing" not in s
