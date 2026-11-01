"""Uploads and links in, track cards out: the analysis endpoints and their background jobs."""

from __future__ import annotations

import re
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ... import blobs, db, service
from ...engine import browser_card
from ...ingest import AUDIO_EXTENSIONS, IngestError, is_url, resolve_inputs
from ..analysis_jobs import JobError, new_id, ref
from ..context import AppContext, _failure_reason, _results, log, tastr_home, upload_dir
from ..hosted import audio_minutes, names, stored_bytes, today

# TASTR_BROWSER_ENGINE=1: the page runs the whole Rust engine (WebAssembly, static/engine.js) and sends the finished
# card with the audio (tastr/engine/browser_card.py); the server stores it when it checks out.
CARD_NAME = "card.json"


class UrlBody(BaseModel):
    url: str


def router(ctx: AppContext) -> APIRouter:
    api = APIRouter()

    # Plain `def` endpoints below run in FastAPI's threadpool, so slow analysis
    # doesn't block the event loop.
    @api.post("/api/analyze")
    def analyze(request: Request, files: list[UploadFile] = File(...), background: bool = False,
                card: list[UploadFile] | None = File(None)):
        """Analyze uploaded files and answer with the results. With ?background=1, answer at once with a job
        to poll at /api/analyze/jobs/{id}, which says which step each file is on (the page's progress bars).
        `card`: with TASTR_BROWSER_ENGINE on, the card the page made from a single uploaded file; the server
        stores it when it checks out and analyzes the file itself otherwise (or when it's missing or too big)."""
        if ctx.hosted:
            check_upload(request.state.user, files)
        paths: list[str] = []
        for f in files:
            name = re.sub(r"[^\w.\- ]", "_", Path(f.filename or "upload").name) or "upload"
            # The unique part is a folder, so the stored file keeps the name the user gave it (that is what the title falls back to).
            dest = upload_dir() / uuid.uuid4().hex[:8] / name
            dest.parent.mkdir()
            size = 0
            with open(dest, "wb") as out:
                while chunk := f.file.read(1 << 20):
                    size += len(chunk)
                    if ctx.hosted and size > ctx.settings.max_upload_bytes:
                        break
                    out.write(chunk)
            if ctx.hosted and size > ctx.settings.max_upload_bytes:
                discard([dest, *map(Path, paths)])
                raise ctx.refuse(request.state.user, 413, f"{name} is larger than {ctx.settings.max_upload_mb:g} MB.",
                             f"upload size limit ({ctx.settings.max_upload_mb:g} MB)", name)
            paths.append(str(dest))
            minutes = audio_minutes(dest) if ctx.hosted else None
            if minutes is not None and minutes > ctx.settings.max_minutes:
                discard(map(Path, paths))
                raise ctx.refuse(request.state.user, 413,
                             f"{name} is {minutes:.0f} minutes long. This server analyzes tracks up to "
                             f"{ctx.settings.max_minutes:g} minutes, so a long mix needs cutting into shorter files first.",
                             f"track length limit ({ctx.settings.max_minutes:g} min)", f"{name}, {minutes:.0f} min")
        sig = save_card(card[0], Path(paths[0])) if ctx.browser_engine and card and len(card) == len(paths) == 1 else {}
        store(paths, sig, request)
        given = [Path(p).name for p in paths]
        job_id = new_id()
        if ctx.hosted:
            ctx.event(request.state.user, "upload", f"Uploaded {names(given)}" + (f" (ref {ref(job_id)})" if background else ""))
        if background:
            user = getattr(request.state, "user", None)
            return JSONResponse(ctx.analyses.start(ctx.owner_of(request), given,
                                               analysis_work(paths, user, ctx.owner_of(request), job_id, cards=sig), job_id),
                                status_code=202)
        with closing(ctx.conn()) as c:
            try:
                with ctx.analysis_slot():
                    rs = service.analyze_inputs(c, paths, owner=ctx.owner_of(request), cards=sig)
            except TimeoutError:
                raise HTTPException(503, "The server is busy analyzing other tracks. Try again in a few minutes.") from None
            finally:
                drop_cards(sig)
                release(paths)
            if ctx.hosted:  # only tracks that analyzed count toward the daily limit, so a failed try can be retried
                db.add_usage(c, request.state.user, "tracks", today(), sum(r.track_id is not None for r in rs))
            return ctx.results(rs, ctx.owner_of(request))

    def store(paths: list[str], sig: dict[str, str], request: Request) -> None:
        """With the audio bucket on (tastr/blobs.py), copy each upload there before analysis, so Modal reads it
        from there and the server's disk only caches it."""
        try:
            for p in paths:
                blobs.push(p)
        except Exception as e:
            log.exception("couldn't store an upload in the audio bucket")
            drop_cards(sig)
            for p in paths:  # what did reach the bucket goes again
                try:
                    blobs.remove(p)
                except Exception:  # noqa: BLE001 - the bucket is unreachable; nothing more to clean up there
                    log.warning("couldn't remove %s from the audio bucket", Path(p).name)
            discard(map(Path, paths))
            if ctx.hosted:
                ctx.event(request.state.user, "failed", f"Couldn't store the upload: {names([Path(p).name for p in paths])}, "
                          f"{type(e).__name__}", ok=False)
            raise HTTPException(503, "Couldn't store the upload. Try again in a minute.") from None

    def release(paths) -> None:
        """Once analyzed, an upload the audio bucket holds needs no copy here."""
        if not blobs.enabled():
            return
        for p in map(Path, paths):
            if blobs.in_bucket(p):
                p.unlink(missing_ok=True)
                if p.parent.is_dir() and not any(p.parent.iterdir()):
                    p.parent.rmdir()

    def discard(files) -> None:
        """Nothing from a refused upload stays on disk."""
        for p in files:
            p.unlink(missing_ok=True)
            if p.parent.is_dir():
                p.parent.rmdir()

    def save_card(upload: UploadFile, audio: Path) -> dict[str, str]:
        """Store the card the page made next to its audio until the analysis has read it: {audio: card file}.
        Nothing ({}) when it is too big; the server then analyzes the file itself."""
        dest, size = audio.parent / CARD_NAME, 0
        with open(dest, "wb") as out:
            while chunk := upload.file.read(1 << 20):
                size += len(chunk)
                if size > browser_card.MAX_BYTES:
                    break
                out.write(chunk)
        if size > browser_card.MAX_BYTES:
            dest.unlink(missing_ok=True)
            return {}
        return {str(audio): str(dest)}

    def drop_cards(sig: dict[str, str]) -> None:
        for path in sig.values():
            Path(path).unlink(missing_ok=True)

    def analysis_work(inputs: list[str], user: str | None, owner: str | None, job_id: str, link: bool = False,
                      cards: dict[str, str] | None = None):
        """The work of one background analysis job (see analysis_jobs.py), reporting each step as it starts.
        `cards`: {audio path: card file the page made} for uploads that came with one (deleted once read).
        Anything that stops the whole job goes in the admin log with the job's ref, as failed analyses do."""
        sig = {str(Path(k).resolve()): v for k, v in (cards or {}).items()}

        def work(progress) -> dict[str, Any]:
            try:
                return run(progress)
            except JobError as e:
                ctx.event(user, "failed", f"Analysis didn't start: {names([Path(i).name for i in inputs])}, {e} (ref {ref(job_id)})",
                      ok=False)
                raise
            except Exception as e:
                log.exception("analysis job %s failed", ref(job_id))
                ctx.event(user, "failed", f"Analysis job failed: {names([Path(i).name for i in inputs])}, "
                      f"{_failure_reason(f'{type(e).__name__}: {e}')} (ref {ref(job_id)})", ok=False)
                raise
            finally:
                drop_cards(sig)
                if not link:
                    release(inputs)

        def run(progress) -> dict[str, Any]:
            with closing(ctx.conn()) as c:
                if link:
                    progress.stage("fetch")
                try:
                    sources = resolve_inputs(inputs)
                except IngestError as e:
                    raise JobError(str(e)) from e
                progress.stage("waiting")
                rs, failed_at = [], []
                try:
                    with ctx.analysis_slot():
                        progress.running()
                        for i, source in enumerate(sources):
                            progress.stage("decode", i)
                            r = service.analyze_source(c, source, owner=owner, card=sig.get(source.local_path),
                                                       on_stage=lambda name, *_, i=i: progress.stage(name, i))
                            rs.append(r)
                            failed_at.append(progress.last_stage if r.error else None)
                except TimeoutError:
                    raise JobError("The server is busy analyzing other tracks. Try again in a few minutes.") from None
                if ctx.hosted:  # as for a plain upload: only tracks that analyzed count toward the daily limit
                    db.add_usage(c, user, "tracks", today(), sum(r.track_id is not None for r in rs))
            out = ctx.results(rs, user, job_id, failed_at)
            for item, stage in zip(out["results"], failed_at):
                if stage:
                    item["stage"] = stage
            return out
        return work

    @api.get("/api/analyze/jobs/{job_id}")
    def analysis_job(job_id: str, request: Request):
        """A background analysis: queued (with `position`), running (with `stage`), done (with `results`) or error."""
        job = ctx.analyses.status(job_id, ctx.owner_of(request))
        if job is None:
            raise HTTPException(404, "no such job")
        return job

    def check_upload(user: str, files: list[UploadFile]) -> None:
        if len(files) > ctx.settings.max_files:
            raise ctx.refuse(user, 400, f"Upload at most {ctx.settings.max_files} files at a time.",
                         f"files-per-upload limit ({ctx.settings.max_files})")
        for f in files:
            if Path(f.filename or "").suffix.lower() not in AUDIO_EXTENSIONS:
                raise HTTPException(415, f"{Path(f.filename or 'upload').name} isn't an audio file Tastr can read.")
        if stored_bytes(tastr_home()) >= ctx.settings.storage_gb * 1024 ** 3:
            raise ctx.refuse(user, 507, "This server is out of space for new uploads. Ask whoever invited you.",
                         f"storage cap ({ctx.settings.storage_gb:g} GB)")
        with closing(ctx.conn()) as c:
            used, most = db.usage_today(c, user, "tracks", today()), ctx.cap(c, user, "tracks")
        if used + len(files) > most:
            left = max(0, most - used)
            raise ctx.refuse(user, 429, f"You can analyze {most} tracks a day; {left} left today.",
                         f"daily track limit ({most})")

    @api.post("/api/analyze-url")
    def analyze_url(body: UrlBody, background: bool = False):
        if ctx.hosted:
            raise HTTPException(403, "Link analysis is turned off on this server. Upload the audio file instead.")
        if not is_url(body.url):  # never a path: anyone who can reach the server could read files off its disk
            raise HTTPException(400, "Paste an http(s) link to a track or playlist.")
        if background:
            job_id = new_id()
            return JSONResponse(ctx.analyses.start(None, [body.url], analysis_work([body.url], None, None, job_id, link=True), job_id),
                                status_code=202)
        with closing(ctx.conn()) as c:
            try:
                results = service.analyze_inputs(c, [body.url])
            except IngestError as e:
                raise HTTPException(400, str(e)) from e
        return _results(results)

    return api
