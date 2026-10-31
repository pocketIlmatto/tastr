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
| `apt-get install ffmpeg libsndfile1` | ~27 s |
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
- **Cache the apt packages** (ffmpeg) to save ~20 s per run. Because billing rounds up per
  job, this only saves a minute when a run drops below a boundary (e.g. 3.1 → 2.8).
- **New workflows** (for example the Modal/R2 benchmark): never run heavy benchmarks on
  GitHub runners. Trigger them with `workflow_dispatch` or a schedule, add `timeout-minutes`,
  a `concurrency` group, and keep the job a thin client that hands work to Modal.

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
