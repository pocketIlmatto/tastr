"""A moment from a link: give a link, a start, an end and an optional sentence, and come back to a finished moment.

The whole source is fetched once (yt-dlp, `tastr[links]`) and kept as a deposit of its own: a track, or a
collection when it is long like a DJ set or a mix (LONG_SOURCE_S). A second moment from the same source reuses it.
The moment is cut out of it (ffmpeg, else libsndfile) and sits in it, at its start and end. Only the slice is
broken down: the engine's card and, where Demucs or Modal can run, its four stems. A whole set waits for later.

Files live under `$TASTR_HOME/bank/<deposit id>/` (`deposits.files_dir`), which the audio bucket keeps too when it
is on: `source.<ext>` for a source, and `slice.flac`, `card.json` and `stems/` for a moment. What the tooling
found goes into the moment's `extracted` data and its status moves from 'needs-source' through 'working' to
'ready' or 'failed' (extracted["error"] says why). `run` can start again at any point: what is done is kept.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from . import blobs, db, deposits

log = logging.getLogger(__name__)

LONG_SOURCE_S = 20 * 60  # a source longer than this is a collection (a DJ set, a mix), else a track
MAX_SOURCE_S = 6 * 3600  # longer sources aren't fetched
MAX_MOMENT_S = 15 * 60  # the slice that is analyzed and separated
SLICE, CARD, STEMS = "slice.flac", "card.json", "stems"

# fetch(url, folder, known) -> (audio file in `folder`, info): reads the link, and downloads the whole source into
# `folder` unless known(info) says the bank has it already (then the path is None). Tests replace it.
Fetcher = Callable[[str, Path, Callable[[dict[str, Any]], bool]], tuple[Path | None, dict[str, Any]]]


class MomentError(RuntimeError):
    """A moment that can't be finished; the message is fit to show the person."""


def check_range(start_s: float, end_s: float) -> None:
    if start_s is None or end_s is None:
        raise deposits.DepositError("A moment from a link needs a start and an end.")
    if end_s - start_s > MAX_MOMENT_S:
        raise deposits.DepositError(f"A moment can be at most {MAX_MOMENT_S // 60} minutes long.")


def add(conn: sqlite3.Connection, owner: str | None, url: str, start_s: float, end_s: float, *,
        note: str | None = None, title: str | None = None, tags=None, held_by: int | None = None) -> dict[str, Any]:
    """Save the moment as 'needs-source' and return it; `run` (or a background job running it) does the rest.
    `held_by`: a collection (or track) to put it in besides the source it is cut from."""
    if not str(url or "").startswith(("http://", "https://")):
        raise deposits.DepositError("The source has to be a link.")
    check_range(start_s, end_s)
    return deposits.add(conn, owner, "moment", title=title, note=note, source_kind="link", source_uri=url,
                        start_s=start_s, end_s=end_s, tags=tags, status="needs-source", held_by=held_by)


def owner_of(conn: sqlite3.Connection, deposit_id: int) -> tuple[bool, str | None]:
    """(whether the deposit exists, whose it is), for background jobs that only kept its id."""
    r = conn.execute("SELECT owner FROM deposits WHERE id = ?", (deposit_id,)).fetchone()
    return (r is not None, r[0] if r else None)


