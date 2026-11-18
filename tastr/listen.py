"""Lyrics by listening: Whisper on a track's vocal stem (tastr/transcribe.py), for what LRCLIB can't give.

`POST /api/tracks/{id}/lyrics/listen` starts a background job that separates the track's stems first when it has
none (the usual stems job, waited on), then transcribes the vocals and stores the result in the `lyrics` table:

- When LRCLIB found the words but no times, they keep LRCLIB's words and get line times from what was heard
  (`timed_by: "whisper"`). A line Whisper didn't hear gets a time between its neighbours and `guess: true`.
- When the person has edited the lyrics and not timed them, their words get the times instead (the edits stay theirs,
  tastr/lyrics.py: edit_lines).
- Otherwise what was heard becomes the lyrics, with `provider: "whisper"`. The page says they were heard by a
  machine and may be wrong: in the trial, clear singing and rap were about 3 in 4 words right, while repetitive
  dance hooks lost most of their repeats.

Like LRCLIB's, the words belong to their writers, so they stay with the track's owner (tastr/lyrics.py). On a
server whose stems run on Modal with the audio bucket on (the hosted site), Whisper runs in Modal's `transcribe`
function on a T4, about 5-10 s of GPU a track, reading the vocals from a signed link; otherwise it runs here, which
needs faster-whisper (`pip install 'tastr[transcribe]'`) and is slow on a CPU.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
from pathlib import Path
from typing import Any, Callable

from . import blobs, db, runner, stems
from . import transcribe as tr

log = logging.getLogger(__name__)

INSTALL_HINT = "Listening for lyrics needs faster-whisper: pip install 'tastr[transcribe]'"
WAIT_SECONDS = 5  # how often a job checks on the stems it waits for


def backend() -> str:
    """"modal" when stems run on Modal and the audio bucket is on (Modal reads the vocals from it), else "local"."""
    return "modal" if stems.backend() == "modal" and blobs.enabled() else "local"


def available() -> bool:
    if backend() == "modal":
        from .cloud import remote

        return remote.available()
    return importlib.util.find_spec("faster_whisper") is not None


def hear(folder: Path) -> dict:
    """What Whisper hears in the folder's vocal stem (tastr/transcribe.transcribe's answer)."""
    vocals = stems.stem_path(folder, "vocals")
    if backend() == "modal":
        from .cloud import remote

        return remote.transcribe(blobs.signed_url(vocals), vocals.suffix)
    return tr.transcribe(blobs.ensure(vocals) or vocals)


def apply(stored: dict | None, heard: dict) -> dict[str, Any]:
    """The lyrics to store once Whisper has listened, given what is stored: LRCLIB's plain words timed by what was
    heard (left as they were when no line could be placed), or what was heard."""
    plain = (stored or {}).get("plain")
    if stored and stored.get("status") == "found" and stored.get("provider") == "lrclib" and plain \
            and not stored.get("synced"):
        # LRCLIB's words stay whatever was heard: timed when some lines could be placed, else as they were.
        out = {k: v for k, v in stored.items() if k != "fetched_at"}
        lines = [x for x in plain.splitlines() if x.strip()]
        placed = fill_times(tr.align_lines(lines, heard["words"]))
        if any(not x.get("guess") for x in placed):
            return out | {"synced": placed, "timed_by": "whisper", "model": heard.get("model")}
        return out
    lines = [{"t": x["t"], "line": x["line"]} for x in heard["lines"]]
    if not lines:
        return {"provider": "whisper", "status": "not_found", "heard": "nothing", "model": heard.get("model")}
    return {"provider": "whisper", "status": "found", "synced": lines, "plain": "\n".join(x["line"] for x in lines),
            "instrumental": False, "language": heard.get("language"), "languages": heard.get("languages"),
            "model": heard.get("model")}


def time_edits(edits: dict, heard: dict) -> dict[str, Any]:
    """The person's edited, untimed lyrics (tastr/lyrics.py: edit_lines) timed by what was heard, like LRCLIB's plain
    words in apply(): their words stay, lines that couldn't be placed get `guess: true`. Unchanged when no line could
    be placed."""
    lines = [x for x in (edits.get("plain") or "").splitlines() if x.strip()]
    placed = fill_times(tr.align_lines(lines, heard["words"]))
    if any(not x.get("guess") for x in placed):
        return edits | {"synced": placed, "timed_by": "whisper"}
    return edits


def fill_times(placed: list[dict]) -> list[dict]:
    """Give each line align_lines couldn't place (t=None) a time between its placed neighbours, marked
    `guess: true`, so the lines stay in order and the page can leave them unlit."""
    known = [(i, x["t"]) for i, x in enumerate(placed) if x["t"] is not None]
    out = []
    for i, x in enumerate(placed):
        if x["t"] is not None:
            out.append({"t": x["t"], "line": x["line"]})
            continue
        before = [(j, t) for j, t in known if j < i]
        after = [(j, t) for j, t in known if j > i]
        if before and after:
            (j0, t0), (j1, t1) = before[-1], after[0]
            t = t0 + (t1 - t0) * (i - j0) / (j1 - j0)
        elif before:
            t = before[-1][1] + 2.0 * (i - before[-1][0])
        elif after:
            t = max(0.0, after[0][1] - 2.0 * (after[0][0] - i))
        else:
            t = 0.0
        out.append({"t": round(t, 2), "line": x["line"], "guess": True})
    return out


class Jobs:
    """Background listening for the web server, one at a time; later requests wait their turn. Keyed by track id.
    Nothing to resume after a restart: the page asks again."""

    def __init__(self, conn: Callable, stem_jobs: stems.Jobs) -> None:
        self._conn = conn  # opens a database connection
        self._stems = stem_jobs
        self._lane = runner.current().lane("listen", 1)
        self._lock = threading.Lock()
        self._state: dict[int, dict] = {}

    def status(self, track_id: int) -> dict:
        """{"state": none | queued | separating | listening | error, "error"?}. Done is "none": the result is in
        the lyrics."""
        with self._lock:
            return dict(self._state.get(track_id) or {"state": "none"})

    def start(self, track_id: int, audio_path: Path, folder: Path) -> dict:
        with self._lock:
            job = self._state.get(track_id)
            if not (job and job["state"] in ("queued", "separating", "listening")):
                self._state[track_id] = {"state": "queued"}
                self._lane.submit(self._run, track_id, audio_path, folder)
        return self.status(track_id)

    def forget(self, track_id: int) -> None:
        with self._lock:
            job = self._state.get(track_id)
            if job and job["state"] == "error":
                del self._state[track_id]

    def _set(self, track_id: int, **job) -> None:
        with self._lock:
            self._state[track_id] = job

    def _run(self, track_id: int, audio_path: Path, folder: Path) -> None:
        try:
            if not stems.is_ready(folder):
                self._set(track_id, state="separating")
                if not self._stems.busy(folder):
                    self._stems.start(audio_path, folder)
                while self._stems.busy(folder):
                    runner.current().sleep(WAIT_SECONDS)
                if not stems.is_ready(folder):
                    err = self._stems.status(folder).get("error") or "the stems weren't made"
                    raise RuntimeError(f"Couldn't separate the stems: {err}")
            self._set(track_id, state="listening")
            heard = hear(folder)
            from contextlib import closing

            with closing(self._conn()) as c:
                edits = db.get_lyrics_edits(c, track_id)
                if edits and not edits["synced"]:  # the person's own words, not timed yet: time those
                    db.time_lyrics_edits(c, track_id, edits, time_edits(edits, heard))  # unless they changed meanwhile
                else:
                    db.save_lyrics(c, track_id, apply(db.get_lyrics(c, track_id), heard))
        except Exception as e:  # noqa: BLE001 - the page shows it
            log.exception("Listening for lyrics failed for track %s", track_id)
            self._set(track_id, state="error", error=f"{type(e).__name__}: {e}")
            return
        with self._lock:
            self._state.pop(track_id, None)
