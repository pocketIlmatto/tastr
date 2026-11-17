"""The hosted server's background work: deleting expired audio, logging GitHub Actions runs (watch.py), and the
admin log lines a restart writes."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Callable

from .. import backfill, blobs, db, runner
from . import watch
from .analysis_jobs import ref
from .context import AppContext, log, upload_dir
from ..markers import Markers
from .hosted import expire_audio, names, orphan_uploads
from .routes.analyze import UPLOAD_MARKERS, prune_uploads

EXPIRY_EVERY = 6 * 3600  # seconds between sweeps for expired audio on a hosted server


def start(ctx: AppContext) -> list[Callable[[], None]]:
    """Start a hosted server's repeating work (tastr/runner.py: threads, or the Worker's alarm); returns the
    functions that stop it."""
    settings, every, stops = ctx.settings, runner.current().every, []
    # Always: besides expired audio (TASTR_AUDIO_DAYS, or a person's own days from the Admin tab) the sweep
    # clears unfinished uploads, orphaned files and the bucket's local cache.
    stops.append(every("expiry", EXPIRY_EVERY, lambda: expire_once(ctx)))
    if settings.watch_minutes > 0 and settings.github_repo and settings.github_workflows:
        # The first check waits for the server to finish starting.
        stops.append(every("watch", settings.watch_minutes * 60, lambda: watch_once(ctx), delay=60))
    return stops


def expire_once(ctx: AppContext) -> None:
    """Delete expired audio, abandoned direct uploads, uploads no track uses and local copies of what the audio
    bucket holds, and move waveform data still kept in cards to the bucket."""
    settings = ctx.settings
    try:
        with closing(ctx.conn()) as c:
            gone = expire_audio(c, upload_dir(), settings.audio_days, settings.admins, per_person=db.audio_days(c))
        ctx.snapshot_storage()
        abandoned = prune_uploads(ctx.db_path)  # direct uploads given a link and never finished
        if abandoned:
            log.info("Deleted %d unfinished upload(s)", abandoned)
        with closing(ctx.conn()) as c:  # copies nothing points at (a file uploaded again, before TAS-94)
            orphans = orphan_uploads(c, upload_dir(), {k for k, _, _ in Markers(ctx.db_path, UPLOAD_MARKERS).items()})
        if orphans:
            log.info("Deleted %d uploaded file(s) no track uses", orphans)
        evicted = blobs.evict()  # local copies of what the audio bucket holds; the disk is a cache then
        if evicted:
            log.info("Removed %d local copies of files kept in the audio bucket", evicted)
        with closing(ctx.conn()) as c:  # waveform data a card kept because the bucket couldn't take it then
            moved = backfill.move_views(c)
        if moved["views"]:
            log.info("Moved %d card(s)' waveform data to the audio bucket", moved["views"])
        if gone:
            log.info("Deleted the audio of %d track(s) past their owner's expiry", len(gone))
    except Exception:  # noqa: BLE001 - try again next time
        log.exception("Deleting expired audio failed")


def watch_once(ctx: AppContext, fetch_runs=watch.fetch_runs) -> None:
    """Log GitHub Actions runs on main that are new since the last check."""
    try:
        _watch(ctx, fetch_runs)
    except Exception:  # noqa: BLE001 - try again next time
        log.exception("Checking GitHub failed")


def _watch(ctx: AppContext, fetch_runs) -> None:
    settings = ctx.settings
    now = datetime.now(timezone.utc)
    with closing(ctx.conn()) as c:
        if settings.github_repo and settings.github_workflows:
            try:
                runs = fetch_runs(settings.github_repo, settings.github_token)
            except Exception as e:  # noqa: BLE001 - offline, rate-limited or a bad token: try next time
                log.warning("Checking GitHub Actions failed: %s", e)
            else:
                last = db.get_meta(c, watch.LAST_RUN)
                lines, newest = watch.run_events(runs, int(last) if last else None, now, settings.github_workflows)
                for e in lines:
                    db.add_event(c, **e)
                if newest:
                    db.set_meta(c, watch.LAST_RUN, str(newest))


SERVER_VERSION = "server_version"  # meta: the commit that last started


def log_start(ctx: AppContext, cut: list[dict[str, Any]]) -> None:
    """Admin log lines when the server starts: a new version deployed, and each analysis a restart cut off.
    A plain wake-up after an idle stop (the Worker's Durable Object) logs nothing."""
    settings = ctx.settings
    version = settings.commit
    host = "cloudflare" if settings.worker_name else "app"
    # The version that last started, in the database: it outlives a restart (the Worker's files don't).
    with closing(ctx.conn()) as c:
        before = db.get_meta(c, SERVER_VERSION)
        if version and before != version:
            db.set_meta(c, SERVER_VERSION, version)
    deployed = bool(version) and before is not None and before != version
    if deployed:
        what = f"{settings.commit[:7]}: {settings.commit_subject[:100]}" if settings.commit_subject else settings.commit[:7]
        ctx.event(None, "deploy", f"Deployed {what or 'a new version'} (the server restarted)", link=settings.deploy_run or None,
              source=host)
    elif cut:
        ctx.event(None, "restart", "The server restarted in the middle of an analysis (it crashed or its machine was stopped)",
              ok=False, source=host)
    for job in cut:
        ctx.event(job.get("owner"), "failed", f"Cut off by the restart: {names(job.get('files') or ['upload'])}, "
              f"while at step {job.get('stage') or 'waiting'} (ref {ref(job['id'])})", ok=False)
