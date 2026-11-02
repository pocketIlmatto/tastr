"""The hosted server's background work: deleting expired audio, logging GitHub Actions runs and R2 backups
(watch.py), and the admin log lines a restart writes."""

from __future__ import annotations

import os
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Callable

from .. import blobs, db, runner
from . import watch
from .analysis_jobs import ref
from .context import AppContext, log, tastr_home, upload_dir
from .hosted import expire_audio, names
from .routes.analyze import prune_uploads

EXPIRY_EVERY = 6 * 3600  # seconds between sweeps for expired audio on a hosted server


def start(ctx: AppContext) -> list[Callable[[], None]]:
    """Start a hosted server's repeating work (tastr/runner.py: threads, or the Worker's alarm); returns the
    functions that stop it."""
    settings, every, stops = ctx.settings, runner.current().every, []
    if settings.audio_days > 0:
        stops.append(every("expiry", EXPIRY_EVERY, lambda: expire_once(ctx)))
    if settings.watch_minutes > 0 and ((settings.github_repo and settings.github_workflows)
                                       or os.environ.get("LITESTREAM_BUCKET")):
        # The first check waits for the server to finish starting (and Litestream its first sync).
        stops.append(every("watch", settings.watch_minutes * 60, lambda: watch_once(ctx), delay=60))
    return stops


def expire_once(ctx: AppContext) -> None:
    """Delete expired audio, abandoned direct uploads and local copies of what the audio bucket holds."""
    settings = ctx.settings
    try:
        with closing(ctx.conn()) as c:
            gone = expire_audio(c, upload_dir(), settings.audio_days, settings.admins)
        ctx.snapshot_storage()
        abandoned = prune_uploads(ctx.db_path)  # direct uploads given a link and never finished
        if abandoned:
            log.info("Deleted %d unfinished upload(s)", abandoned)
        evicted = blobs.evict()  # local copies of what the audio bucket holds; the disk is a cache then
        if evicted:
            log.info("Removed %d local copies of files kept in the audio bucket", evicted)
        if gone:
            log.info("Deleted the audio of %d track(s) older than %g days", len(gone), settings.audio_days)
    except Exception:  # noqa: BLE001 - try again next time
        log.exception("Deleting expired audio failed")


def watch_once(ctx: AppContext, fetch_runs=watch.fetch_runs, snapshots=watch.litestream_snapshots) -> None:
    """Log GitHub Actions runs on main and Litestream snapshots that are new since the last check."""
    try:
        _watch(ctx, fetch_runs, snapshots)
    except Exception:  # noqa: BLE001 - try again next time
        log.exception("Checking GitHub and the R2 backup failed")


def _watch(ctx: AppContext, fetch_runs, snapshots) -> None:
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
        if os.environ.get("LITESTREAM_BUCKET"):
            snaps, error = None, None
            try:
                snaps = snapshots(str(ctx.db_path or db.default_db_path()))
            except Exception as e:  # noqa: BLE001 - logged as a problem line, once a day
                error = str(e) or type(e).__name__
            lines, newest, day = watch.backup_events(snaps, error, db.get_meta(c, watch.LAST_SNAPSHOT),
                                                     db.get_meta(c, watch.PROBLEM_DAY), now)
            for e in lines:
                db.add_event(c, **e)
            db.set_meta(c, watch.LAST_SNAPSHOT, newest)
            db.set_meta(c, watch.PROBLEM_DAY, day)


def log_start(ctx: AppContext, cut: list[dict[str, Any]]) -> None:
    """Admin log lines when the server starts: a new version deployed, and each analysis a restart cut off.
    A plain wake-up after Fly stopped an idle machine logs nothing."""
    settings = ctx.settings
    marker, version = tastr_home() / "server-version", settings.fly_image or settings.commit
    host = "flyio" if settings.fly_app else "app"
    try:
        before = marker.read_text().strip()
    except OSError:
        before = None
    deployed = bool(version) and before is not None and before != version
    if version and before != version:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(version)
        except OSError as e:  # without the marker the next start logs this deploy again, so say why
            log.warning("can't record the deployed version in %s: %s", marker, e)
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
