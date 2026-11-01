# Tastr

Drop in audio files, folders or track/playlist links and get a **track card**
for each track: tempo, key, energy, sections, vibe, drums, bass and chords,
plus ideas for rebuilding it in Ableton Live or Strudel. Every card lands in a
SQLite database you can search, chart and query with SQL.

Run it on your own machine, or host it for a few invited people behind a login.
Heavy jobs (stem separation, benchmarks) can optionally run on a cloud GPU.

<img src="docs/images/library.png" width="720" alt="The Tastr library and a track card with its energy chart and sections">

## What it does

- **Track cards:** BPM and key with how likely each is right, time signature (4/4 or 3/4),
  an energy curve with sections, vibe tags, file tags, and optionally the BPM and key other
  catalogs list.
- **Drums, bass and chords** per section: a drum grid, the bass line and the chord loop.
  Edit them, play them in the browser and export MIDI.
- **Try this in Ableton or Strudel:** setup and programming notes for Ableton Live, and a
  live-code [Strudel](https://strudel.cc) sketch of the track.
- **Listen and explore:** waveform, spectrogram and a live 3D view with playback, stem
  separation, a filterable library, and **Find tracks** to search it without code or in SQL.
- **Glossary:** what every label and number on a card means and how Tastr works it out.

[docs/features.md](docs/features.md) describes every feature in detail.

<img src="docs/images/drums-bass-chords.png" width="560" alt="Drum grid, bass piano roll and chord cards for one section">

<img src="docs/images/strudel.png" width="560" alt="A generated Strudel sketch with the original audio">

<img src="docs/images/spectrogram.png" width="560" alt="Spectrogram view with sections and playback">

<img src="docs/images/live-3d.png" width="560" alt="The Live tab's 3D view: a landscape of frequency ridges rolling back from the playing moment">

<img src="docs/images/find-tracks.png" width="560" alt="Find tracks: a key that mixes with 8A and a BPM range, and the matching tracks">

<img src="docs/images/glossary.png" width="560" alt="The Glossary's Camelot wheel, showing which keys mix with the open track's key">

## Measured, not guessed

`tastr bench` scores the engine against labeled datasets (GiantSteps, Beatport EDM Key,
Harmonix, Beat This!, your own tracks), so every analysis change is judged by numbers.
Highlights so far:

| Area | Before | Now | Measured on |
|---|---|---|---|
| Exact key, held out | 47.6% | 60.4% | GiantSteps Key, 599 EDM previews |
| BPM within 4% | 0.49 | 0.74 | GiantSteps Tempo, 43 tracks |
| Bar 1 in the right place | 0.43 | 0.64 | GTZAN, 933 songs |
| Toms found (onset F, optional model) | 0.01 | 0.37 | MDB Drums, real mixes |
| Section boundaries within 3 s (F) | 0.48 | 0.58 | Harmonix Set, 912 songs |
| Danceability vs listeners (AUC) | 0.70 | 0.82 | MTG-Jamendo, 510 tracks |

The [analysis engine report](docs/engine-report.md) has every change, the datasets that
found and checked it, and what's still open. [docs/bench.md](docs/bench.md) covers running
the benchmarks.

## Quick start

You need Python 3.10 or newer (`python3 --version`; on a Mac, `brew install python`).
From the repo folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'    # use '.[dev,links]' to analyze track/playlist links
(cd engine-rs && cargo build --release --lib)   # the engine that analyzes tracks; needs Rust (rustup.rs)
tastr serve
```

Then open http://127.0.0.1:8000 and drop in your audio files. Run the tests with
`python -m pytest -q`.

## Docs

| Doc | What's in it |
|---|---|
| [features.md](docs/features.md) | everything a track card shows and what you can do with it |
| [usage.md](docs/usage.md) | the `tastr` command, links, stems, the drum model, tests, cloud jobs |
| [hosting.md](docs/hosting.md), [deploy.md](docs/deploy.md), [hosting-map.md](docs/hosting-map.md) | hosting for a few invited people (Cloudflare Access, Fly.io, Modal, R2) |
| [bench.md](docs/bench.md), [engine-report.md](docs/engine-report.md) | accuracy benchmarks and what they changed |
| [audio-terms.md](docs/audio-terms.md) | audio analysis terms (STFT, chroma, HPSS, onsets...) explained, with diagrams |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md), [modules/](docs/modules/) | data flow, card fields, DB tables, code layout |
| [ci.md](docs/ci.md) | CI, Actions minutes and how the README screenshots are made |

## Licenses

Tastr's code is under the MIT License ([LICENSE](LICENSE)). Security reports: see
[SECURITY.md](SECURITY.md).

Two generated files hold numbers fitted on public datasets, so they also carry the
datasets' terms:

- `tastr/engine/key_model.py` (the learned key model) was fitted on GiantSteps Key and
  the Beatport EDM Key dataset (Zenodo 1101082), both CC BY-SA 4.0. Treat these weights
  as derived from that data: credit the datasets and share changes to the weights alike.
- `tastr/engine/calibration_table.py` (the "N% likely right" tables) was fitted on
  GiantSteps and Beatport tempo/key results and HJDB from the Beat This! release
  (Zenodo 13922116, CC BY 4.0).

Third-party pieces it uses or links to:

- **mutagen** (file tags, a required dependency) is GPL-2.0-or-later. Tastr doesn't
  bundle it; pip installs it alongside.
- The Rust engine (`engine-rs/`) builds in **symphonia** (MPL-2.0, unmodified) for decoding,
  **rustfft** and **libm** (MIT/Apache-2.0), and **miniz_oxide** (MIT/Zlib/Apache-2.0) for the
  spectrogram PNG.

- **Strudel** is AGPL-3.0. Tastr doesn't bundle it: "Open in Strudel" opens
  the generated sketch on strudel.cc. The optional sketch tests install Strudel's
  packages under `tests/strudel-runtime/` to play the sketches; they are never
  shipped with Tastr (not in the package or the Docker image).
- Ideas in the Strudel sketch (short mini-notation, sliders, the punchcard, named
  lines, sidechain and wobble, testing against Strudel itself) come from
  [Toaster](https://github.com/StopBanning/toaster) (MIT). No code was copied.
- **Demucs** (stems) is MIT. It is an optional install, and its model weights
  download on first use.
- **yt-dlp** (links) is under the Unlicense, also optional.
- **Catalog lookups** (optional, your own API keys): [GetSongBPM](https://getsongbpm.com)
  requires a link back wherever its data is shown, which the card does. FreqBlog's API
  allows storing results only on paid plans, so Tastr keeps them only with
  `FREQBLOG_PLAN=paid`. [AcousticBrainz](https://acousticbrainz.org) dumps and
  [MusicBrainz](https://musicbrainz.org) core data are public domain (CC0); Tastr reads a
  local index of the 2022 AcousticBrainz dump. See [docs/lookup.md](docs/lookup.md).
- **ADTOF** (optional drum model): its code and model weights are **CC BY-NC-SA
  4.0, non-commercial and share-alike**. Tastr doesn't include either. The weights
  download on first use from the ADTOF repository; the network and preprocessing
  that run them are Tastr's own code, written from the paper's architecture.
  [ADTOF](https://github.com/MZehren/ADTOF): M. Zehren, M. Alunno, P. Bientinesi,
  "High-Quality and Reproducible Automatic Drum Transcription from Crowdsourced
  Data", Signals 4(4), 2023.
- **Benchmark datasets** each have their own license, listed in
  [docs/datasets.md](docs/datasets.md). Some don't allow redistribution, so
  audio and labels are never committed to this repo.
