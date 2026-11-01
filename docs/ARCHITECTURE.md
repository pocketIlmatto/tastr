# Tastr architecture

```
inputs (files, folders, links)
   │  tastr/ingest.py          resolve_inputs() -> [Source]   (links: tastr/resolvers/*)
   ▼
audio file  ──►  engine.analyze_file()  ──►  the Rust engine (engine-rs/, through engine/rust.py)
   │  core ──► sections ──► tags ──► suggestions        engine-rs/src/stages/*.rs
   │  views (independent: waveform + spectrogram data for the UI)
   ▼
TrackCard (tastr/engine/card.py)  ──►  SQLite (tastr/db.py)  ──►  CLI (tastr/cli.py) / web (tastr/web/)
```

`tastr/service.py` wires these together; CLIs and web handlers call
`service.analyze_inputs()` / `service.analyze_source()`. With `TASTR_ANALYSIS_BACKEND=modal` (a hosted server,
docs/deploy.md) `analyze_source()` sends the audio to the `analyze` function on Modal
(`tastr/cloud/modal_web.py`) and stores the card that comes back.

## The track card (`tastr/engine/card.py`)

The single contract between modules. Every stage writes only its own part:

| Field | Written by | Notes |
|---|---|---|
| `source` | ingest | `sha256` is set by the service before stages run |
| `duration`, `sample_rate` | pipeline | |
| `core` (`bpm`, `bpm_confidence`, `key`, `energy`, `loudness_db`, `beat_times`, `downbeat_times`) | `core` stage | `key` is a `Key(tonic, mode, scale, camelot, confidence)` |
| `sections` | `sections` stage | list of `Section` |
| `tags` | `tags` stage | list of `Tag(tag, score)`, scores of 0.20 and up. Every positive score, including the ones under 0.20, is in `extras["tags.vibe_scores"]` (`tag -> score`) |
| `suggestions` | `suggestions` stage | list of `Suggestion(category, title, body, data)` |
| `features` | any stage | flat `name -> float`; see the feature registry below |
| `series` | any stage | named curves `Series(times, values)` for plots |
| `extras` | any stage | structured JSON; keys prefixed with the stage name (`"suggestions.drum_grid"`). `extras["views"]` (the `views` stage) holds the waveform (int8 min/max per block, base64) and spectrogram (grayscale log-frequency PNG data URL); the web API serves it from `/api/tracks/{id}/views` and leaves it out of the card body |
| `stages_run`, `errors` | pipeline | a failed stage lands in `errors`; its dependents are skipped |

Changing an existing field is a schema change: bump `CARD_SCHEMA_VERSION`
and update `db.py` in the same PR. Adding new features / series / extras is
not, which is why those bags exist.

### Feature registry

Names stages agree on. Producers must emit them; consumers must tolerate them
being missing (a stage can fail or be a stub).

| Name | Range | Producer | Meaning |
|---|---|---|---|
| `brightness` | 0..1 | core | mean spectral centroid / 3500 Hz, clipped |
| `percussiveness` | 0..1 | core | percussive energy / total energy after HPSS |
| `low_end` | 0..1 | core | share of power below 150 Hz |
| `pulse_clarity` | 0..1 | core | how strongly onsets line up with beats |
| `danceability` | 0..1 | core | 0.75 percussiveness + 0.25 tempo fit (bell around 135 BPM), fitted in docs/vibes.md |
| `onset_rate` | per second | core | detected onsets per second |
| `dynamic_range_db` | dB | core | p95 - p10 of frame RMS in dB |
| `swing_percent` | 50..75 | core | where the off-beat 16th lands; 50 straight, ~66 triplet |
| `swing_confidence` | 0..1 | core | |
| `section_contrast` | 0..1 | sections | std of section energies |
| `section_count` | count | sections | |

Series: `energy` (per bar, 0..1, written by core) is the one the UI draws.

## Pipeline (`tastr/engine/pipeline.py`)

The Rust engine (`engine-rs/`, [rust-engine.md](rust-engine.md)) makes every card, drum-model cards included
(the server runs the model and hands Rust its hits). The Python reference engine below, which the Rust one was
ported from, keeps only what the benchmarks score: core and sections (`run_pipeline` over
`tastr/engine/stages/`) and the drum step grid (`drums.step_grid`, `drums.drum_grid`).

A stage is any object with `name`, `version`, `requires` (tuple of stage
names) and `run(ctx)`. `register_stage(obj)` at import time of a module in
`tastr/engine/stages/` makes it part of the default pipeline. `ctx` gives the audio
(`ctx.y`, `ctx.sr`, `ctx.hop`), the card (`ctx.card`) and a per-track cache
(`ctx.get(name, fn)`). Shared intermediates (HPSS, onset envelope, chroma,
RMS) live in `tastr/engine/signals.py`; use them so they are computed once.

Where the time goes (2026-10-31, a 3-minute synthetic track, 4 CPUs): the whole
analysis took 14.5 s, 10.8 s of it HPSS, and 7.9 s of that was librosa's two 2-D
median filters, which take scipy's slow general path. `signals.hpss` now filters
row by row on scipy's fast 1-D path, with the same output sample for sample
(checked against `librosa.effects.hpss`, and `tests/test_engine.py` keeps it so):
7.1 s in all, HPSS 3.4 s. Sharing the repeated STFTs would save only about 0.3 s more.

## Browser engine

`TASTR_BROWSER_ENGINE=1` (off by default): the page runs the Rust engine's
WebAssembly build (`/engine/tastr_engine.wasm`, built into the image) on the upload in a worker
(`tastr/web/static/engine.js`) and sends the finished card with the audio, so the server runs no analysis.
`tastr/engine/browser_card.py` takes the card only if it was made by the build the server serves now (a hash
of the wasm file), ran every stage at the server's versions and sample rate, and lasts as long as the file;
otherwise it is set aside (`extras["browser_card"]` says why) and the server analyzes as usual. Opus files,
tracks over 20 minutes, and browsers without WebAssembly leave the analysis to the server.

