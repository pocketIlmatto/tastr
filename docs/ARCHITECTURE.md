# Tastr architecture

```
inputs (files, folders, links)
   │  tastr/ingest.py          resolve_inputs() -> [Source]   (links: tastr/resolvers/*)
   ▼
decoded mono audio @ 22050 Hz   engine.audio.load_audio()
   │  tastr/engine/pipeline.py  run_pipeline(): stages in dependency order
   ▼
core ──► sections ──► tags ──► suggestions        tastr/engine/stages/*.py
views (independent: waveform + spectrogram data for the UI)
   │
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
| `danceability` | 0..1 | core | pulse clarity + tempo fit + percussiveness |
| `onset_rate` | per second | core | detected onsets per second |
| `dynamic_range_db` | dB | core | p95 - p10 of frame RMS in dB |
| `swing_percent` | 50..75 | core | where the off-beat 16th lands; 50 straight, ~66 triplet |
| `swing_confidence` | 0..1 | core | |
| `section_contrast` | 0..1 | sections | std of section energies |
| `section_count` | count | sections | |

Series: `energy` (per bar, 0..1, written by core) is the one the UI draws.

## Pipeline (`tastr/engine/pipeline.py`)

A stage is any object with `name`, `version`, `requires` (tuple of stage
names) and `run(ctx)`. `register_stage(obj)` at import time of a module in
`tastr/engine/stages/` makes it part of the default pipeline. `ctx` gives the audio
(`ctx.y`, `ctx.sr`, `ctx.hop`), the card (`ctx.card`) and a per-track cache
(`ctx.get(name, fn)`). Shared intermediates (HPSS, onset envelope, chroma,
RMS) live in `tastr/engine/signals.py`; use them so they are computed once.

## Storage (`tastr/db.py`)

SQLite, one file (default `~/.tastr/tastr.db`, override with `TASTR_DB` or
`--db`). Tables:

- `tracks`: one row per track, headline numbers as columns (bpm, key, camelot, energy...). Deduped on `sha256` within one owner's library; `owner` is the uploader's Access email on a hosted server (docs/hosting.md) and NULL on your own machine.
- `sections`, `tags`, `suggestions`: one row per item, keyed by `track_id`.
- `features`: long format `(track_id, name, value)`, so new features need no migration.
- `series`: long format `(track_id, name, idx, t, value)` for curves.
- `cards`: the full card JSON, the source of truth for re-rendering.
- `lookups`: `(track_id, provider, status, bpm, key_name, fetched_at, result_json)`, what online catalogs list for the track (opt-in, `tastr/lookup/`, [lookup.md](lookup.md)). Kept across re-analysis.

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