def run(conn: sqlite3.Connection, owner: str | None, moment_id: int, *, fetch: Fetcher | None = None,
        analyze: Callable[[Path], dict[str, Any]] | None = None, separate: bool = True,
        on_stage: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Fetch the moment's source (unless the bank has it), cut the slice, analyze it and separate its stems.
    Returns the moment as `deposits.get` does; on failure it is saved as 'failed' and MomentError is raised."""
    moment = deposits.get(conn, owner, moment_id)
    if moment is None or moment["size"] != "moment":
        raise MomentError(f"No moment {moment_id}.")
    if moment["source_kind"] != "link" or not moment["source_uri"]:
        raise MomentError("This moment has no link to fetch its source from.")
    check_range(moment["start_s"], moment["end_s"])

    def stage(name: str) -> None:
        deposits.update(conn, owner, moment_id, {"status": "working", "extracted": {"stage": name, "error": None}})
        if on_stage:
            on_stage(name)

    try:
        stage("fetch")
        source = source_for(conn, owner, moment["source_uri"], fetch or fetch_link)
        audio = _source_audio(source)
        length = (source["extracted"].get("audio") or {}).get("duration")
        if length and moment["end_s"] > length + 0.5:
            raise MomentError(f"The moment ends at {_clock(moment['end_s'])}, after the source's end "
                              f"({_clock(length)}).")
        deposits.place(conn, owner, source["id"], moment_id)
        stage("slice")
        folder = deposits.files_dir(moment_id)
        cut = folder / SLICE
        if not blobs.exists(cut):
            slice_audio(audio, cut, moment["start_s"], moment["end_s"])
            blobs.push(cut)
        found: dict[str, Any] = {"slice": {"path": _rel(cut), "bytes": blobs.size(cut),
                                           "duration": round(moment["end_s"] - moment["start_s"], 3)},
                                 "source_deposit": source["id"]}
        stage("analyze")
        found["analysis"] = _analysis(cut, folder / CARD, moment, analyze)
        found["stems"] = _stems(cut, folder / STEMS, stage) if separate else {"skipped": "not asked for"}
    except Exception as e:  # noqa: BLE001 - saved on the moment, then raised for the job's log
        message = str(e) if isinstance(e, (MomentError, deposits.DepositError)) else f"{type(e).__name__}: {e}"
        deposits.update(conn, owner, moment_id, {"status": "failed", "extracted": {"error": message}})
        if isinstance(e, MomentError):
            raise
        raise MomentError(message) from e
    title = moment["title"] or (f"{source['title']} {_clock(moment['start_s'])}-{_clock(moment['end_s'])}"
                                if source.get("title") else None)
    return deposits.update(conn, owner, moment_id, {
        "status": "ready", "title": title,
        "extracted": {**found, "stage": None, "error": None, "finished_at": db._now()}})


# ---- the source ----

def source_for(conn: sqlite3.Connection, owner: str | None, url: str, fetch: Fetcher) -> dict[str, Any]:
    """The deposit that holds the whole audio behind `url`: one the bank has already (by the link, or by the site's
    own id once the link is read), else a new one, fetched now."""
    have = _known_source(conn, owner, url=url)
    if have:
        return have
    incoming = blobs.home() / "bank" / "incoming" / f"{int(time.time() * 1000)}"
    shutil.rmtree(incoming, ignore_errors=True)
    incoming.mkdir(parents=True)
    try:
        same: list[dict[str, Any]] = []

        def known(info: dict[str, Any]) -> bool:  # the same video under another link: no second download
            got = _known_source(conn, owner, key=source_key(info))
            if got:
                same.append(got)
            return bool(got)

        path, info = fetch(url, incoming, known)
        if same:
            return same[0]
        if path is None:
            raise MomentError(f"Nothing was fetched from {url}.")
        key = source_key(info)
        duration = info.get("duration") or probe_duration(path)
        if duration and duration > MAX_SOURCE_S:
            raise MomentError(f"The source is {_clock(duration)} long; the bank fetches up to "
                              f"{MAX_SOURCE_S // 3600} hours.")
        size = "collection" if duration and duration > LONG_SOURCE_S else "track"
        context = {k: v for k, v in {
            "source_key": key, "link": url, "page": info.get("webpage_url"), "site": info.get("extractor"),
            "artist": info.get("artist") or info.get("creator") or info.get("uploader"),
            "uploaded": info.get("upload_date"), "fetched_with": info.get("fetched_with", "yt-dlp")}.items() if v}
        source = deposits.add(conn, owner, size, title=(info.get("title") or "")[:deposits.MAX_TITLE] or None,
                              source_kind="link", source_uri=info.get("webpage_url") or url, context=context,
                              status="working", fetched=True)
        dest = deposits.files_dir(source["id"]) / f"source{path.suffix.lower()}"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), dest)
            blobs.push(dest)
        except Exception:
            deposits.delete(conn, owner, source["id"])  # no shell without its audio
            raise
        audio = {"path": _rel(dest), "bytes": blobs.size(dest), "duration": round(duration, 3) if duration else None,
                 "format": dest.suffix.lstrip(".")}
        return deposits.update(conn, owner, source["id"], {"status": "ready", "extracted": {"audio": audio}})
    finally:
        shutil.rmtree(incoming, ignore_errors=True)


def source_key(info: dict[str, Any]) -> str | None:
    """The site and its own id for the media (e.g. "youtube:CsGauHXioos"), the same under any link to it."""
    site, mid = info.get("extractor_key") or info.get("extractor"), info.get("id")
    return f"{str(site).lower()}:{mid}" if site and mid else None


def _known_source(conn, owner, *, url: str | None = None, key: str | None = None) -> dict[str, Any] | None:
    if url:
        rows = conn.execute("SELECT id FROM deposits WHERE owner IS ? AND size != 'moment' AND source_kind = 'link' "
                            "AND (source_uri = ? OR json_extract(context_json, '$.link') = ?) ORDER BY id",
                            (owner, url, url)).fetchall()
    elif key:
        rows = conn.execute("SELECT id FROM deposits WHERE owner IS ? AND size != 'moment' AND source_kind = 'link' "
                            "AND json_extract(context_json, '$.source_key') = ? ORDER BY id", (owner, key)).fetchall()
    else:
        return None
    for (did,) in rows:
        got = deposits.get(conn, owner, did)
        if got and _source_audio(got, missing_ok=True):
            return got
    return None


def _source_audio(source: dict[str, Any], missing_ok: bool = False) -> Path | None:
    rel = (source["extracted"].get("audio") or {}).get("path")
    path = blobs.home() / rel if rel else None
    if path is not None and blobs.exists(path):
        return blobs.ensure(path) or path
    if missing_ok:
        return None
    raise MomentError(f"The audio of deposit {source['id']} is gone; fetch it again.")


def fetch_link(url: str, folder: Path, known: Callable[[dict[str, Any]], bool] = lambda info: False
               ) -> tuple[Path | None, dict[str, Any]]:
    """Download the whole audio behind one link (a video, a track, a set) into `folder` with yt-dlp, unless
    `known(info)` says the bank has it already."""
    from .resolvers.ytdlp import _import_yt_dlp

    yt_dlp = _import_yt_dlp()
    template = str(folder).replace("%", "%%") + "/source.%(ext)s"
    opts = {"format": "bestaudio/best", "outtmpl": template, "quiet": True, "no_warnings": True,
            "noplaylist": True}  # a video link that also names a playlist means the video
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("entries") is not None or info.get("_type") == "playlist":
                raise MomentError("A moment needs a link to one video or track, not a playlist.")
            if info.get("is_live"):
                raise MomentError("That link is a live stream; save a moment from it once it has ended.")
            if info.get("duration") and info["duration"] > MAX_SOURCE_S:
                raise MomentError(f"The source is {_clock(info['duration'])} long; the bank fetches up to "
                                  f"{MAX_SOURCE_S // 3600} hours.")
            keep = {k: info.get(k) for k in ("id", "extractor", "extractor_key", "title", "duration", "webpage_url",
                                             "artist", "creator", "uploader", "upload_date")}
            if known(keep):
                return None, keep
            ydl.process_info(info)
    except MomentError:
        raise
    except Exception as e:  # noqa: BLE001 - yt-dlp's own errors say what the site answered
        raise MomentError(f"Could not fetch {url}: {e}") from e
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix not in (".part", ".ytdl", ".temp")]
    if not files:
        raise MomentError(f"yt-dlp finished but left no audio for {url}.")
    return files[0], keep


# ---- the slice ----

def probe_duration(path: Path) -> float | None:
    """The audio's length in seconds, or None when nothing here can tell."""
    try:
        import soundfile as sf

        return float(sf.info(str(path)).duration)
    except Exception:  # noqa: BLE001 - a format libsndfile doesn't read
        pass
    if shutil.which("ffprobe"):
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                              str(path)], capture_output=True, text=True)
        try:
            return float(out.stdout.strip())
        except ValueError:
            return None
    return None


