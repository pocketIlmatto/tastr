"""Lyrics from the audio itself: Whisper speech recognition on a track's vocal stem (tastr/listen.py uses it).

For tracks LRCLIB doesn't have, or has without times. `transcribe` runs faster-whisper (MIT) with OpenAI's
Whisper large-v3-turbo weights (MIT) and returns timed lines in the same shape as tastr/lyrics.py's `synced`.
`align_lines` times lyrics we already have (LRCLIB's plain text) from the words Whisper heard, and `score` measures
a transcription against LRCLIB's lyrics: word error rate, and how close each timed line starts.

Sung words are much harder than speech (held vowels, effects, backing vocals), which is why this listens to the
vocal stem and not the full mix. It runs on a GPU on Modal (`transcribe` in tastr/cloud/modal_app.py); the
`transcribe_files` entry point there tries it on a few files and writes what it heard to a local folder. Whatever
it hears is the song's words, so the output stays private like LRCLIB's, and the tests use made-up lines.
"""

from __future__ import annotations

import re
import time
import unicodedata
from pathlib import Path
from typing import Any

MODEL = "large-v3-turbo"
LINE_GAP = 0.6  # seconds of silence between two words that start a new line
LINE_WORDS = 10  # a longer line is split where a new line most likely starts
MIN_WORDS = 3  # a split never leaves fewer words than this on either side
SPLIT_AT = 0.6  # a line of 2 * MIN_WORDS words or more is split where a break scores this much (see _break)
CJK_WORD = 0.6  # how many words a character of a script without spaces (Chinese, Japanese, Thai...) counts as
WINDOW = 30.0  # seconds Whisper reads at a time; the language is detected again in each one
MIN_LANGUAGE = 0.5  # a window's language counts only when Whisper is at least this sure of it

# Scripts written without spaces between words: their characters are compared one by one, and joined without spaces.
_NO_SPACES = re.compile("[\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff\u2e80-\u2fdf\u3000-\u303f\u3040-\u30ff"
                        "\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef\U00020000-\U0003ffff]")
_END = (".", "?", "!", "。", "？", "！", "؟", "…", "।")  # ends a sentence, in the scripts Whisper writes
_PAUSE = (",", ";", ":", "、", "，", "；", "：", "،", "؛")


def transcribe(path: str | Path, model: str = MODEL, language: str | None = None, device: str = "auto") -> dict:
    """{"language", "language_probability", "languages", "model", "lines": [{t, end, line}],
    "words": [{t, end, w, p, seg, lang?}], "audio_seconds", "seconds"}. Voice activity detection skips the silent
    stretches of a vocal stem, where Whisper otherwise makes things up; not conditioning on the previous text stops it
    repeating a line forever.

    Without a `language`, Whisper detects it again in every 30 s window (`multilingual`), so a song that changes
    language keeps each part's own words instead of having them translated or spelled out in the first one's.
    `languages` lists what was heard, the most words first ({code, share}); `language` is the first of them."""
    from faster_whisper import WhisperModel

    start = time.monotonic()
    cuda = device == "cuda" or (device == "auto" and _has_cuda())
    m = WhisperModel(model, device="cuda" if cuda else "cpu", compute_type="float16" if cuda else "int8")
    loaded = time.monotonic()
    segments, info = m.transcribe(str(path), language=language, word_timestamps=True, vad_filter=True,
                                  condition_on_previous_text=False, beam_size=5, multilingual=language is None)
    # faster-whisper's times and probabilities are numpy scalars; plain floats only, because the answer is pickled
    # back to Modal's web endpoint, whose image has no numpy (it failed there with a DeserializationError).
    words = [{"t": _num(w.start, 2), "end": _num(w.end, 2), "w": w.word.strip(), "p": _num(w.probability, 3),
              "seg": k} for k, s in enumerate(segments) for w in (s.words or []) if w.word.strip()]
    if language is None:
        _tag_languages(m, path, words)
    languages = languages_of(words) or [{"code": str(language or info.language), "share": 1.0}]
    first = languages[0]["code"]
    return {"language": first,
            "language_probability": _num(info.language_probability, 3) if first == info.language else None,
            "languages": languages, "model": model, "device": "cuda" if cuda else "cpu",
            "lines": lines_from_words(words), "words": words, "audio_seconds": _num(info.duration, 1),
            "load_seconds": round(loaded - start, 1), "seconds": round(time.monotonic() - loaded, 1)}


