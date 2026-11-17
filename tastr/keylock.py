"""Key lock for mash-ups: the second track time-stretched to the first one's tempo, with its pitch kept.

Strudel's `.speed()` changes pitch along with tempo, like a turntable, so a mash-up that speeds B up by 4% also
moves it most of a semitone out of key. Instead, the server stretches B (its full mix and, once separated, its
stems) to the mash-up's speed ratio ahead of time, and the code plays that audio at speed 1.

- The stretch is Signalsmith Stretch (MIT) through `python-stretch`, an optional extra (`pip install
  'tastr[keylock]'`); without it `available()` is False and the mash-up falls back to `.speed()`. With
  TASTR_STEMS_BACKEND=modal and the audio bucket on, it runs in Modal's `keylock` function instead
  (CPU only: about a second for a 7-minute track), which reads each part from a signed link and writes it back
  to a signed upload link, like stems.
- Cache: `$TASTR_HOME/keylock/<sha256 prefix>/<ratio>/<part>.flac` (part: mix or a stem name), in the audio
  bucket when that is on. Each part is stretched once per ratio and reused by every mash-up at that ratio (any
  A at that tempo); stems separated later are stretched on their own. Each track keeps its KEEP_RATIOS most
  recently made ratios, and the cache goes with the track (or its expired audio), like its stems.
- `Jobs`: one stretch at a time in the background. Nothing to resume after a restart: the page asks again.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import threading
import time
from pathlib import Path

from . import blobs, runner, stems

PARTS = ("mix", *stems.STEM_NAMES)
SAMPLE_RATE = 44100
MIN_RATIO, MAX_RATIO = 0.7, 1.43  # the mash-up folds its speed into 0.71 to 1.42 (strudelSpeed in strudel.js)
INSTALL_HINT = "Key lock needs the stretcher, an optional install: pip install 'tastr[keylock]'"

log = logging.getLogger(__name__)


def backend() -> str:
    """"modal" when stems run on Modal and the audio bucket is on (Modal reads and writes the bucket), else "local"."""
    return "modal" if stems.backend() == "modal" and blobs.enabled() else "local"


def available() -> bool:
    if backend() == "modal":
        from .cloud import remote

        return remote.available()
    return all(importlib.util.find_spec(m) is not None for m in ("python_stretch", "soundfile", "librosa"))


def ratio_text(ratio: float) -> str:
    """The ratio as it appears in paths and links: three decimals, like the mash-up's speed."""
    return f"{ratio:.3f}"


def parse_ratio(text: str) -> float | None:
    """A ratio from a link or a request, or None when it isn't one the mash-up can ask for."""
    try:
        r = round(float(text), 3)
    except (TypeError, ValueError):
        return None
    return r if MIN_RATIO <= r <= MAX_RATIO else None


def root() -> Path:
    return Path(os.environ.get("TASTR_HOME", Path.home() / ".tastr")) / "keylock"


def track_dir(sha256: str | None, track_id: int) -> Path:
    """Every stretched copy of one audio file (several tracks of the same file share it, like stems)."""
    return root() / (sha256[:16] if sha256 else f"track-{track_id}")


def folder(sha256: str | None, track_id: int, ratio: float) -> Path:
    return track_dir(sha256, track_id) / ratio_text(ratio)


def part_path(where: Path, part: str) -> Path:
    return where / f"{part}.flac"


def ready_parts(where: Path) -> list[str]:
    return [p for p in PARTS if blobs.exists(part_path(where, p))]


def remove(tdir: Path) -> None:
    """Delete every stretched copy of a track (here and in the bucket)."""
    shutil.rmtree(tdir, ignore_errors=True)
    blobs.remove_tree(tdir)


KEEP_RATIOS = 3  # stretched copies kept per track: the ratios used most recently


def _prune(keep: Path, n: int = KEEP_RATIOS) -> None:
    """Delete the track's older ratios so it keeps `n` including `keep` (the one about to be written). Age is
    when a ratio's newest file was written (here, else in the bucket)."""
    tdir = keep.parent
    newest: dict[str, float] = {}
    if tdir.is_dir():
        for d in tdir.iterdir():
            files = [f.stat().st_mtime for f in d.glob("*.flac")] if d.is_dir() else []
            newest[d.name] = max(files, default=d.stat().st_mtime)
    prefix = blobs.key(tdir / "x") if blobs.enabled() else None
    listed = {}
    if prefix:
        listed = blobs.listing(prefix.rsplit("/", 1)[0] + "/", fresh=True)
        for k, (_, when) in listed.items():
            name = k.split("/")[-2]
            newest[name] = max(newest.get(name, 0.0), when)
    others = sorted((r for r in newest if r != keep.name), key=lambda r: newest[r], reverse=True)
    for r in others[max(0, n - 1):]:
        shutil.rmtree(tdir / r, ignore_errors=True)
        for k in listed:
            if k.split("/")[-2] == r:
                blobs.remove(blobs.home() / k)