def slice_audio(src: Path, out: Path, start_s: float, end_s: float) -> None:
    """Write `src` from `start_s` to `end_s` to `out` as FLAC, at the source's rate and channels."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".partial.flac")
    tmp.unlink(missing_ok=True)
    try:
        if shutil.which("ffmpeg"):
            proc = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", f"{start_s:.3f}", "-i", str(src),
                                   "-t", f"{end_s - start_s:.3f}", "-vn", "-c:a", "flac", "-y", str(tmp)],
                                  capture_output=True)
            if proc.returncode != 0:
                raise MomentError(f"ffmpeg could not cut the slice: {proc.stderr.decode(errors='replace')[:300]}")
        else:
            import soundfile as sf

            try:
                with sf.SoundFile(str(src)) as f:
                    f.seek(int(start_s * f.samplerate))
                    data = f.read(int((end_s - start_s) * f.samplerate), always_2d=True)
                    rate = f.samplerate
            except Exception as e:  # noqa: BLE001
                raise MomentError(f"Cutting {src.suffix} audio needs ffmpeg installed ({e}).") from e
            sf.write(str(tmp), data, rate, format="FLAC")
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise MomentError("The slice came out empty; check the start and end against the source.")
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)


# ---- breaking the slice down ----

def _analysis(cut: Path, card_file: Path, moment: dict[str, Any], analyze) -> dict[str, Any]:
    """The engine's card for the slice, kept whole in card.json, and its headline numbers for `extracted`."""
    if blobs.exists(card_file):
        card = json.loads((blobs.ensure(card_file) or card_file).read_text())
    else:
        card = (analyze or analyze_slice)(cut)
        card_file.parent.mkdir(parents=True, exist_ok=True)  # the slice may be only in the bucket (Modal read it)
        card_file.write_text(json.dumps(card))
        blobs.push(card_file)
    core, key = card.get("core") or {}, (card.get("core") or {}).get("key") or {}
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    return {
        "card": _rel(card_file), "duration": card.get("duration"),
        "bpm": core.get("bpm"), "bpm_confidence": core.get("bpm_confidence"),
        "key": (f"{names[key['tonic'] % 12]} {key['mode']}" if key.get("tonic") is not None else None),
        "camelot": key.get("camelot"), "key_confidence": key.get("confidence"), "energy": core.get("energy"),
        "beats_per_bar": core.get("beats_per_bar"),
        # times in the slice; add the moment's start for times in the source
        "sections": [{k: s.get(k) for k in ("start", "end", "label", "letter", "energy")}
                     for s in card.get("sections") or []],
        "tags": [{"tag": t["tag"], "score": t["score"]} for t in card.get("tags") or []],
        "offset_s": moment["start_s"],
    }


