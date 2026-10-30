# Tastr

Drop in audio files, folders or track/playlist links and get a **track card**
for each track: tempo, key, energy, sections, vibe, drums, bass and chords,
plus ideas for rebuilding it in Ableton Live or Strudel. Every card lands in a
SQLite database you can search, chart and query with SQL.

It runs on your own machine. Heavy jobs (stem separation, benchmarks) can
optionally run on a cloud GPU.

<img src="docs/images/library.png" width="720" alt="The Tastr library and a track card with its energy chart and sections">

## What it does

### Track cards

- **Tempo and key.** BPM with how likely it is to be right (calibrated on the benchmarks), a beat and bar grid, and the
  key from a small learned model trained on GiantSteps and Beatport (shown with
  its Camelot code for mixing). Drum & bass is read at its real tempo, not
  half or two thirds of it.
- **Energy and sections.** An energy curve with rough section boundaries
  (intro, build, drop, chorus, outro...). Sections that sound alike share a
  letter, so `Drop B` and `Outro B` are the same part.
- **Vibe tags.** Scored tags such as bass-heavy, dark or percussive, grouped by
  kind (energy, mood, rhythm, sound...) and tinted by how strongly they apply.
- **File tags.** Title, artist and album from MP3, FLAC and m4a tags.
- **Listed elsewhere (optional).** With your own API keys, the BPM and key that
  [GetSongBPM](https://getsongbpm.com) and FreqBlog list for the track, shown next to
  Tastr's figures with a mark when they disagree. Off by default; see
  [docs/lookup.md](docs/lookup.md).

### Drums, bass and chords

<img src="docs/images/drums-bass-chords.png" width="560" alt="Drum grid, bass piano roll and chord cards for one section">

- **Drum lanes** for kick, snare, clap, hat, open hat, tom and perc, as a
  16-step grid per section. Template patterns that fit the tempo are one click
  away.
- **Per-section bass and chords:** the bass note and rhythm for each bar, the
  chord loop with Roman numerals, and what changed from one section to the next.
- **Edit and play it.** Click steps, pick bass notes and chords, and hear it
  in the browser. Export MIDI or copy it as text.
- **Saved edits and versions.** Edits save automatically with the track. Name
  a version to come back to later. Re-analyzing keeps your edits for sections
  that didn't move.

### Try this in Ableton or Strudel

- **Ableton Live:** setup (tempo, scale, Camelot), drum rack programming, bass
  and chord moves, scales to play over each chord, an arrangement map and mix
  notes.
- **[Strudel](https://strudel.cc):** a live-code sketch of the track with each
  section's drums, bass and chords at the right tempo. It can use your edits
  and the original audio cut into bars, either the full mix or separated stems.
  "Open in Strudel" opens it in strudel.cc, ready to play and remix.

<img src="docs/images/strudel.png" width="560" alt="A generated Strudel sketch with the original audio">

### Listen and explore

- **Energy, waveform and spectrogram views** with playback. Click a section to
  play from there, drag the playhead to scrub (you hear it as you drag), and
  scroll to zoom.
- **Stem separation** into vocals, bass, other and drums with Demucs, on your
  machine or on a [Modal](https://modal.com) GPU (see [Stems](#stems-optional)).
- **Library sidebar** with search, sort and filters by vibe, key and BPM.
- **Track and bulk actions:** copy a summary, reload metadata, re-analyze,
  separate stems or delete, for one track or a whole selection.
- **Explore tab** for running SQL against your library in the browser.
- **Keyboard shortcuts** for most things; press `?` for the list.

<img src="docs/images/spectrogram.png" width="560" alt="Spectrogram view with sections and playback">

### Measured, not guessed

`tastr bench` scores the engine against labeled datasets (GiantSteps,
Beatport EDM Key, Harmonix, Beat This!, your own tracks), so every analysis
change is judged by numbers. Experiments that worked and the ones that didn't
are written up in [docs/bench.md](docs/bench.md), [docs/drums.md](docs/drums.md)
and [docs/rhythm.md](docs/rhythm.md). See [Accuracy benchmark](#accuracy-benchmark).

## Quick start

You need Python 3.10 or newer. Check with `python3 --version`. If `python3` is
not found, install Python from [python.org](https://www.python.org/downloads/)
or, on a Mac, run `brew install python`.

From the repo folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'    # use '.[dev,links]' to analyze track/playlist links
tastr serve
```

Then open http://127.0.0.1:8000 in your browser and drop in your audio files.
The web UI is where you analyze tracks and browse their cards.

### Links (optional)

With the `links` extra ([yt-dlp](https://github.com/yt-dlp/yt-dlp)) you can
paste a track or playlist link from YouTube, SoundCloud, Bandcamp, Mixcloud
and other sites that allow downloads. Spotify and Apple Music links don't
work, because those services don't allow downloading audio.

## Command line

The `tastr` command does the same work from a terminal. Activate the virtualenv
from the Quick start first (`source .venv/bin/activate`).

```bash
tastr analyze ~/Music/some-track.wav ~/Music/folder/
tastr list
tastr show 1                     # full card as JSON
tastr stems 1                    # split track 1 into vocals, bass, other and drums (needs the stems extra, below)
tastr lookup                     # catalog BPM/key for every track (opt-in, docs/lookup.md)
tastr retag                      # re-read file tags for analyzed tracks (`tastr serve` does this once after the tag reader changes)
tastr query "SELECT title, bpm, camelot FROM tracks ORDER BY bpm"
tastr stages                     # pipeline stages in run order
tastr serve --host 127.0.0.1 --port 8000   # web UI (defaults shown)
tastr serve --log-level debug    # also print stage tracebacks and request details
tastr bench synth                # score the engine on labeled tracks (docs/bench.md)
```

The database lives at `~/.tastr/tastr.db`. Override it with `--db` or the
`TASTR_DB` environment variable. The web UI reads the same database.

### Stems (optional)

"Separate stems" in a track's ⋯ menu (or `tastr stems ID`) splits one track into
vocals, bass, other and drums with [Demucs](https://github.com/facebookresearch/demucs)
(`htdemucs`, MIT). The Strudel sketch can then play the stems you pick instead of the
full mix. Demucs needs PyTorch, about 2 GB, so it is an optional install:

```bash
python -m pip install -e '.[stems]'
```

The model weights (about 80 MB) download the first time you separate a track; Demucs 4.1
gets them from the Hugging Face Hub and keeps them in `~/.cache/huggingface`. Later runs
load them from there without going online, so the Hub's "unauthenticated requests ...
set a HF_TOKEN" warning only shows on that first download. A free read token in `HF_TOKEN`
(set in your shell, never in the repo) silences it there too, but isn't needed. It
takes under a minute for a 2-minute track on a recent Mac CPU, and about half that on
its GPU, which is used automatically (as is a CUDA GPU); set `TASTR_STEMS_DEVICE`
to `cpu`, `mps` or `cuda` to choose. Stems are FLAC files in `~/.tastr/stems/` and are
deleted with the track.

To run the tests:

```bash
python -m pytest -q
```

## Accuracy benchmark

`tastr bench` runs the engine over tracks with known labels and scores BPM,
key, beats, downbeats and sections, so analysis changes are judged by numbers.
Details, metric definitions and dataset setup are in [docs/bench.md](docs/bench.md).

Supported datasets:

| Dataset | Labels |
|---|---|
| `giantsteps-key` | global key (audio from the Zenodo GiantSteps+ mirror, 599 of 604 tracks) |
| `giantsteps-tempo` | global BPM (the mirror covers only 43 of 664 tracks) |
| `beatport-key` | global key (Beatport EDM Key, Zenodo 1101082; 1,275 tracks with a plain major/minor label) |
| `beatport-tempo` | global BPM (Beatport's listed BPM; 774 of the Beatport EDM Key previews) |
| `harmonix` | BPM, beats, downbeats, sections |
| `manifest` | your own tracks, from a CSV or JSON file |
| `synth` | generated tracks; a smoke test that needs no data |

Stage the data in a folder outside the repo and never commit audio; docs/bench.md
shows the layout. Example commands:

```bash
tastr bench synth
tastr bench giantsteps-key ~/data/giantsteps-key --limit 50               # quick check
tastr bench giantsteps-key ~/data/giantsteps-key --jobs 2 --out key.json  # full run, about 46 min
tastr bench giantsteps-key ~/data/giantsteps-key --jobs 2 --baseline key.json
tastr bench manifest ~/music/labels.csv
```

`--limit N` takes the first N tracks, `--jobs` analyzes tracks in parallel (set
`OMP_NUM_THREADS=1` with it), `--out` writes the JSON report, and `--baseline`
compares against an earlier report.

Current key baseline (`--stages core`, learned key model): GiantSteps 599 tracks, MIREX weighted
score 0.676, exact 59.9%; Beatport EDM Key 1,275 tracks, 0.586, exact 49.1%. Both are in-sample (the model was
trained on them); held-out halves score 0.682 and 0.606 (Beatport confidence-2 labels), against 0.650 and 0.562
for the previous Krumhansl-Kessler method. See docs/bench.md "Key detection notes". Tempo results, including the Beat This! spectrogram benches (`tastr spec-bench`), are under "Tempo notes" there.

### Cloud (optional)

Stem separation and benchmarks can run on [Modal](https://modal.com), with
benchmark datasets in a Cloudflare R2 bucket (`python -m pip install -e '.[cloud]'`).
The web app and everyday analysis stay local. Setup and costs are in
[docs/cloud.md](docs/cloud.md).

### README screenshots

The images in `docs/images/` are generated, so they follow the UI. Add the `readme-shots`
label to a PR that changes how the UI looks; after it merges, a workflow retakes them and
opens a PR with the new images. To retake them yourself, run `python scripts/readme_shots.py`
(it needs node and the playwright npm package; see the script).

## Layout

```
tastr/engine/               the analysis engine; imports nothing else from tastr
  card.py                   TrackCard dataclasses: the contract between modules
  pipeline.py               stage registry, dependency ordering, run_pipeline()
  audio.py                  decoding files to mono audio (librosa, ffmpeg fallback)
  signals.py                shared intermediates (HPSS, onset envelope, chroma, RMS)
  music.py                  key estimation, scales, chords, Camelot
  key_model.py              learned key model constants (generated by bench/train_key.py)
  stages/                   core, sections, tags, suggestions, views (one module each)
  bench/                    accuracy benchmark: dataset loaders, metrics, runner
  bench/key_features.py     cache per-track key features; bench/train_key.py fits the key model
  bench/synth.py            synthetic tracks with known BPM/key/sections (tests/synth.py re-exports it)
tastr/ingest.py             files/folders/links -> Sources, URL resolver registry
tastr/resolvers/            link/playlist resolvers
tastr/lookup/               opt-in catalog lookups (BPM/key by artist + title), one module per provider
tastr/db.py                 SQLite schema and save/load/query
tastr/service.py            inputs -> engine -> DB, used by the CLI and web app
tastr/web/                  FastAPI app and UI
```

The engine is kept self-contained so it can be split into its own library
later; `tests/test_engine.py` fails if it starts importing app code. Use it
directly with `from tastr.engine import analyze_file`.

Read `docs/ARCHITECTURE.md` for the data flow, card fields, feature names and
DB tables, and `docs/modules/` for per-module specs.

## Licenses

Tastr doesn't have a license file yet. Third-party pieces it uses or links to:

- **Strudel** is AGPL-3.0. Tastr doesn't bundle it: "Open in Strudel" opens
  the generated sketch on strudel.cc.
- **Demucs** (stems) is MIT. It is an optional install, and its model weights
  download on first use.
- **yt-dlp** (links) is under the Unlicense, also optional.
- **Catalog lookups** (optional, your own API keys): [GetSongBPM](https://getsongbpm.com)
  requires a link back wherever its data is shown, which the card does. FreqBlog's API
  allows storing results only on paid plans, so Tastr keeps them only with
  `FREQBLOG_PLAN=paid`. See [docs/lookup.md](docs/lookup.md).
- **Benchmark datasets** each have their own license, listed in
  [docs/datasets.md](docs/datasets.md). Some don't allow redistribution, so
  audio and labels are never committed to this repo.
