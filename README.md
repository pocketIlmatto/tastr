# Tastr

Drop in audio files (or, soon, track/playlist links) and get a **track card**
per track: BPM, estimated key, energy, rough section timestamps, vibe tags,
and "Try this in Ableton" ideas (drum patterns, bass movement, chord-scale
guesses). Results go into a SQLite database you can query, chart, or open in
DuckDB.

> Status: foundation only. The pipeline, card schema, storage and CLI work end
> to end, but the analysis stages are stubs. See `docs/modules/` for what each
> module will do.

## Quick start

```bash
pip install -e '.[dev]'          # add ,links for yt-dlp link support later
tastr analyze ~/Music/some-track.wav ~/Music/folder/
tastr list
tastr show 1                     # full card as JSON
tastr query "SELECT title, bpm, camelot FROM tracks ORDER BY bpm"
tastr stages                     # pipeline stages in run order
tastr serve                      # web UI (in progress)
python -m pytest -q
```

The database lives at `~/.tastr/tastr.db` (override with `--db` or `TASTR_DB`).

## Layout

```
tastr/card.py        TrackCard dataclasses: the contract between modules
tastr/pipeline.py    stage registry, dependency ordering, run_pipeline()
tastr/signals.py     shared intermediates (HPSS, onset envelope, chroma, RMS)
tastr/stages/        core, sections, tags, suggestions (one module each)
tastr/ingest.py      files/folders -> Sources, audio decoding, URL resolver registry
tastr/resolvers/     link/playlist resolvers
tastr/db.py          SQLite schema and save/load/query
tastr/service.py     inputs -> pipeline -> DB, used by the CLI and web app
tastr/web/           FastAPI app and UI
tests/synth.py       synthetic tracks with known BPM/key/sections for tests
```

Read `docs/ARCHITECTURE.md` for the data flow, card fields, feature names and
DB tables, and `docs/modules/` for per-module specs.
