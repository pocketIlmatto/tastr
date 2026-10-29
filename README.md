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

You need Python 3.10 or newer. Check with `python3 --version`. If `python3` is
not found, install Python from [python.org](https://www.python.org/downloads/)
or, on a Mac, run `brew install python`.

From the repo folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'    # add ,links for yt-dlp link support later
tastr serve
```

Then open http://127.0.0.1:8000 in your browser and drop in your audio files.
The web UI is where you analyze tracks and browse their cards.

## Command line

The `tastr` command does the same work from a terminal. Activate the virtualenv
from the Quick start first (`source .venv/bin/activate`).

```bash
tastr analyze ~/Music/some-track.wav ~/Music/folder/
tastr list
tastr show 1                     # full card as JSON
tastr query "SELECT title, bpm, camelot FROM tracks ORDER BY bpm"
tastr stages                     # pipeline stages in run order
tastr serve --host 127.0.0.1 --port 8000   # web UI (defaults shown)
tastr bench synth                # score the engine on labeled tracks (docs/bench.md)
```

The database lives at `~/.tastr/tastr.db`. Override it with `--db` or the
`TASTR_DB` environment variable. The web UI reads the same database.

To run the tests:

```bash
python -m pytest -q
```

## Layout

```
tastr/engine/               the analysis engine; imports nothing else from tastr
  card.py                   TrackCard dataclasses: the contract between modules
  pipeline.py               stage registry, dependency ordering, run_pipeline()
  audio.py                  decoding files to mono audio (librosa, ffmpeg fallback)
  signals.py                shared intermediates (HPSS, onset envelope, chroma, RMS)
  music.py                  key profiles, scales, chords, Camelot
  stages/                   core, sections, tags, suggestions (one module each)
  bench/                    accuracy benchmark: dataset loaders, metrics, runner
  bench/synth.py            synthetic tracks with known BPM/key/sections (tests/synth.py re-exports it)
tastr/ingest.py             files/folders/links -> Sources, URL resolver registry
tastr/resolvers/            link/playlist resolvers
tastr/db.py                 SQLite schema and save/load/query
tastr/service.py            inputs -> engine -> DB, used by the CLI and web app
tastr/web/                  FastAPI app and UI
```

The engine is kept self-contained so it can be split into its own library
later; `tests/test_engine.py` fails if it starts importing app code. Use it
directly with `from tastr.engine import analyze_file`.

Read `docs/ARCHITECTURE.md` for the data flow, card fields, feature names and
DB tables, and `docs/modules/` for per-module specs.