def _tag_languages(m: Any, path: str | Path, words: list[dict]) -> None:
    """Mark each heard word with the language Whisper detects in its 30 s window (`lang`), where it is sure enough.
    faster-whisper decodes each window in the language it detects but doesn't say which; this asks again, one
    encoder pass a window with words in it (a fraction of a second on the GPU)."""
    try:
        from faster_whisper import decode_audio

        audio = decode_audio(str(path))
        rate = 16000
        for k in sorted({int(w["t"] // WINDOW) for w in words}):
            clip = audio[int(k * WINDOW * rate):int((k + 1) * WINDOW * rate)]
            code, prob, _ = m.detect_language(audio=clip, vad_filter=True)
            if float(prob) >= MIN_LANGUAGE:
                for w in words:
                    if int(w["t"] // WINDOW) == k:
                        w["lang"] = str(code)
    except Exception:  # noqa: BLE001 - the words matter, their language is a label: one language it is
        for w in words:  # all or nothing
            w.pop("lang", None)


def languages_of(words: list[dict]) -> list[dict]:
    """[{code, share}] of the words tagged with a language, the most words first; a language under 5% of them (a
    window of ad-libs detected as something else) is left out."""
    counts: dict[str, int] = {}
    for w in words:
        if w.get("lang"):
            counts[w["lang"]] = counts.get(w["lang"], 0) + 1
    total = sum(counts.values())
    return [{"code": c, "share": round(n / total, 3)} for c, n in sorted(counts.items(), key=lambda x: -x[1])
            if n / total >= 0.05]


def _num(x: Any, digits: int) -> float:
    """A plain Python float, rounded; Whisper hands back numpy scalars (see `transcribe`)."""
    return round(float(x), digits)


def _has_cuda() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001 - no CUDA build or driver: the CPU it is
        return False


def lines_from_words(words: list[dict]) -> list[dict]:
    """Group heard words into lines: a new line after a pause of LINE_GAP; within a run of words, where a new line
    most likely starts (`_break`: a short pause, punctuation, a capital, a new Whisper segment), when the run is
    longer than LINE_WORDS or the break is clear. A run of one or two words close after a line joins it, so a held
    word or an ad-lib doesn't make a line of its own. Whisper's "words" in Chinese, Japanese or Thai are a character
    or two, so they count as CJK_WORD words a character, and are joined without spaces."""
    runs: list[list[dict]] = []
    for w in words:
        if runs and w["t"] - runs[-1][-1]["end"] < LINE_GAP:
            runs[-1].append(w)
        else:
            runs.append([w])
    merged: list[list[dict]] = []
    for r in runs:
        if merged and _size(r) < MIN_WORDS and r[0]["t"] - merged[-1][-1]["end"] < 2 * LINE_GAP \
                and _size(merged[-1]) + _size(r) <= LINE_WORDS:
            merged[-1] += r
        else:
            merged.append(r)
    out = [line for r in merged for line in _split(r)]
    return [{"t": g[0]["t"], "end": g[-1]["end"], "line": join_words([w["w"] for w in g])} for g in out]


def _word_size(w: str) -> float:
    n = len(_NO_SPACES.findall(w))
    return CJK_WORD * n if n else 1.0


def _size(g: list[dict]) -> float:
    return sum(_word_size(w["w"]) for w in g)


def join_words(words: list[str]) -> str:
    """Words as a line: with spaces, except between two characters of a script written without them (so a Japanese
    line with an English word in it reads "君と dance して")."""
    out = ""
    for w in words:
        if out and not (_NO_SPACES.match(out[-1]) and _NO_SPACES.match(w[:1])):
            out += " "
        out += w
    return out


def _break(g: list[dict], i: int) -> float:
    """How much a new line looks like it starts at g[i]: the pause before it in seconds, plus 0.5 after a full stop,
    question or exclamation mark (0.1 after a comma: Whisper puts commas inside sung lines too), 0.4 for a capital that
    isn't "I" (Whisper starts most sung lines with one; in a script without capitals, for any word after a full
    stop), 0.3 for a new segment. Tuned on the trial's tracks."""
    prev, w = g[i - 1]["w"], g[i]["w"]
    s = max(0.0, g[i]["t"] - g[i - 1]["end"])
    s += 0.5 if prev.endswith(_END) else 0.1 if prev.endswith(_PAUSE) else 0.0
    caseless = w[:1].isalpha() and not (w[:1].isupper() or w[:1].islower())
    s += 0.4 if (w[:1].isupper() and not re.match(r"I\b|I'", w)) or (caseless and prev.endswith(_END)) else 0.0
    s += 0.3 if g[i].get("seg") is not None and g[i].get("seg") != g[i - 1].get("seg") else 0.0
    return s


def _split(g: list[dict]) -> list[list[dict]]:
    size = _size(g)
    if size < 2 * MIN_WORDS:
        return [g]
    # Breaks that leave at least MIN_WORDS on each side; the best one, nudged towards the middle so a long run
    # splits into lines of similar length.
    before, at = [], 0.0
    for w in g:
        before.append(at)
        at += _word_size(w["w"])
    ok = [k for k in range(1, len(g)) if MIN_WORDS <= before[k] <= size - MIN_WORDS]
    if not ok:
        return [g]
    i = max(ok, key=lambda k: _break(g, k) - 0.02 * abs(before[k] - size / 2))
    if size > LINE_WORDS or _break(g, i) >= SPLIT_AT:
        return _split(g[:i]) + _split(g[i:])
    return [g]


def norm_words(text: str) -> list[str]:
    """Lowercase words without punctuation, for comparing what was sung with what was written, in any script.
    Accents and vowel marks go (é is e; Arabic and Hebrew are compared without their optional vowel signs), so a
    word written either way still matches; Chinese, Japanese, Thai and the like have no spaces between words, so
    each of their characters is a word of its own."""
    out, cur = [], []

    def done() -> None:
        word = unicodedata.normalize("NFC", "".join(cur)).strip("'")
        if word:
            out.append(word)
        cur.clear()

    text = unicodedata.normalize("NFKC", text).casefold().replace("’", "'").replace("\u0640", "")
    for ch in unicodedata.normalize("NFD", text):
        cat = unicodedata.category(ch)
        if cat == "Mn":
            continue
        if _NO_SPACES.match(ch):
            done()
            if cat[0] in "LN":
                out.append(ch)
        elif cat[0] in "LN" or cat == "Mc" or ch == "'":
            cur.append(ch)
        else:
            done()
    done()
    return out


def _align(ref: list[str], hyp: list[str]) -> tuple[int, dict[int, int]]:
    """Edit distance between two word lists, and which hyp word each matched ref word lines up with."""
    n, m = len(ref), len(hyp)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]))
    pairs, i, j = {}, n, m
    while i and j:
        if d[i][j] == d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]):
            if ref[i - 1] == hyp[j - 1]:
                pairs[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif d[i][j] == d[i - 1][j] + 1:
            i -= 1
        else:
            j -= 1
    return d[n][m], pairs


WORD_SECONDS = 0.3  # about how long a sung word takes, to start a line whose first words weren't heard


def align_lines(lines: list[str], words: list[dict]) -> list[dict]:
    """Time lyrics we have as text: [{t, line}], each line starting where the heard words line up with its own (an
    edit-distance alignment, in order). A line is placed only when at least two of its words were heard (its one
    word, for a one-word line): a single word of a repeated hook lines up just as well with the wrong chorus. A
    line whose first words weren't heard starts WORD_SECONDS a word earlier than its first heard word, but never
    before the line above. A line that can't be placed gets t=None."""
    ref, owner, place = [], [], []
    for k, line in enumerate(lines):
        for j, w in enumerate(norm_words(line)):
            ref.append(w)
            owner.append(k)
            place.append(j)
    hyp = heard_words(words)
    _, pairs = _align(ref, [h[0] for h in hyp])
    hits: dict[int, list[tuple[int, float]]] = {}
    for i in sorted(pairs):
        hits.setdefault(owner[i], []).append((place[i], hyp[pairs[i]][1]))
    out, last = [], 0.0
    for k, line in enumerate(lines):
        h, n = hits.get(k, []), len(norm_words(line))
        t = None
        if h and len(h) >= min(2, n):
            # Never before the line above: the words came in order, an early start is only the estimate.
            t = round(max(last, h[0][1] - WORD_SECONDS * h[0][0]), 2)
            last = t
        out.append({"t": t, "line": line})
    return out


def heard_words(words: list[dict]) -> list[tuple[str, float]]:
    """(word, start) for every word in what was heard, as norm_words splits it: one of Whisper's words can hold
    several (two Chinese characters, "mighty-moon"), timed evenly across it."""
    out = []
    for w in words:
        parts = norm_words(w["w"])
        step = (w.get("end", w["t"]) - w["t"]) / len(parts) if parts else 0.0
        out += [(nw, round(w["t"] + k * step, 2)) for k, nw in enumerate(parts)]
    return out


def lines_heard(timed: list[dict], words: list[dict], slack: float = 2.0) -> float:
    """The share of timed lines (LRCLIB's, [{t, line}]) at least half of whose words were heard while the line was
    being sung: between its start and the next line's (the last line: 10 s), give or take `slack` seconds. Unlike
    `align_lines`, this can't mix up the choruses of a repeated hook."""
    heard = [(t, nw) for nw, t in heard_words(words)]
    got = 0
    for k, x in enumerate(timed):
        end = timed[k + 1]["t"] if k + 1 < len(timed) else x["t"] + 10.0
        pool = [nw for t, nw in heard if x["t"] - slack <= t <= end + slack]
        want = norm_words(x["line"])
        found = 0
        for w in want:
            if w in pool:
                pool.remove(w)
                found += 1
        got += found * 2 >= len(want)
    return round(got / len(timed), 3) if timed else 0.0


def score(heard: dict, plain: str | None, synced: list[dict] | None) -> dict[str, Any]:
    """How a transcription compares with LRCLIB's lyrics: `wer` (word error rate against the plain text, 0 is
    perfect, can pass 1), and with timed lyrics, `timing` of the lines that `align_lines` could place: the median
    start error in seconds and the share within 1 s, and `lines_heard` (see there). Numbers only, no words."""
    ref_text = plain or "\n".join(x["line"] for x in synced or [])
    ref, hyp = norm_words(ref_text), [nw for w in heard["words"] for nw in norm_words(w["w"])]
    out: dict[str, Any] = {"ref_words": len(ref), "heard_words": len(hyp)}
    if ref:
        out["wer"] = round(_align(ref, hyp)[0] / len(ref), 3)
    timed = [x for x in synced or [] if norm_words(x["line"])]
    if timed:
        placed = align_lines([x["line"] for x in timed], heard["words"])
        errs = sorted(abs(p["t"] - x["t"]) for p, x in zip(placed, timed) if p["t"] is not None)
        out["timing"] = {"lines": len(timed), "placed": len(errs),
                         "median_error": round(errs[len(errs) // 2], 2) if errs else None,
                         "within_1s": round(sum(e <= 1.0 for e in errs) / len(timed), 3),
                         "lines_heard": lines_heard(timed, heard["words"])}
    return out
