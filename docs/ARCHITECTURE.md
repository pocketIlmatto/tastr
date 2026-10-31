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
| `danceability` | 0..1 | core | 0.75 percussiveness + 0.25 tempo fit (bell around 135 BPM), fitted in docs/vibes.md |
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

Where the time goes (2026-10-31, a 3-minute synthetic track, 4 CPUs): the whole
analysis took 14.5 s, 10.8 s of it HPSS, and 7.9 s of that was librosa's two 2-D
median filters, which take scipy's slow general path. `signals.hpss` now filters
row by row on scipy's fast 1-D path, with the same output sample for sample
(checked against `librosa.effects.hpss`, and `tests/test_engine.py` keeps it so):
7.1 s in all, HPSS 3.4 s. Sharing the repeated STFTs would save only about 0.3 s more.

## Browser signals

`TASTR_BROWSER_SIGNALS=1` (off by default) moves HPSS, half of the server's analysis time, into the visitor's
browser. The page decodes the upload with Web Audio (an `OfflineAudioContext` at 22,050 Hz, channels averaged
like `librosa.to_mono`), runs `hpssHarmonic` from `tastr/web/static/signals.js` in a worker, and uploads a
signals file next to the audio. The format is in `tastr/engine/browser_signals.py`: the decoded audio and
HPSS's harmonic part, as int16 with a scale each.

The engine then analyzes that audio instead of decoding the file. It takes the harmonic part as sent and the
percussive part as audio minus harmonic. HPSS's soft masks add up to one and its STFT round trip rebuilds
the input, so that difference is HPSS's percussive output to about 1e-7. Signals that don't fit are set aside
and the file is decoded as usual: a bad file, the wrong rate, or a duration more than 1 s (or 1%) off the
file's. `extras["browser_signals"]` on the card says which happened. The page sends nothing for tracks over
20 minutes, tracks too short for the 31-frame median, or files the browser can't decode. Re-analysis and
links always run fully on the server.

What was measured (2026-10-31), with `tests/test_browser_signals.py` keeping the first two:

- `hpssHarmonic` matches `signals.hpss` to about 1e-7, about 3 s for a 3-minute track in Node on one core.
- In Chromium, an MP3 and a WAV analyzed from the browser's signals got the same BPM, key, downbeats (within
  1 ms) and sections as the server decoding the file itself.
- On four synthetic tracks, the only differences from the server-only card were drum velocities, a few
  vibe scores in the third decimal and, on one track, one ghost note in a drum lane. All of them come from the
  int16 rounding: with float32 audio the cards matched except for one velocity. Float32 would double the
  upload, to 8 bytes a sample (about 32 MB for 3 minutes), so the format uses int16. Synthetic tracks have
  digital silence, where that rounding shows most.

## Storage (`tastr/db.py`)

SQLite, one file (default `~/.tastr/tastr.db`, override with `TASTR_DB` or
`--db`). Tables:

- `tracks`: one row per track, headline numbers as columns (bpm, key, camelot, energy...). Deduped on `sha256` within one owner's library; `owner` is the uploader's Access email on a hosted server (docs/hosting.md) and NULL on your own machine.
- `sections`, `tags`, `suggestions`: one row per item, keyed by `track_id`.
- `features`: long format `(track_id, name, value)`, so new features need no migration.
- `series`: long format `(track_id, name, idx, t, value)` for curves.
- `cards`: the full card JSON, the source of truth for re-rendering.
- `lookups`: `(track_id, provider, status, bpm, key_name, fetched_at, result_json)`, what online catalogs list for the track (opt-in, `tastr/lookup/`, [lookup.md](lookup.md)). Kept across re-analysis.
- `track_labels`: `(track_id, bpm, key_name, boundaries, note, updated_at)`, your own labels for a track (`tastr/labels.py`). Never touched by analysis; exported as a bench manifest.

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
