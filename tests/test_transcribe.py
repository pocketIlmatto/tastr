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
    assert s["timing"] == {"lines": 2, "placed": 2, "median_error": 1.0, "within_1s": 1.0, "lines_heard": 1.0}


def test_score_without_timed_lyrics():
    s = tr.score({"words": _words("la la la")}, "La la la la", None)
    assert s["wer"] == 0.25 and "timing" not in s


def test_short_runs_join_the_line_before():
    words = _words("paper lanterns drift away", 10) + _words("yeah", 12.2)  # an ad-lib 0.5 s after the line
    assert [x["line"] for x in tr.lines_from_words(words)] == ["paper lanterns drift away yeah"]


def test_lines_split_at_punctuation_and_capitals_without_a_pause():
    text = "Paper lanterns drift away, Over the quiet harbour. Nobody saw them go"
    lines = tr.lines_from_words(_words(text, pauses={4: 0.1}))  # "away," then a short breath
    assert [x["line"] for x in lines] == ["Paper lanterns drift away,", "Over the quiet harbour.",
                                         "Nobody saw them go"]


def test_long_runs_dont_leave_one_word_lines():
    lines = tr.lines_from_words(_words(" ".join(f"w{i}" for i in range(23)), pauses={1: 0.4}))
    assert all(len(x["line"].split()) >= tr.MIN_WORDS for x in lines)


def test_one_word_of_a_line_isnt_enough_to_place_it():
    lines = ["Round and round the silver wheel", "Paper lanterns"]
    heard = _words("round", 5) + _words("paper lanterns", 40)
    placed = tr.align_lines(lines, heard)
    assert placed[0]["t"] is None and placed[1]["t"] == 40.0


def test_lines_heard_only_counts_words_heard_in_their_place():
    timed = [{"t": 10.0, "line": "Round and round the silver wheel"}, {"t": 20.0, "line": "Paper lanterns drift"},
             {"t": 30.0, "line": "Round and round the silver wheel"}]
    heard = _words("round and round the silver wheel", 30)  # only the second chorus
    assert tr.lines_heard(timed, heard) == 0.333


def test_aligned_lines_stay_in_order():
    heard = [{"t": 10.0, "end": 10.05, "w": "paper"}, {"t": 10.1, "end": 10.15, "w": "lanterns"},
             {"t": 10.2, "end": 10.25, "w": "quiet"}, {"t": 10.3, "end": 10.35, "w": "harbour"}]
    placed = tr.align_lines(["paper lanterns", "over the quiet harbour"], heard)
    assert [p["t"] for p in placed] == [10.0, 10.0]


def test_transcribe_returns_plain_python_types(monkeypatch, tmp_path):
    """faster-whisper hands back numpy scalars; the answer is unpickled by Modal's web endpoint, which has no numpy,
    so everything in it must be plain Python."""
    import pickle
    import sys
    import types

    import numpy as np

    class Model:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, *a, **k):
            words = [types.SimpleNamespace(start=np.float32(1.0 + i), end=np.float32(1.4 + i), word=f" w{i}",
                                           probability=np.float32(0.91)) for i in range(4)]
            info = types.SimpleNamespace(language="en", language_probability=np.float32(0.98),
                                         duration=np.float64(12.5))
            return iter([types.SimpleNamespace(words=words)]), info

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))
    out = tr.transcribe(tmp_path / "vocals.flac", device="cpu")

    class NoNumpy(pickle.Unpickler):
        def find_class(self, module, name):
            assert not module.startswith("numpy"), f"{module}.{name} in the answer"
            return super().find_class(module, name)

    import io

    assert NoNumpy(io.BytesIO(pickle.dumps(out))).load() == out
    assert out["words"][0] == {"t": 1.0, "end": 1.4, "w": "w0", "p": 0.91, "seg": 0, "lang": "en"}
    assert out["lines"] and all(type(x["t"]) is float for x in out["lines"])


# Other languages and scripts. The lines are made up, like the English ones above.