The server makes its own cards with the same engine, natively through ctypes (`tastr/engine/rust.py`), so
browser and server cards agree byte for byte. See [rust-engine.md](rust-engine.md). (Before the Rust engine,
`TASTR_BROWSER_SIGNALS` had the browser run only HPSS; the whole card replaced it.)
- On four synthetic tracks, the only differences from the server-only card were drum velocities, a few
  vibe scores in the third decimal and, on one track, one ghost note in a drum lane. All of them come from the
  int16 rounding: with float32 audio the cards matched except for one velocity. Float32 would double the
  upload, to 8 bytes a sample (about 32 MB for 3 minutes), so the format uses int16. Synthetic tracks have
  digital silence, where that rounding shows most.

## Storage (`tastr/db.py`)

SQLite, one file (default `~/.tastr/tastr.db`, override with `TASTR_DB` or
`--db`). Tables:

- `tracks`: one row per track, headline numbers as columns (bpm, key, camelot, energy...), and `created_at`, when it was uploaded (re-analysis keeps it). Deduped on `sha256` within one owner's library; `owner` is the uploader's Access email on a hosted server (docs/hosting.md) and NULL on your own machine.
- `sections`, `tags`, `suggestions`: one row per item, keyed by `track_id`.
- `features`: long format `(track_id, name, value)`, so new features need no migration.
- `series`: long format `(track_id, name, idx, t, value)` for curves.
- `cards`: the full card JSON, the source of truth for re-rendering.
- `lookups`: `(track_id, provider, status, bpm, key_name, fetched_at, result_json)`, what online catalogs list for the track (opt-in, `tastr/lookup/`, [lookup.md](lookup.md)). Kept across re-analysis.
- `track_labels`: `(track_id, bpm, key_name, boundaries, note, updated_at, kept, tastr_bpm, tastr_key)`, your own labels for a track (`tastr/labels.py`). `kept` marks a "Keep as is"; `tastr_bpm` / `tastr_key` are Tastr's reading when it was saved. Never touched by analysis; exported as a bench manifest.
- `review_flags`: `(track_id, state, updated_at)`, tracks you put in the review list (`flagged`) or took out of it (`skipped`). Kept across re-analysis.

Example queries:

```sql
-- Tracks that mix with 8A at 120-128 BPM
SELECT title, bpm, camelot FROM tracks WHERE camelot IN ('7A','8A','9A','8B') AND bpm BETWEEN 120 AND 128;
-- Dark, bass-heavy tracks
SELECT t.title FROM tracks t JOIN tags g ON g.track_id = t.id WHERE g.tag = 'dark'
  AND t.id IN (SELECT track_id FROM features WHERE name = 'low_end' AND value > 0.3);
-- Average drop length in bars
SELECT avg(end_bar - start_bar + 1) FROM sections WHERE label = 'drop';
```

DuckDB reads the same file: `ATTACH 'tastr.db' AS t (TYPE sqlite);`.

## Code layout

```
tastr/engine/               the analysis engine; imports nothing else from tastr
  card.py                   TrackCard dataclasses: the contract between modules
  pipeline.py               stage registry, dependency ordering, run_pipeline()
  audio.py                  decoding files to mono audio (librosa, ffmpeg fallback)
  signals.py                shared intermediates (HPSS, onset envelope, chroma, RMS)
  rust.py                   the Rust engine (engine-rs/) that makes every card, through ctypes
  browser_card.py           checks cards the browser made with the WebAssembly build (TASTR_BROWSER_ENGINE)
  music.py                  key estimation, scales, chords, Camelot
  key_model.py              learned key model constants (generated by bench/train_key.py)
  stages/                   the reference engine's core and sections stages (the benchmarks score them)
  patterns.py               the template drum patterns per style (the Glossary lists them)
  bench/                    accuracy benchmark: dataset loaders, metrics, runner
  bench/key_features.py     cache per-track key features; bench/train_key.py fits the key model
  bench/synth.py            synthetic tracks with known BPM/key/sections (tests/synth.py re-exports it)
tastr/ingest.py             files/folders/links -> Sources, URL resolver registry
tastr/resolvers/            link/playlist resolvers
tastr/lookup/               opt-in catalog lookups (BPM/key by artist + title), one module per provider
tastr/db.py                 SQLite schema and save/load/query
tastr/labels.py             your own BPM/key/section labels, the review queue, manifest export
tastr/service.py            inputs -> engine -> DB, used by the CLI and web app
tastr/web/                  FastAPI app and UI; hosted.py is the shared-server mode (docs/hosting.md)
  server.py                 create_app: the shared AppContext (context.py), login middleware, and the routes/ modules
  routes/                   the API by area: tracks, media (audio, stems, drum model), edits, analyze, search, admin
tastr/cloud/                Modal apps: benchmarks and stems (modal_app.py), a hosted server's analysis (modal_web.py)
engine-rs/                  the Rust engine that makes every card, on the server and in the browser (docs/rust-engine.md)
Dockerfile, fly.toml, deploy/   the hosted server's image, Fly.io config and Litestream backup (docs/deploy.md)
```

The engine is kept self-contained so it can be split into its own library
later; `tests/test_engine.py` fails if it starts importing app code. Use it
directly with `from tastr.engine import analyze_file`.

See `docs/modules/` for per-module specs, and `docs/hosting-map.md` for how the
hosted setup (Cloudflare, Fly.io, Modal, R2) fits together.
