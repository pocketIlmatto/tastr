# CI and GitHub Actions minutes

The repo is private, so every Actions minute counts against the owner's monthly quota
(2,000 minutes on GitHub Free, 3,000 on Pro). Linux runners bill at 1x, and each job is
rounded **up** to the next whole minute.

## The workflow

One workflow, `.github/workflows/test.yml`, one job on `ubuntu-latest`. It runs on every
`pull_request` event and every push to `main`.

A typical audio run (about 2.5 minutes, billed 3):

| Step | Time |
|---|---|
| checkout + setup-python (pip cache) | ~5 s |
| `apt-get install ffmpeg libsndfile1` (cached .deb files) | ~20 s |
| `pip install -e '.[dev]'` | ~20 s |
| pytest, `-n auto` | 80–100 s |
| perf budget (main only) | ~28 s |

The Rust engine (`engine-rs/`) builds natively and for WebAssembly when it, its tests
(`tests/test_rust_engine.py`, `tests/test_rust_switch.py`) or its Python and page side (`tastr/engine/rust.py`,
`tastr/engine/browser_card.py`, `tastr/web/static/engine.js`) change, and also whenever the audio tests run,
because the test suite analyzes on the Rust engine like the app does: about 50 s from clean, less with the cargo
cache.

Lint runs in the same job, before the tests: `ruff check` (a second or two) whenever code changes,
eslint through `npx` (a few seconds) only when a web UI script or `eslint.config.mjs` changes, and
`cargo clippy -D warnings` inside the Rust step, so only when the Rust engine is built.

Already in place: pip cache, docs-only PRs skip everything after the scope step, UI-only PRs
skip the audio tests, perf budget only on main. Node and the Strudel test packages
(`npm ci` in `tests/strudel-runtime`, npm cache, a few seconds) install only when
`tastr/web/static/strudel.js` or its tests change.

## Audit, 2026-10-29

All 97 runs since the repo started (2026-10-28 to 2026-10-29) used about 274 billed minutes:
40 on 2026-10-28 and 234 on 2026-10-29 (78 runs during a day of 14 merged PRs).

- 57 PR runs (~125 min) and 40 pushes to `main` (~120 min, about 44%).
- No runs were retried and only one failed. One PR run took 10 minutes instead of ~2.
- Several PRs had back-to-back pushes whose runs overlapped, both billed in full.

At that peak pace (~230 min/day) the Free quota lasts about 9 days, so a sustained burst
would exhaust it. At a normal pace (a few PRs a day) the project uses roughly 30–50 min/day,
well inside the quota.

## Changes made (PR after the audit)

- `concurrency` with `cancel-in-progress` for PRs: a newer push cancels the superseded run.
  Runs on `main` are never cancelled.
- `timeout-minutes: 15`: the default is 360, so a hung test could bill 6 hours.
- Pushes to `main` use the same docs-only / UI-only scoping as PRs (diffing against the
  previous `main` commit), so merging a docs PR no longer runs the whole suite. The perf
  budget runs only when audio code changed.

## Proposals not yet made

- **Skip the full suite on `main` after a squash merge.** The PR already tested the same
  tree, unless `main` moved since. Saves ~40% but loses the post-merge safety net, and the
  perf budget only runs there. Possible middle ground: on `main`, run only the perf budget.
- **Skip draft PRs** (`if: !github.event.pull_request.draft` plus the `ready_for_review`
  event). Saves runs on work in progress, but drafts get no CI feedback.
- **New workflows** (for example the Modal/R2 benchmark): never run heavy benchmarks on
  GitHub runners. Trigger them with `workflow_dispatch` or a schedule, add `timeout-minutes`,
  a `concurrency` group, and keep the job a thin client that hands work to Modal.

## Speed pass, 2026-11-01 (TAS-56)

Full runs had been 2–4 minutes, then on 2026-10-31 rose to 5–12. Step timings from the runs API:

- **ffmpeg install:** the ~70 MB download from the Azure Ubuntu mirror took 17 s to 503 s (141 kB/s on the
  worst run). The `.deb` files are now cached in `~/apt-archives` (key: runner image version, falling back to the
  previous image's cache), so apt only downloads packages that changed. `apt-get update` still runs (~3 s).
- **Rust build:** only the engine's own crate recompiles each run (the cargo cache covers the dependencies), but
  with `lto = true` and one codegen unit it took ~42 s native plus ~19 s WebAssembly. CI now sets
  `CARGO_PROFILE_RELEASE_LTO=false` and 16 codegen units: about 11 s plus 10 s locally, with the native and
  WebAssembly cards still bit-identical. The Docker build is unchanged and keeps LTO. The perf budget times the
  Python engine, so it isn't affected.
- **numba JIT:** every pytest worker compiled librosa's kernels into a fresh `/tmp/numba-gw*` cache, about
  10–15 s per worker at start-up. Those folders are now cached between runs (key: `pyproject.toml`). A reinstall
  of the same librosa keeps file times, so the cached kernels stay valid.

First warm run on PR #73: 2.9 min in all, with apt 20 s (nothing downloaded), Rust 33 s and pytest 73 s, against
up to 503 s, 78 s and 90 s on the slow runs.

A daily scheduled check (03:17 UTC) reads the last 5 successful full runs on `main` and starts a Claude thread
to investigate when their median goes over 5 minutes. Docs-only runs (under a minute) don't count.

## Deploy workflow

`.github/workflows/deploy.yml` ships the hosted app (Modal, then Fly; [deploy.md](deploy.md#updating)). It
doesn't watch pushes itself: it runs on `workflow_run` when the `tests` workflow completes on `main`, and only
deploys when that run passed on a push (not a PR). So a merge that breaks the tests never reaches the app.

- **What counts as a change that ships:** files under `tastr/`, `engine-rs/` or `deploy/`, `Dockerfile`,
  `.dockerignore`, `fly.toml`, `pyproject.toml` and `deploy.yml` itself, compared with the commit of the last run
  whose Fly step succeeded. A merge of only docs, tests, Markdown or other workflows is tested and not deployed.
  An allowlist rather than `paths-ignore`, because `workflow_run` has no path filter and the Docker and Modal
  images are built from exactly these paths.
- **Out-of-order runs:** if `main` moved on by the time the run starts, it skips; the newer commit's run deploys,
  so an older run can't roll the app back.
- **Cost:** a skipped run is one short job (checkout and two API calls, under a minute). A deploy is about 3
  minutes; the image builds on Fly's remote builder and Modal builds its own images, not the runner.
- **Concurrency:** group `deploy`, never cancelled mid-deploy; a run waits for the one in progress.
- **By hand:** **Actions > deploy > Run workflow** always deploys the chosen branch, with no scope check.
- **Known gap:** the scope covers every commit since the last deploy, but the tests only ran on the newest. If an
  earlier merge failed its tests and a later docs-only merge passes, the later run deploys the earlier code.

## README screenshots workflow

The README screenshots are generated, not taken by hand, so they follow the UI.

- **How they're made:** `scripts/readme_shots.py` builds a throwaway library of three
  synthetic tracks (nothing touches your own library), starts `tastr serve` on it, and
  `scripts/readme-shots.js` takes each shot with Playwright in the default 808 look: the
  library with an open card, the spectrogram, the Live 3D view, drums, bass and chords, the Strudel sketch
  and the Glossary's Camelot wheel. They land in `docs/images/`.
- **When they update:** add the `readme-shots` label to a PR that changes how the UI looks.
  After it merges, the `readme-shots` GitHub workflow retakes them on `main` and opens a PR
  with the new images if any changed. It never runs on ordinary pushes, so it only spends
  Actions minutes when the UI changed.
- **By hand:** run `python scripts/readme_shots.py` (it needs ffmpeg, node and the
  playwright npm package; see the script). To add a shot, add a step to
  `scripts/readme-shots.js` and an `<img>` to the README.

### The workflow's details

`.github/workflows/readme-shots.yml` retakes the README screenshots with
`scripts/readme_shots.py`. It runs only after a PR labeled `readme-shots` merges, or by hand
(`workflow_dispatch`), takes about 3 minutes, and opens a PR with the new images when they
changed. Two things to know:

- GitHub has to allow it to open PRs: Settings → Actions → General → Workflow permissions →
  "Allow GitHub Actions to create and approve pull requests".
- PRs opened with the workflow's token don't start other workflows, so `tests` won't run on
  them. They only change `docs/images/`, which `tests` skips anyway.