def stretch_file(src: str | Path, dst: Path, ratio: float) -> None:
    """Write `src` sped up by `ratio` with its pitch kept, as 16-bit FLAC at SAMPLE_RATE. Tests replace this."""
    import numpy as np
    import python_stretch as ps
    import soundfile as sf

    from .engine.audio import load_audio

    x = load_audio(src, SAMPLE_RATE, mono=False)  # (channels, n)
    if x.shape[0] == 1:
        x = np.repeat(x, 2, axis=0)
    st = ps.Signalsmith.Stretch()
    st.preset(x.shape[0], SAMPLE_RATE)
    st.setTimeFactor(ratio)
    # The binding reads the buffer as it lies in memory: a transposed view comes out as noise an octave down.
    y = np.asarray(st.process(np.ascontiguousarray(x[:2], dtype=np.float32)), dtype=np.float32)
    peak = float(np.abs(y).max()) if y.size else 0.0
    if peak > 1.0:  # the stretch can overshoot a hot master a little; scale rather than clip
        y /= peak
    dst.parent.mkdir(parents=True, exist_ok=True)
    sf.write(dst, y.T, SAMPLE_RATE, subtype="PCM_16")


def stretch(sources: dict[str, Path], where: Path, ratio: float) -> None:
    """Stretch each part in `sources` ({part: its audio}) into `where`, skipping parts already there."""
    todo = {p: src for p, src in sources.items() if p in PARTS and not blobs.exists(part_path(where, p))}
    if not todo:
        return
    _prune(where)
    if backend() == "modal":
        from .cloud import remote

        remote.keylock({p: blobs.signed_url(src) for p, src in todo.items()}, ratio,
                       {p: blobs.put_url(part_path(where, p)) for p in todo})
        blobs.reset_listings()
        missing = [p for p in todo if not blobs.in_bucket(part_path(where, p))]
        if missing:
            raise RuntimeError(f"Key lock produced no {', '.join(missing)}")
        return
    for p, src in todo.items():
        local = blobs.ensure(src) or src
        tmp = part_path(where, p).with_suffix(".partial.flac")
        try:
            stretch_file(local, tmp, ratio)
            tmp.replace(part_path(where, p))
        finally:
            tmp.unlink(missing_ok=True)
        blobs.push(part_path(where, p))


class Jobs:
    """Background stretches for the web server, one at a time; later requests wait their turn. Keyed by the
    ratio's folder. A job stretches the parts asked for that aren't there yet."""

    def __init__(self) -> None:
        self._lane = runner.current().lane("keylock", 1)
        self._lock = threading.Lock()
        self._state: dict[Path, dict] = {}

    def status(self, where: Path) -> dict:
        """{"state": none | queued | running | ready | error, "parts": the parts ready, "error"?}."""
        with self._lock:
            job = dict(self._state.get(where) or {})
        parts = ready_parts(where)
        if job.get("state") in ("queued", "running", "error"):
            return {"state": job["state"], "parts": parts, **({"error": job["error"]} if "error" in job else {})}
        return {"state": "ready" if parts else "none", "parts": parts}

    def start(self, sources: dict[str, Path], where: Path, ratio: float) -> dict:
        with self._lock:
            job = self._state.get(where)
            if not (job and job["state"] in ("queued", "running")):
                self._state[where] = {"state": "queued"}
                self._lane.submit(self._run, sources, where, ratio)
        return self.status(where)

    def forget(self, tdir: Path) -> None:
        """For a deleted track: drop its finished jobs (a running one writes files that the delete then misses,
        which the next delete or expiry sweep of the same file picks up)."""
        with self._lock:
            for k in [k for k, j in self._state.items() if k.parent == tdir and j["state"] not in ("queued", "running")]:
                del self._state[k]

    def _run(self, sources: dict[str, Path], where: Path, ratio: float) -> None:
        with self._lock:
            self._state[where] = {"state": "running"}
        start = time.monotonic()
        try:
            stretch(sources, where, ratio)
        except Exception as e:  # noqa: BLE001 - the page shows it and falls back to .speed()
            log.exception("Key lock failed for %s", where)
            with self._lock:
                self._state[where] = {"state": "error", "error": f"{type(e).__name__}: {e}"}
            return
        log.info("Key lock ready for %s (%.1f s)", where, time.monotonic() - start)
        with self._lock:
            self._state.pop(where, None)