def analyze_slice(path: Path) -> dict[str, Any]:
    """The engine's card for a slice, here or on Modal (TASTR_ANALYSIS_BACKEND), as a dict."""
    from .engine.card import Source
    from .service import _analyze_audio

    source = Source(kind="file", uri=str(path), local_path=str(path), title=path.stem)
    return _analyze_audio(source, None, None).to_dict()


def _stems(cut: Path, folder: Path, stage: Callable[[str], None]) -> dict[str, Any]:
    from . import stems

    if stems.is_ready(folder):
        return {"names": list(stems.STEM_NAMES), "made_with": stems.made_with(folder), "folder": _rel(folder)}
    if not stems.available():
        return {"skipped": stems.INSTALL_HINT}
    stage("stems")
    stems.separate_track(cut, folder)
    return {"names": list(stems.STEM_NAMES), "made_with": stems.made_with(folder), "folder": _rel(folder)}


def slice_path(moment_id: int) -> Path:
    return deposits.files_dir(moment_id) / SLICE


def card_path(moment_id: int) -> Path:
    return deposits.files_dir(moment_id) / CARD


def stems_folder(moment_id: int) -> Path:
    return deposits.files_dir(moment_id) / STEMS


def source_path(source: dict[str, Any]) -> Path | None:
    rel = (source["extracted"].get("audio") or {}).get("path")
    return blobs.home() / rel if rel else None


def _rel(path: Path) -> str:
    return Path(path).resolve().relative_to(blobs.home().resolve()).as_posix()


def _clock(seconds: float) -> str:
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
