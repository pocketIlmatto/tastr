"""The hosted server's background work: deleting expired audio, logging GitHub Actions runs and R2 backups
(watch.py), and the admin log lines a restart writes."""

from __future__ import annotations

import os
import threading
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from .. import db
from . import watch
from .analysis_jobs import ref
from .context import AppContext, log, tastr_home, upload_dir
from .hosted import expire_audio, names

EXPIRY_EVERY = 6 * 3600  # seconds between sweeps for expired audio on a hosted server


def expire_loop(ctx: AppContext, stop: threading.Event) -> None:
    settings = ctx.settings
    while True:
        try:
            with closing(ctx.conn()) as c:
                gone = expire_audio(c, upload_dir(), settings.audio_days, settings.admins)
            ctx.snapshot_storage()
            if gone:
                log.info("Deleted the audio of %d track(s) older than %g days", len(gone), settings.audio_days)
        except Exception:  # noqa: BLE001 - try again next time
            log.exception("Deleting expired audio failed")
        if stop.wait(EXPIRY_EVERY):
            return


def watch_once(ctx: AppContext, fetch_runs=watch.fetch_runs, snapshots=watch.litestream_snapshots) -> None:
    """Log GitHub Actions runs on main and Litestream snapshots that are new since the last check."""
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


def watch_loop(ctx: AppContext, stop: threading.Event) -> None:
    if stop.wait(60):  # let the server finish starting (and Litestream its first sync)
        return
    while True:
        try:
            watch_once(ctx)
        except Exception:  # noqa: BLE001 - try again next time
            log.exception("Checking GitHub and the R2 backup failed")
        if stop.wait(ctx.settings.watch_minutes * 60):
            return


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
        except OSError:
            pass
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
