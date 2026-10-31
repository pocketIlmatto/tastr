# Using Tastr

Install it with the [Quick start](../README.md#quick-start) first.

## Links (optional)

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
tastr set-owner you@example.com  # give tracks with no owner to a hosted-mode user (hosting.md)
tastr pack library.tar --owner you@example.com  # your library, audio and stems in one file for a hosted server (deploy.md)
tastr unpack library.tar        # on the hosted server, into its empty library
tastr retag                      # re-read file tags for analyzed tracks (`tastr serve` does this once after the tag reader changes)
tastr query "SELECT title, bpm, camelot FROM tracks ORDER BY bpm"
tastr stages                     # pipeline stages in run order
tastr serve --host 127.0.0.1 --port 8000   # web UI (defaults shown)
tastr serve --log-level debug    # also print stage tracebacks and request details
tastr bench synth                # score the engine on labeled tracks (bench.md)
```

The database lives at `~/.tastr/tastr.db`. Override it with `--db` or the
`TASTR_DB` environment variable. The web UI reads the same database.

## Stems (optional)

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

## Drum model (optional)

"Detect drums with the model" in a track's ⋯ menu (or the bulk Actions menu)
re-analyzes it in the background with ADTOF's pretrained drum transcription model (Zehren et al.), which also adds a cymbal lane. On real
mixes it finds toms (onset F 0.37 against 0.01 on MDB Drums mixes, 0.88 against
0.00 on unseen StemGMD kits) and scores higher on kick and hats; snare, clap, perc and open hats stay with the built-in detector. It is
slower than the built-in lanes, so everyday analysis doesn't use it; re-analyzing a track that has it keeps it. Details and
scores are in [drums.md](drums.md#pretrained-drum-model-adtof).

```bash
python -m pip install -e '.[drums]'   # PyTorch only; shared with .[stems]
tastr drum-model fetch                 # shows the license, downloads the weights (optional: first use does it too)
tastr drum-model run 1                 # re-analyze track 1 with the model, like the menu item
```

**The weights are CC BY-NC-SA 4.0: non-commercial use only, and share-alike.** They
are not part of Tastr's repository: the first run (or `tastr drum-model fetch`) downloads
them (about 5 MB, from a pinned commit of ADTOF's GitHub repository, checked by SHA-256)
into `~/.tastr/models/`. `TASTR_DRUM_MODEL=adtof` runs the model on every analysis
instead of on request; `TASTR_DRUM_MODEL=off` switches it off.

## Tests

To run the tests:

```bash
python -m pytest -q
```

CI also lints, and a PR fails if any of these complain:

```bash
ruff check .                                         # Python; rules in [tool.ruff] in pyproject.toml
npx eslint@10.12.0 tastr/web/static                  # web UI scripts; rules in eslint.config.mjs
(cd engine-rs && cargo clippy --release --all-targets -- -D warnings)   # Rust engine
```

`ruff check --fix .` fixes the simple ones (unused imports).

`tests/test_strudel_runtime.py` plays the generated Strudel sketches in Strudel itself.
It skips unless Strudel's packages are installed for it (Node 22.15 or newer):
`npm ci --ignore-scripts --prefix tests/strudel-runtime`. CI installs them when the
sketch generator or its tests change.

## Hosting for a few people (optional)

Tastr can also run as a small shared web app. Hosted mode (`TASTR_HOSTED=1`, the
`hosted` extra) puts it behind Cloudflare Access for a few invited people:

- every request needs their login (a one-time code sent to their email);
- each person has their own library and sees only the tracks they uploaded;
- link analysis is off, the SQL box reads only your own library, and uploads
  and stem jobs have daily limits;
- uploaded audio and stems are deleted after a while (30 days by default), but
  the track cards stay;
- admins get an **Admin** tab with usage per person and an activity log.

[docs/hosting.md](hosting.md) lists everything hosted mode changes.
[docs/deploy.md](deploy.md) puts it on Fly.io, with analysis and stems on
Modal and the library backed up to R2, and [docs/hosting-map.md](hosting-map.md)
shows how those pieces fit together. Uploads use the built-in drum detector;
"Detect drums with the model" runs the optional drum model on one track at a time.

## Cloud (optional)

Stem separation and benchmarks can run on [Modal](https://modal.com), with
benchmark datasets in a Cloudflare R2 bucket (`python -m pip install -e '.[cloud]'`).
On your own machine the web app and everyday analysis stay local. Setup and costs
are in [cloud.md](cloud.md). A hosted server ([hosting.md](hosting.md)) uses its own Modal
app for analysis and stems.
