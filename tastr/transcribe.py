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
from pathlib import Path
from typing import Any

MODEL = "large-v3-turbo"
LINE_GAP = 0.6  # seconds of silence between two words that start a new line
LINE_WORDS = 10  # a longer line is split where a new line most likely starts
MIN_WORDS = 3  # a split never leaves fewer words than this on either side
SPLIT_AT = 0.6  # a line of 2 * MIN_WORDS words or more is split where a break scores this much (see _break)


def transcribe(path: str | Path, model: str = MODEL, language: str | None = None, device: str = "auto") -> dict:
    """{"language", "language_probability", "model", "lines": [{t, end, line}], "words": [{t, end, w, p}],
    "audio_seconds", "seconds"}. Voice activity detection skips the silent stretches of a vocal stem, where Whisper
    otherwise makes things up; not conditioning on the previous text stops it repeating a line forever."""
    from faster_whisper import WhisperModel

    start = time.monotonic()
    cuda = device == "cuda" or (device == "auto" and _has_cuda())
    m = WhisperModel(model, device="cuda" if cuda else "cpu", compute_type="float16" if cuda else "int8")
    loaded = time.monotonic()
    segments, info = m.transcribe(str(path), language=language, word_timestamps=True, vad_filter=True,
                                  condition_on_previous_text=False, beam_size=5)
    words = [{"t": round(w.start, 2), "end": round(w.end, 2), "w": w.word.strip(), "p": round(w.probability, 3),
              "seg": k} for k, s in enumerate(segments) for w in (s.words or []) if w.word.strip()]
    return {"language": info.language, "language_probability": round(info.language_probability, 3), "model": model,
            "device": "cuda" if cuda else "cpu", "lines": lines_from_words(words), "words": words,
            "audio_seconds": round(info.duration, 1), "load_seconds": round(loaded - start, 1),
            "seconds": round(time.monotonic() - loaded, 1)}


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
    word or an ad-lib doesn't make a line of its own."""
    runs: list[list[dict]] = []
    for w in words:
        if runs and w["t"] - runs[-1][-1]["end"] < LINE_GAP:
            runs[-1].append(w)
        else:
            runs.append([w])
    merged: list[list[dict]] = []
    for r in runs:
        if merged and len(r) < MIN_WORDS and r[0]["t"] - merged[-1][-1]["end"] < 2 * LINE_GAP \
                and len(merged[-1]) + len(r) <= LINE_WORDS:
            merged[-1] += r
        else:
            merged.append(r)
    out = [line for r in merged for line in _split(r)]
    return [{"t": g[0]["t"], "end": g[-1]["end"], "line": " ".join(w["w"] for w in g)} for g in out]


def _break(g: list[dict], i: int) -> float:
    """How much a new line looks like it starts at g[i]: the pause before it in seconds, plus 0.5 after a full stop,
    question or exclamation mark (0.1 after a comma: Whisper puts commas inside sung lines too), 0.4 for a capital that
    isn't "I" (Whisper starts most sung lines with one), 0.3 for a new segment. Tuned on the trial's tracks."""
    prev, w = g[i - 1]["w"], g[i]["w"]
    s = max(0.0, g[i]["t"] - g[i - 1]["end"])
    s += 0.5 if prev.endswith((".", "?", "!")) else 0.1 if prev.endswith((",", ";", ":")) else 0.0
    s += 0.4 if w[:1].isupper() and not re.match(r"I\b|I'", w) else 0.0
    s += 0.3 if g[i].get("seg") is not None and g[i].get("seg") != g[i - 1].get("seg") else 0.0
    return s


def _split(g: list[dict]) -> list[list[dict]]:
    if len(g) < 2 * MIN_WORDS:
        return [g]
    # The best break, nudged towards the middle so a long run splits into lines of similar length.
    i = max(range(MIN_WORDS, len(g) - MIN_WORDS + 1), key=lambda k: _break(g, k) - 0.02 * abs(k - len(g) / 2))
    if len(g) > LINE_WORDS or _break(g, i) >= SPLIT_AT:
        return _split(g[:i]) + _split(g[i:])
    return [g]


def norm_words(text: str) -> list[str]:
    """Lowercase words without punctuation, for comparing what was sung with what was written."""
    return re.findall(r"[a-z0-9À-ɏ']+", text.lower().replace("’", "'"))


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
    line whose first words weren't heard starts WORD_SECONDS a word earlier than its first heard word. A line that
    can't be placed gets t=None."""
    ref, owner, place = [], [], []
    for k, line in enumerate(lines):
        for j, w in enumerate(norm_words(line)):
            ref.append(w)
            owner.append(k)
            place.append(j)
    hyp = [(nw, w) for w in words for nw in norm_words(w["w"])[:1]]
    _, pairs = _align(ref, [h[0] for h in hyp])
    hits: dict[int, list[tuple[int, float]]] = {}
    for i in sorted(pairs):
        hits.setdefault(owner[i], []).append((place[i], hyp[pairs[i]][1]["t"]))
    out = []
    for k, line in enumerate(lines):
        h, n = hits.get(k, []), len(norm_words(line))
        t = round(max(0.0, h[0][1] - WORD_SECONDS * h[0][0]), 2) if h and len(h) >= min(2, n) else None
        out.append({"t": t, "line": line})
    return out


def lines_heard(timed: list[dict], words: list[dict], slack: float = 2.0) -> float:
    """The share of timed lines (LRCLIB's, [{t, line}]) at least half of whose words were heard while the line was
    being sung: between its start and the next line's (the last line: 10 s), give or take `slack` seconds. Unlike
    `align_lines`, this can't mix up the choruses of a repeated hook."""
    heard = [(w["t"], nw) for w in words for nw in norm_words(w["w"])]
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
