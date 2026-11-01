"""Uploads and links in, track cards out: the analysis endpoints and their background jobs."""

from __future__ import annotations

import re
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ... import blobs, db, service
from ...engine import browser_card
from ...ingest import AUDIO_EXTENSIONS, IngestError, is_url, resolve_inputs
from ...markers import Markers
from ..analysis_jobs import JobError, new_id, ref
from ..context import AppContext, _failure_reason, _results, log, tastr_home, upload_dir
from ..hosted import audio_minutes, names, stored_bytes, today

# TASTR_BROWSER_ENGINE=1: the page runs the whole Rust engine (WebAssembly, static/engine.js) and sends the finished
# card with the audio (tastr/engine/browser_card.py); the server stores it when it checks out.
CARD_NAME = "card.json"


class UrlBody(BaseModel):
    url: str


class UploadFileInfo(BaseModel):
    name: str
    size: int = Field(ge=0)


class UploadsBody(BaseModel):
    files: list[UploadFileInfo] = Field(min_length=1)


def safe_name(name: str | None) -> str:
    """A stored upload's file name: the one the user gave, without a path or unusual characters."""
    return re.sub(r"[^\w.\- ]", "_", Path(name or "upload").name) or "upload"


UPLOAD_MARKERS = "upload"  # job_markers kind for direct uploads given a link and not yet finished


def prune_uploads(db_path, older_than: float = 2 * blobs.UPLOAD_SECONDS, now: float | None = None) -> int:
    """Delete direct uploads that got a link but were never finished (the page closed, the file failed), from
    the bucket and here; returns how many. Run by the expiry loop."""
    marks = Markers(db_path, UPLOAD_MARKERS)
    cutoff = (now if now is not None else time.time()) - older_than
    n = 0
    for upload_id, data, at in marks.items():
        if at >= cutoff:
            continue
        p = upload_dir() / upload_id / str(data.get("name") or "upload")
        try:
            blobs.remove(p)
        except Exception:  # noqa: BLE001 - the bucket is unreachable: try again next time
            log.warning("couldn't remove the abandoned upload %s", upload_id)
            continue
        if p.parent.is_dir() and not any(p.parent.iterdir()):
            p.parent.rmdir()
        marks.remove(upload_id)
        n += 1
    return n


