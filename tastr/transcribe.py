"""Lyrics from the audio itself: Whisper speech recognition on a track's vocal stem (an experiment, not in the app).

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
LINE_WORDS = 12  # a line longer than this is split at its widest gap


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
    words = [{"t": round(w.start, 2), "end": round(w.end, 2), "w": w.word.strip(), "p": round(w.probability, 3)}
             for s in segments for w in (s.words or []) if w.word.strip()]
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
    """Group words into lines: a new line after a pause of LINE_GAP, and a line of more than LINE_WORDS words split
    at its widest gap."""
    groups: list[list[dict]] = []
    for w in words:
        if groups and w["t"] - groups[-1][-1]["end"] < LINE_GAP:
            groups[-1].append(w)
        else:
            groups.append([w])
    out: list[list[dict]] = []
    while groups:
        g = groups.pop(0)
        if len(g) > LINE_WORDS:
            i = max(range(1, len(g)), key=lambda k: g[k]["t"] - g[k - 1]["end"])
            groups[:0] = [g[:i], g[i:]]
            continue
        out.append(g)
    return [{"t": g[0]["t"], "end": g[-1]["end"], "line": " ".join(w["w"] for w in g)} for g in out]


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


def align_lines(lines: list[str], words: list[dict]) -> list[dict]:
    """Time lyrics we have as text: [{t, line}] with each line's start taken from the heard word that lines up with
    its first word that was heard (a line whose first words weren't heard starts a little late). A line with no
    word heard gets t=None."""
    ref, owner = [], []
    for k, line in enumerate(lines):
        for w in norm_words(line):
            ref.append(w)
            owner.append(k)
    hyp = [(nw, w) for w in words for nw in norm_words(w["w"])[:1]]
    _, pairs = _align(ref, [h[0] for h in hyp])
    out = [{"t": None, "line": line} for line in lines]
    for i in sorted(pairs):
        k = owner[i]
        if out[k]["t"] is None:
            out[k]["t"] = hyp[pairs[i]][1]["t"]
    return out


def score(heard: dict, plain: str | None, synced: list[dict] | None) -> dict[str, Any]:
    """How a transcription compares with LRCLIB's lyrics: `wer` (word error rate against the plain text, 0 is
    perfect, can pass 1), and with timed lyrics, `timing` of the lines that `align_lines` could place: the median
    start error in seconds and the share within 1 s. Numbers only, no words."""
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
                         "within_1s": round(sum(e <= 1.0 for e in errs) / len(timed), 3)}
    return out