def _pieces(pieces: list[str], start: float, step: float = 0.3) -> list[dict]:
    return [{"t": round(start + step * i, 2), "end": round(start + step * i + 0.25, 2), "w": w}
            for i, w in enumerate(pieces)]


def test_norm_words_reads_every_script():
    assert tr.norm_words("Ночь, улица — ФОНАРЬ!") == ["ночь", "улица", "фонарь"]
    assert tr.norm_words("Καλημέρα κόσμε") == ["καλημερα", "κοσμε"]  # accents go, as with é
    assert tr.norm_words("Café déjà-vu") == ["cafe", "deja", "vu"]
    assert tr.norm_words("שָׁלוֹם עוֹלָם") == tr.norm_words("שלום עולם") == ["שלום", "עולם"]  # vowel points go
    assert tr.norm_words("مَرْحَبًا يا عالم؟") == tr.norm_words("مرحبا يا عالم") == ["مرحبا", "يا", "عالم"]
    assert len(tr.norm_words("नमस्ते दुनिया")) == 2  # Devanagari's vowel signs don't split a word
    assert tr.norm_words("夜空に、星！") == ["夜", "空", "に", "星"]  # no spaces: one character a word
    assert tr.norm_words("君と dance して") == ["君", "と", "dance", "し", "て"]
    assert tr.norm_words("ＡＢＣ　１２") == ["abc", "12"]  # full-width letters are the usual ones


def test_align_lines_times_cyrillic_lyrics():
    lines = ["Бумажные фонари уплывают", "Над тихой гаванью", "Эту строку никто не слышал"]
    heard = _words("бумажные фонари уплывают", 10) + _words("над тихой гаванью", 20)
    assert [p["t"] for p in tr.align_lines(lines, heard)] == [10.0, 20.0, None]


def test_align_lines_times_japanese_lyrics_from_whispers_pieces():
    # Whisper splits Japanese into a character or two a "word", with no spaces.
    lines = ["紙の灯籠が流れていく", "静かな港の上を"]
    heard = _pieces(["紙の", "灯", "籠が", "流", "れて", "いく"], 10) + _pieces(["静", "かな", "港の", "上を"], 20)
    assert [p["t"] for p in tr.align_lines(lines, heard)] == [10.0, 20.0]


def test_score_compares_chinese_by_character():
    synced = [{"t": 10.0, "line": "纸灯笼漂走了"}]
    heard = {"words": _pieces(["纸灯", "笼", "飘走", "了"], 10)}
    s = tr.score(heard, None, synced)
    assert s["ref_words"] == 6 and s["heard_words"] == 6 and s["wer"] == 0.167  # one character misheard
    assert s["timing"]["placed"] == 1


def test_lines_join_cjk_without_spaces_and_split_at_full_width_punctuation():
    pieces = ["夜", "空", "に", "星", "が", "光", "る。", "君", "と", "dance", "し", "た", "い"]
    words = [{"t": 10.0 + 0.25 * i, "end": 10.2 + 0.25 * i, "w": w} for i, w in enumerate(pieces)]
    assert [x["line"] for x in tr.lines_from_words(words)] == ["夜空に星が光る。", "君と dance したい"]


def test_short_cjk_lines_are_not_split_like_ten_english_words():
    # Twelve characters is a normal sung line, not one to split.
    words = [{"t": 10.0 + 0.25 * i, "end": 10.2 + 0.25 * i, "w": w} for i, w in enumerate("静かな港の上を流れていく")]
    assert [x["line"] for x in tr.lines_from_words(words)] == ["静かな港の上を流れていく"]


def test_languages_of_lists_what_was_heard_most_first():
    words = [{"w": "x", "lang": "es"}] * 30 + [{"w": "x", "lang": "en"}] * 10 + [{"w": "x", "lang": "pt"}]
    words += [{"w": "x"}] * 5
    assert tr.languages_of(words) == [{"code": "es", "share": 0.732}, {"code": "en", "share": 0.244}]