def router(ctx: AppContext) -> APIRouter:
    api = APIRouter()
    uploads = Markers(ctx.db_path, UPLOAD_MARKERS)

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
            check_upload(request.state.user, [f.filename or "" for f in files])
        paths: list[str] = []
        for f in files:
            name = safe_name(f.filename)
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
                raise too_big(request.state.user, name)
            paths.append(str(dest))
            check_minutes(request, paths, dest)
        sig = save_card(card[0], Path(paths[0])) if ctx.browser_engine and card and len(card) == len(paths) == 1 else {}
        store(paths, sig, request)
        return begin(request, paths, sig, background)

    # ---- uploads straight to the audio bucket (TASTR_DIRECT_UPLOAD, TAS-66) ----------------------------------
    # The page asks for a signed link per file (the limits are checked here, before any bytes move), PUTs the file
    # to the bucket itself, then calls /api/analyze/uploaded, which checks what arrived and analyzes it as above.
    # Each link leaves a marker until the upload is finished; abandoned ones are deleted by `prune_uploads`.

    def direct() -> bool:
        return ctx.hosted and ctx.settings.direct_upload and blobs.enabled()

    def upload_path(upload_id: str, user: str) -> Path:
        """The stored path an upload link was made for, if it's this person's and still open; 404 otherwise."""
        data = uploads.get(upload_id) if re.fullmatch(r"[0-9a-f]{8}", upload_id) else None
        if not data or data.get("owner") != user:
            raise HTTPException(404, "That upload has expired or was already used. Upload the file again.")
        return upload_dir() / upload_id / data["name"]

    def link(path: Path) -> dict[str, Any]:
        return {"url": blobs.put_url(path, blobs.UPLOAD_SECONDS), "content_type": blobs.content_type(path),
                "expires_in": blobs.UPLOAD_SECONDS}

    @api.post("/api/uploads")
    def new_uploads(body: UploadsBody, request: Request):
        """Signed links to upload files straight to the audio bucket: {uploads: [{id, name, url, content_type,
        expires_in}]}, one per file in order. PUT each file to its url with that Content-Type."""
        if not direct():
            raise HTTPException(404, "Uploads go through /api/analyze on this server.")
        user = request.state.user
        check_upload(user, [f.name for f in body.files])
        for f in body.files:
            if f.size > ctx.settings.max_upload_bytes:
                raise too_big(user, safe_name(f.name))
        out = []
        for f in body.files:
            upload_id, name = uuid.uuid4().hex[:8], safe_name(f.name)
            uploads.put(upload_id, {"owner": user, "name": name, "size": f.size})
            out.append({"id": upload_id, "name": name} | link(upload_dir() / upload_id / name))
        return {"uploads": out}

    @api.post("/api/uploads/{upload_id}/link")
    def renew_link(upload_id: str, request: Request):
        """A fresh link for an upload whose link ran out before the file got there."""
        if not direct():
            raise HTTPException(404, "Uploads go through /api/analyze on this server.")
        path = upload_path(upload_id, request.state.user)
        uploads.put(upload_id, uploads.get(upload_id))  # restarts the clock for `prune_uploads`
        return {"id": upload_id, "name": path.name} | link(path)

    @api.post("/api/analyze/uploaded")
    def analyze_uploaded(request: Request, ids: list[str] = Form(...), background: bool = False,
                         card: list[UploadFile] | None = File(None)):
        """Analyze files the page uploaded to the bucket with /api/uploads links; the same answer as /api/analyze.
        `ids`: the uploads' ids. `card`: as for /api/analyze, with a single file."""
        if not direct():
            raise HTTPException(404, "Uploads go through /api/analyze on this server.")
        user = request.state.user
        paths = [upload_path(i, user) for i in ids]  # all of them are this person's before anything is touched
        check_upload(user, [p.name for p in paths])
        kept: list[str] = []
        for upload_id, path in zip(ids, paths):
            size = blobs.arrived(path)
            if size is None:
                raise HTTPException(409, f"{path.name} didn't reach storage. Upload it again.")
            if size > ctx.settings.max_upload_bytes:
                give_up(ids, paths)
                raise too_big(user, path.name)
            try:
                blobs.ensure(path)  # tags, length and the file's hash are read from a local copy (the disk is a cache)
            except Exception:
                log.exception("couldn't read back the upload %s", upload_id)
                give_up(ids, paths)
                raise HTTPException(503, f"Couldn't read {path.name} back from storage. Upload it again.") from None
            kept.append(str(path))
            try:
                check_minutes(request, kept, path)
            except HTTPException:
                give_up(ids, paths)
                raise
        for upload_id in ids:
            uploads.remove(upload_id)
        sig = save_card(card[0], paths[0]) if ctx.browser_engine and card and len(card) == len(paths) == 1 else {}
        return begin(request, kept, sig, background)

    def give_up(ids: list[str], paths: list[Path]) -> None:
        """A refused direct upload: nothing of it stays, here or in the bucket."""
        for upload_id, p in zip(ids, paths):
            uploads.remove(upload_id)
            try:
                blobs.remove(p)
            except Exception:  # noqa: BLE001 - `prune_uploads` can't see it any more; the expiry sweep will
                log.warning("couldn't remove %s from the audio bucket", p.name)
            if p.parent.is_dir() and not any(p.parent.iterdir()):
                p.parent.rmdir()

    # ---- shared by both kinds of upload ---------------------------------------------------------------------

    def too_big(user: str, name: str) -> HTTPException:
        return ctx.refuse(user, 413, f"{name} is larger than {ctx.settings.max_upload_mb:g} MB.",
                          f"upload size limit ({ctx.settings.max_upload_mb:g} MB)", name)

    def check_minutes(request: Request, paths: list[str], dest: Path) -> None:
        """Refuse a track longer than TASTR_MAX_MINUTES (hosted), dropping `paths` (this request's files so far)."""
        minutes = audio_minutes(dest) if ctx.hosted else None
        if minutes is not None and minutes > ctx.settings.max_minutes:
            discard(map(Path, paths))
            raise ctx.refuse(request.state.user, 413,
                             f"{dest.name} is {minutes:.0f} minutes long. This server analyzes tracks up to "
                             f"{ctx.settings.max_minutes:g} minutes, so a long mix needs cutting into shorter files first.",
                             f"track length limit ({ctx.settings.max_minutes:g} min)", f"{dest.name}, {minutes:.0f} min")

    def begin(request: Request, paths: list[str], sig: dict[str, str], background: bool):
        """Analyze stored uploads: a background job to poll, or the results at once."""
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
            if p.parent.is_dir() and not any(p.parent.iterdir()):
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

    def check_upload(user: str, files: list[str]) -> None:
        """The hosted limits on one upload of `files` (their names): how many, which kinds, the storage cap and
        the person's daily track limit."""
        if len(files) > ctx.settings.max_files:
            raise ctx.refuse(user, 400, f"Upload at most {ctx.settings.max_files} files at a time.",
                         f"files-per-upload limit ({ctx.settings.max_files})")
        for f in files:
            if Path(f).suffix.lower() not in AUDIO_EXTENSIONS:
                raise HTTPException(415, f"{Path(f or 'upload').name} isn't an audio file Tastr can read.")
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
