"""One track: the library list, its card, views, tags, re-analysis, lookups and deleting it."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from ... import blobs, db, lookup, service, stems
from ..context import AppContext, _failure_reason, _unprefixed, log, upload_dir


def router(ctx: AppContext) -> APIRouter:
    api = APIRouter()

    @api.get("/api/bench")
    def bench_scores():
        # Headline benchmark scores the Glossary quotes; `tastr bench ... --record` keeps the file current.
        from ...engine.bench import scores

        return scores.load()

    @api.get("/api/patterns")
    def patterns():
        # The engine's template drum patterns per style, for the Glossary's pattern grids.
        from ...engine.patterns import STYLE_LABELS, VARIATIONS

        return [{"style": STYLE_LABELS[style], "name": name, "pattern": pat}
                for style, items in VARIATIONS.items() for name, pat in items]

    @api.get("/api/tracks")
    def tracks(request: Request):
        with closing(ctx.conn()) as c:
            rows = db.list_tracks(c, ctx.owner_of(request))
            uris = dict(c.execute("SELECT id, source_uri FROM tracks").fetchall())
            vibes: dict[int, list[str]] = {}
            for tid, tag in c.execute("SELECT track_id, tag FROM tags ORDER BY score DESC"):
                vibes.setdefault(tid, []).append(tag)
        for r in rows:  # tracks uploaded before the hash moved into a folder still have it in their title
            r["title"] = _unprefixed(r["title"], uris.get(r["id"]))
            r["tags"] = vibes.get(r["id"], [])
        return rows

    @api.get("/api/tracks/{track_id}")
    def track(track_id: int):
        with closing(ctx.conn()) as c:
            return ctx.card_body(c, track_id)

    @api.get("/api/tracks/{track_id}/views")
    def track_views(track_id: int):
        """Waveform and spectrogram data from the `views` stage (404 for cards analyzed before it existed)."""
        with closing(ctx.conn()) as c:
            card = db.load_card(c, track_id)
        if card is None:
            raise HTTPException(404, "no such track")
        views = card.extras.get("views")
        if views is None:
            raise HTTPException(404, "This track was analyzed before waveform data was stored.")
        return views

    @api.post("/api/tracks/{track_id}/retag")
    def retag(track_id: int):
        """Re-read one file track's embedded tags and audio info (`tastr retag` for one track)."""
        with closing(ctx.conn()) as c:
            result = db.retag_track(c, track_id)
            if result == "missing":
                raise HTTPException(404, "no such track")
            if result == "not_file":
                raise HTTPException(400, "Only uploaded files have tags to reload.")
            if result == "no_file":
                raise HTTPException(410, "The original file is no longer on disk.")
            body = ctx.card_body(c, track_id)
        return {"changed": result == "changed", "tags_found": bool(body["source"].get("file_tags")), "card": body}

    @api.post("/api/tracks/{track_id}/reanalyze")
    def reanalyze(track_id: int, request: Request, meter: str | None = None):
        """Run the full engine again on the track's stored audio file (same id, fresh card).

        `?meter=3/4` or `4/4` sets the time signature, `auto` goes back to detecting it; without it a meter
        the user set before is kept.
        """
        if meter not in (None, "3/4", "4/4", "auto"):
            raise HTTPException(400, "meter must be 3/4, 4/4 or auto")
        beats = service.KEEP if meter is None else None if meter == "auto" else int(meter[0])
        with closing(ctx.conn()) as c:
            try:
                with ctx.analysis_slot():
                    result = service.reanalyze_track(c, track_id, beats)
            except FileNotFoundError:
                raise HTTPException(410, "The original audio file is no longer on disk.") from None
            except TimeoutError:
                raise HTTPException(503, "The server is busy analyzing other tracks. Try again in a few minutes.") from None
            if result is None:
                raise HTTPException(404, "no such track")
            if result.error:
                if ctx.hosted:
                    log.warning("re-analysis of track %s failed: %s", track_id, result.error)
                    ctx.event(request.state.user, "failed", f"Re-analysis failed, {_failure_reason(result.error)}", ok=False)
                    raise HTTPException(422, "Couldn't analyze the file.")
                raise HTTPException(422, f"Couldn't analyze the file: {result.error}")
            ctx.event(ctx.owner_of(request), "reanalyze", "Re-analyzed 1 track")
            return {"card": ctx.card_body(c, track_id), "edits_dropped": result.edits_dropped}

    @api.post("/api/tracks/{track_id}/lookup")
    def lookup_metadata(track_id: int):
        """Ask the enabled catalogs (TASTR_LOOKUP) for this track's BPM and key again."""
        providers = lookup.enabled()
        if not providers:
            raise HTTPException(400, "No lookup providers are set up. See docs/lookup.md.")
        with closing(ctx.conn()) as c:
            card = db.load_card(c, track_id)
            if card is None:
                raise HTTPException(404, "no such track")
            if lookup.query_for(card) is None:
                raise HTTPException(422, "This track has no artist and title tags to look up.")
            results = lookup.lookup_track(c, track_id, card, providers)
            body = ctx.card_body(c, track_id)
            # Answers a provider's terms don't let us keep are shown this once, not stored.
            body["lookups"] += lookup.describe(card, [r for r in results if r.get("transient")])
            # "limited" (a guardrail held the request back) and "recent" (asked within the refresh window)
            # aren't stored; the UI says why nothing new came back.
            notes = [{"provider": r["provider"], "status": r["status"], "error": r.get("error")}
                     for r in results if r["status"] in ("limited", "recent", "error")]
            return {"card": body, "notes": notes}

    @api.delete("/api/tracks/{track_id}", status_code=204)
    def delete_track(track_id: int):
        with closing(ctx.conn()) as c:
            card = db.load_card(c, track_id)
            if not db.delete_track(c, track_id):
                raise HTTPException(404, "no such track")
            # Stems are kept per audio file, so another library holding the same file keeps them.
            shared = card is not None and c.execute(
                "SELECT 1 FROM tracks WHERE sha256 = ?", (card.source.sha256,)).fetchone() is not None
        ctx.drum_runs.forget(track_id)
        if card is not None and not shared:
            folder = ctx.stems_dir(card, track_id)
            ctx.stem_jobs.forget(folder)
            stems.remove(folder)
        if ctx.hosted and card is not None:
            remove_upload(card.source.local_path)
        return Response(status_code=204)

    def remove_upload(path: str | None) -> None:
        """A deleted track's uploaded audio goes too (hosted only: on your own machine Tastr leaves files alone).
        Only Tastr's own copies under uploads/, and only when no other track points at the same file."""
        if not path:
            return
        p, root = Path(path).resolve(), upload_dir().resolve()
        if root not in p.parents:
            return
        with closing(ctx.conn()) as c:
            if c.execute("SELECT 1 FROM tracks WHERE local_path = ?", (path,)).fetchone():
                return
        blobs.remove(p)  # here and in the audio bucket
        if p.parent != root and p.parent.is_dir() and not any(p.parent.iterdir()):
            p.parent.rmdir()

    return api
