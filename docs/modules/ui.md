# Module: web UI (drop zone, track cards, visualizations)

## Goal
A local web app (`tastr serve`) where you drop files or paste links, watch
them analyze, and browse track cards with charts. Plus a small explore view
across the library.

## Files
- Edit: `tastr/web/server.py` (keep `create_app(db_path)` and `run(host, port, db_path)`). Since TAS-34 the routes live in
  `tastr/web/routes/` (one module per area, each a `router(ctx)`), sharing an `AppContext` from `tastr/web/context.py`;
  the hosted background loops are in `tastr/web/background.py`.
- New: `tastr/web/static/` (`index.html`, `app.js`, `style.css`). Plain HTML/JS, no build step; load chart libs only from a CDN with pinned versions, or draw with SVG/canvas.
- New: `tests/test_web.py`.

## API (JSON). Existing endpoints must keep their shape.
| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/api/health` | | `{"ok": true}` (exists) |
| GET | `/api/tracks` | | list of `db.list_tracks` rows (exists) |
| GET | `/api/tracks/{id}` | | `{"id": ..., **card, "has_audio", "has_views"}`; `extras.views` is left out (exists) |
| GET | `/api/tracks/{id}/audio` | | the original file, with HTTP Range support; 404 when it isn't on disk |
| GET | `/api/tracks/{id}/strudel-link` | | `{"audio", "days"}`: where strudel.cc can fetch the audio (stems at `.../stems/<name>` next to it). Locally `/api/tracks/{id}/audio`; hosted, a signed `/pub/<token>/tracks/{id}/audio` that works for `days` days without the login (docs/hosting.md) |
| GET | `/api/tracks/{id}/views` | | `extras["views"]` (waveform + spectrogram data); 404 for cards analyzed before the `views` stage, where the UI computes both from the decoded audio |
| POST | `/api/analyze` | multipart `files[]` | `{"results": [{"track_id", "title", "error"}]}`; saves each upload as `$TASTR_HOME/uploads/<random id>/<original name>` then calls `service.analyze_inputs` |
| POST | `/api/analyze?background=1`, `/api/analyze-url?background=1` | same | 202 `{"id", "state", "stage", "file_index", "file_count"}` right after the upload is stored; the analysis runs in the background (tastr/web/analysis_jobs.py) |
| GET | `/api/analyze/jobs/{id}` | | `{"state": "queued"\|"running"\|"done"\|"error", "stage", "position"?, "results"?, "error"?}`; `stage` is waiting, fetch, decode or an engine stage; a failed result names the `stage` it stopped at. 404 for an unknown job or someone else's (hosted). A job a restarted server left unfinished reads as an error |
| POST | `/api/analyze-url` | `{"url": "..."}` | same shape; `IngestError` → 400 with the message; anything but an `http(s)` link (a server path) → 400; 403 in hosted mode |
| GET | `/api/config` | | `{"hosted", "links", "query", "user", "admin", "limits"}`: what this server allows, so the UI hides link analysis and the SQL box when they're off (docs/hosting.md) |
| GET | `/api/admin/usage` | | hosted mode, `TASTR_ADMIN_EMAILS` only (404 otherwise): `{"today", "users": [{"email", "tracks", "audio_bytes", "stems_bytes", "last_upload", "last_seen", "today": {"tracks", "stems", "visits"}}], "history": [{"day", "user", "kind", "n"}], "admins", "limits", "storage": {"used_bytes", "cap_bytes"}, "active_stem_jobs", "analyses": {"running", "waiting", "max"}, "recent": [{"at", "user", "kind", "ok", "detail"}], "blocked_today"}`; `email` is null for tracks with no owner |
| DELETE | `/api/tracks/{id}` | | 204 / 404; also deletes the track's edits and versions |
| GET / PUT | `/api/tracks/{id}/edits` | PUT: the whole edits object | the current, autosaved edits to the drums, bass and chords, `{drums\|bass\|chords\|drumsrc: {section idx or "all": value}}`, or `{}`; PUT returns 204 (an object with no edited section clears them) |
| GET / POST | `/api/tracks/{id}/edit-versions` | POST: `{"name", "edits"}` | saved versions, newest first: `[{"id", "name", "created_at", "sections"}]`; POST returns the new one (201) |
| GET / DELETE | `/api/tracks/{id}/edit-versions/{vid}` | | one version with its `edits` / 204; 404 for an unknown track or version |
| POST | `/api/tracks/{id}/reanalyze` | | `{"card", "edits_dropped"}`. Edits move to the new section with the same start and end bars; edits whose section is gone are dropped (in versions too), and `edits_dropped` counts them |
| GET | `/api/bench` | | the benchmark scores the Glossary quotes, from `tastr/engine/bench_scores.json` (`{"scores": {key: {"exact", "tracks", "what", "run"}}}`); `{"scores": {}}` when the file is missing |
| GET | `/api/patterns` | | the engine's template drum patterns, `[{"style", "name", "pattern"}]`, for the Glossary's pattern grids |
| POST | `/api/query` | `{"sql": "...", "mine": false}` | `{"columns": [...], "rows": [[...]]}`; one read-only SELECT (`db.run_query`), 400 on SQL error or when it runs too long. Hosted: admins query the whole database unless `mine` is true (the Simple search sends it, since its rows open cards); everyone else a copy of their own library (docs/hosting.md) |
| GET / PUT | `/api/searches` | PUT: `{"mode": "simple"\|"sql", "name", "data"}` | your saved Find tracks searches, oldest first: `[{"id", "mode", "name", "data"}]` (`data` is `{groups, join, sort}` or `{sql}`); PUT returns the saved one, replacing one with the same name (any case) in the same mode. Each person's own when hosted |
| DELETE | `/api/searches/{id}` | | 204 / 404 (also for someone else's) |
| GET | `/` | | the static UI |

Analysis is CPU-bound and slow (tens of seconds); run it in FastAPI's threadpool (plain `def` endpoint) so the server stays responsive.

## Track card view (one per track)
Render from the card JSON, tolerating any field being null/empty:
- Header: title, duration (m:ss), BPM (1 decimal) with "N% likely right" (`core.bpm_likely_correct`; older cards show the raw `bpm_confidence` as "N% conf"), key name + Camelot + scale + "N% likely right" (`key.likely_correct`, hidden when absent), energy meter 0..1.
- Energy curve: `series.energy` as a line/area over time, with `sections` as shaded bands labeled `label letter`.
- Tags as chips sized or shaded by score.
- Drum grid: from any suggestion whose `data.pattern` exists, a 16-column grid per lane (`x` full, `o` half, `.` empty), beats 1-4 visually grouped.
- Suggestions grouped by `category`, title bold, body below.
- Errors: if `errors` is non-empty, a muted note listing failed stages.

## Explore view
- Scatter of BPM (x) vs energy (y) for all tracks, point label on hover, click opens the card.
- Filter by tag and BPM range.
- **Find tracks**, with a Simple / SQL toggle (remembered in localStorage `tastr-query-mode`):
  - Simple: conditions (BPM, key or the keys that mix with it, energy, vibe, length, section, text) in groups one level deep; AND/OR inside each group and between groups. The page builds the SQL (`qSql()` in app.js) and runs it through `/api/query` with `mine: true`; "Edit as SQL" opens that SQL in the SQL box.
  - SQL: the box posting to `/api/query`, and a "Tables and columns" reference (`Q_SCHEMA`; a friend's copy leaves out the private columns).
  - Saved searches as chips (`/api/searches`), and built-in examples in an "Examples ▾" menu, separate for each mode.

## Admin view (hosted mode, admins only)
- The Admin header button shows only when `/api/config` says `"admin": true`; `#admin` opens it, and falls back to the Library for everyone else.
- Fetches `/api/admin/usage` each time it opens (no polling): tiles, a stacked 30-day chart per person (Tracks / Stem jobs), the people table (click or Enter picks a person out in the chart), the activity log, limits and the blocked-request count. The 30-day cloud cost is estimated in the client (`ADM_TRACK_COST`, `ADM_STEM_COST`, `ADM_BUDGET` in app.js).

## Tests (`tests/test_web.py`) with `fastapi.testclient.TestClient`
Seed the DB with `db.save_card` (see `full_card()` in `tests/test_foundation.py`) instead of running analysis, except one upload test using the synth wav.

## Acceptance criteria
1. Upload of the synth wav via `/api/analyze` returns a track id and the track appears in `/api/tracks`.
2. `/api/query` with `DELETE FROM tracks` returns 400 and the row is still there.
3. `/` serves HTML that references the static JS/CSS and those return 200.
4. The card view renders a card whose `core`, `sections`, `tags` and `suggestions` are all empty without JS errors (check with Playwright: `require('playwright')` works; capture `page.on('pageerror')`).
5. Layout works at 375 px width with no horizontal scroll.
6. `python -m pytest -q` passes.