def test_language_spans_follow_confident_windows_only():
    found = [("en", 0.5), ("es", 0.9), ("es", 0.95), (None, 0.0), ("fr", 0.4), ("en", 0.92), ("en", 0.85),
             ("es", 0.9)]
    # Unsure windows follow the nearest sure one before them (the first: after it).
    assert tr.language_spans(found) == [(0.0, 150.0, "es"), (150.0, 210.0, "en"), (210.0, None, "es")]
    assert tr.language_spans([("ja", 0.99)]) == [(0.0, None, "ja")]
    assert tr.language_spans([("fr", 0.55), ("en", 0.3), ("fr", 0.5)]) == [(0.0, None, "fr")]
    assert tr.language_spans([(None, 0.0), ("en", 0.3)]) == [(0.0, None, None)]  # Whisper decides


def test_made_up_subtitle_lines_are_recognised():
    for line in ["Sous-titrage Société Radio-Canada", "Субтитры создавал DimaTorzok", "Altyazı M.K.",
                 "ترجمة نانسي قنقر", "ご視聴ありがとうございました", "Thanks for watching!"]:
        assert tr.made_up(line), line
    assert not tr.made_up("Paper lanterns drift away over the quiet harbour")
    assert not tr.made_up("thank you for watching the lanterns drift away over the quiet harbour every night")


def test_transcribe_sets_the_language_of_each_stretch(monkeypatch, tmp_path):
    """A song that moves from Spanish to English: each stretch is transcribed with its own language set, so the
    English isn't written down in Spanish, and made-up subtitle lines are dropped."""
    import sys
    import types

    calls = []

    class Model:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, src, **k):
            calls.append((len(src), k["language"]))
            text = {"es": ["hola", "luna"], "en": ["hello", "moon"], "de": []}[k["language"]]
            words = [types.SimpleNamespace(start=1.0 + i, end=1.5 + i, word=f" {w}", probability=0.9)
                     for i, w in enumerate(text)]
            junk = [types.SimpleNamespace(start=9.0, end=9.5, word=" Subtitles by someone", probability=0.5)]
            info = types.SimpleNamespace(language=k["language"], language_probability=0.97, duration=30.0)
            return iter([types.SimpleNamespace(words=words), types.SimpleNamespace(words=junk)]), info

        def detect_language(self, audio, vad_filter=False):
            return {0: ("es", 0.9, []), 1: ("en", 0.95, []), 2: ("de", 0.7, [])}[audio[0]]

    def decode_audio(path):
        return [0] * (60 * 16000) + [1] * (30 * 16000) + [2] * (30 * 16000)

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model,
                                                                            decode_audio=decode_audio))
    out = tr.transcribe(tmp_path / "vocals.flac", device="cpu")
    # The German stretch heard nothing, so it was tried again in the English before it.
    assert calls == [(60 * 16000, "es"), (30 * 16000, "en"), (30 * 16000, "de"), (30 * 16000, "en")]
    assert [(w["w"], w["t"], w["lang"]) for w in out["words"]] == [
        ("hola", 1.0, "es"), ("luna", 2.0, "es"), ("hello", 61.0, "en"), ("moon", 62.0, "en"),
        ("hello", 91.0, "en"), ("moon", 92.0, "en")]
    assert out["languages"] == [{"code": "en", "share": 0.667}, {"code": "es", "share": 0.333}]
    assert out["language"] == "en" and out["audio_seconds"] == 120.0
    assert [x["lang"] for x in out["stretches"]] == ["es", "en", "en"]
    assert out["windows"][3] == {"t": 90.0, "lang": "de", "p": 0.7}
    assert out["dropped"] == [{"t": 9.0, "words": 1}, {"t": 69.0, "words": 1}, {"t": 99.0, "words": 1}]  # no text

    calls.clear()
    out = tr.transcribe(tmp_path / "vocals.flac", device="cpu", language="en")  # told the language: no detection
    assert [c[1] for c in calls] == ["en"] and out["languages"] == [{"code": "en", "share": 1.0}]
