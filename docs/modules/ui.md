# Module: web UI (drop zone, track cards, visualizations)

## Goal
A local web app (`tastr serve`) where you drop files or paste links, watch
them analyze, and browse track cards with charts. Plus a small explore view
across the library.

## Files
- Edit: `tastr/web/server.py` (keep `create_app(db_path)` and `run(host, port, db_path)`).
- New: `tastr/web/static/` (`index.html`, `app.js`, `style.css`). Plain HTML/JS, no build step; load chart libs only from a CDN with pinned versions, or draw with SVG/canvas.
- New: `tests/test_web.py`.

## API (JSON). Existing endpoints must keep their shape.
| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/api/health` | | `{"ok": true}` (exists) |
| GET | `/api/tracks` | | list of `db.list_tracks` rows (exists) |
| GET | `/api/tracks/{id}` | | `{"id": ..., **card, "has_audio", "has_views"}`; `extras.views` is left out (exists) |
| GET | `/api/tracks/{id}/audio` | | the original file, with HTTP Range support; 404 when it isn't on disk |
| GET | `/api/tracks/{id}/views` | | `extras["views"]` (waveform + spectrogram data); 404 for cards analyzed before the `views` stage, where the UI computes both from the decoded audio |
| POST | `/api/analyze` | multipart `files[]` | `{"results": [{"track_id", "title", "error"}]}`; saves each upload as `$TASTR_HOME/uploads/<random id>/<original name>` then calls `service.analyze_inputs` |
| POST | `/api/analyze-url` | `{"url": "..."}` | same shape; `IngestError` → 400 with the message |
| DELETE | `/api/tracks/{id}` | | 204 / 404 |
| POST | `/api/query` | `{"sql": "..."}` | `{"columns": [...], "rows": [[...]]}`; opens the DB with `db.connect(path, readonly=True)` so writes fail; 400 on SQL error |
| GET | `/` | | the static UI |

Analysis is CPU-bound and slow (tens of seconds); run it in FastAPI's threadpool (plain `def` endpoint) so the server stays responsive.

## Track card view (one per track)
Render from the card JSON, tolerating any field being null/empty:
- Header: title, duration (m:ss), BPM (1 decimal) with confidence, key name + Camelot + scale, energy meter 0..1.
- Energy curve: `series.energy` as a line/area over time, with `sections` as shaded bands labeled `label letter`.
- Tags as chips sized or shaded by score.
- Drum grid: from any suggestion whose `data.pattern` exists, a 16-column grid per lane (`x` full, `o` half, `.` empty), beats 1-4 visually grouped.
- Suggestions grouped by `category`, title bold, body below.
- Errors: if `errors` is non-empty, a muted note listing failed stages.

## Explore view
- Scatter of BPM (x) vs energy (y) for all tracks, point label on hover, click opens the card.
- Filter by tag and BPM range.
- A SQL box posting to `/api/query`, results as a table, with 3 example queries from `docs/ARCHITECTURE.md` as buttons.

## Tests (`tests/test_web.py`) with `fastapi.testclient.TestClient`
Seed the DB with `db.save_card` (see `full_card()` in `tests/test_foundation.py`) instead of running analysis, except one upload test using the synth wav.

## Acceptance criteria
1. Upload of the synth wav via `/api/analyze` returns a track id and the track appears in `/api/tracks`.
2. `/api/query` with `DELETE FROM tracks` returns 400 and the row is still there.
3. `/` serves HTML that references the static JS/CSS and those return 200.
4. The card view renders a card whose `core`, `sections`, `tags` and `suggestions` are all empty without JS errors (check with Playwright: `require('playwright')` works; capture `page.on('pageerror')`).
5. Layout works at 375 px width with no horizontal scroll.
6. `python -m pytest -q` passes.
