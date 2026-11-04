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

The Cloudflare Worker (`worker/check.sh`) builds and runs a short smoke test when anything under `tastr/`,
`worker/` or `pyproject.toml` changes: Node and uv set up, pywrangler bundles the Worker with its Pyodide
packages and starts it locally, about a minute in all.

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

## Slow tests

`.github/workflows/slow-tests.yml` keeps the run time in check without anyone watching. After each successful
`tests` run on a push to `main`, `.github/scripts/slow_tests.py` takes the median of the last 5 full runs
(docs-only runs, under a minute, don't count). When it is over 5 minutes:

1. it opens an issue labeled `slow-tests` with the runs and the newest run's slowest steps, and
2. starts Claude Code in Actions (`anthropics/claude-code-action`), which compares step timings, fixes the cause
   on a `claude/slow-tests-<issue>` branch and opens a pull request that closes the issue. It may not skip or
   weaken tests, or raise the limit or the job timeout. If the slowness was outside the repo and has passed (a
   mirror outage), it comments and closes the issue instead.

While a `slow-tests` issue is open, nothing new starts, so a slow week opens one issue, not one per merge. The
pull request runs the usual `tests` and waits for the owner to merge it.

- **Secret:** `CLAUDE_CODE_OAUTH_TOKEN` (run `claude setup-token` locally; uses the owner's Claude plan) or
  `ANTHROPIC_API_KEY` (billed per use), under Settings > Secrets and variables > Actions. Without either, the
  issue still opens and gets a comment saying Claude didn't start.
- **Try it:** Actions > slow-tests > Run workflow with "force" ticked opens a test issue and starts Claude, which
  should comment that the setup works and close it.
- **Cost:** the check is one short job after each main run. Claude runs only when the tests are slow, for up to
  45 minutes and 60 turns.
- **Change the limit:** `LIMIT_MIN` and `RUNS` in `.github/scripts/slow_tests.py`.

## Worker errors

`.github/workflows/worker-errors.yml` watches the live app without anyone watching. Every 30 minutes
`.github/scripts/worker_errors.py` reads the last hour of the `tastr` Worker's error logs (Workers Observability),
groups the lines by request (a Python traceback arrives as one log line per traceback line) and leaves out the
known harmless ZipFile noise (TAS-98). When 5 or more requests failed, or the Durable Object was reset (out of
memory or CPU time), which fails every request in flight:

1. it opens an issue labeled `worker-errors` with the grouped errors and one example of each, and
2. starts Claude Code in Actions, which traces the cause and, when the fix is clear, opens a pull request on a
   `claude/worker-errors-<issue>` branch that closes the issue. When it needs a setting or a secret, or the cause
   came from outside and has passed, it comments on the issue with what to do instead. It never changes
   settings or secrets, deploys or merges.

While a `worker-errors` issue is open, nothing new starts; each new report is added to it as a comment. The repo
is public, so the report masks emails, upload names and link tokens.

- **Secrets:** `CF_LOGS_TOKEN`, a Cloudflare API token with only Account > Workers Observability > Read (My
  Profile > API Tokens > Create Token > Custom token), and the same Claude secret as slow tests. Without
  `CF_LOGS_TOKEN` the check only says so in the run summary.
  If Cloudflare refuses the token, the check fails (a red run) and shows Cloudflare's reason in its log;
  no issue is opened. Check the token's permission, account and IP filtering, and that it hasn't expired.
- **Try it:** Actions > worker-errors > Run workflow with "force" ticked (and "minutes" to look further back).
- **Cost:** a 15-second job every 30 minutes, free on a public repository. Claude runs only when the check trips,
  for up to 45 minutes and 80 turns.
- **Change the limit:** `FAILED_LIMIT` and `WINDOW_MIN` in `.github/scripts/worker_errors.py`; add known noise to
  `NOISE` there.

## Live health check

`.github/workflows/health.yml` asks the live app's `/api/health` every hour, at 41 past, through Cloudflare Access
with a service token. It tries three times, a minute apart. When all three fail it opens a `site-down` issue
assigned to the owner (or adds to the open one) and the run goes red, so GitHub's failed-run email arrives too.
When the site answers again it comments and closes the issue. No Claude runs: an outage needs a person, and
`worker-errors` already investigates failing requests.

- **Secrets:** `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET`. Make a service token in Cloudflare
  **Zero Trust > Access > Service credentials > Service Tokens** (name it `github-health`, pick a long duration),
  then open the Tastr application in **Access > Applications**, add a policy with action **Service Auth** that
  includes that service token, and save. The token carries no email, so it reaches only `/api/health`; every other
  page still answers 401. Without the secrets, the run only says so.
- **Try it:** Actions > health > Run workflow.
- **Cost:** a few seconds an hour, free on a public repository.

## Claude on issues and PRs

`.github/workflows/claude.yml` runs Claude Code when the owner writes `@claude` in an issue, an issue or PR
comment, a review comment or a review. Comments from anyone else are ignored, so strangers can't start it on
this public repo. Claude works on a branch and opens or updates a pull request; it doesn't merge, deploy or change
settings. Be careful with `@claude` on someone else's pull request: Claude then reads and runs their code.

- **Secret:** the same Claude secret as slow tests.
- **Cost:** up to 45 minutes and 60 turns per mention, on the owner's Claude plan.

## Dependency updates

`.github/dependabot.yml` asks for updates once a month, in one grouped pull request per ecosystem: Python
(`pyproject.toml`), Rust (`engine-rs/`), the Worker's npm packages (`worker/`), the Strudel test packages and the
GitHub Actions. Python requirements that are lower bounds are left alone unless a newer release falls outside
them. `sql.js` is skipped, because `tastr/web/routes/search.py` and the Dockerfile name its version; bump those
together by hand.

When an update fails `tests`, `.github/workflows/dependabot-fix.yml` (which runs from main, after the failed run)
starts Claude Code on the same branch. Claude adapts the code to the new version, runs the checks, pushes one
commit and explains it on the pull request. If the new version looks broken, it comments instead of forcing a fix.
It never skips tests or pins a dependency back. When the tests still fail after Claude's commit, it doesn't try
again; it says so on the pull request.

- **Secret:** the same Claude secret as slow tests. If the run says the secret isn't available, add
  `CLAUDE_CODE_OAUTH_TOKEN` under **Settings > Secrets and variables > Dependabot** as well.
- **Cost:** at most five update PRs a month, each with a normal `tests` run (free on a public repository); Claude
  runs only on a failure, for up to 45 minutes and 60 turns.
- **Skip a package:** add an `ignore` entry under its ecosystem in `.github/dependabot.yml`.

## Benchmark smoke run

`.github/workflows/bench-smoke.yml` runs on the 3rd of each month at 06:17 UTC. It scores main's engine on the
first 100 tracks of GiantSteps Key (exact key) and Beatport tempo (BPM within 4%) on Modal, reading the R2 bucket.
It uses `modal run`, a temporary app from main's code, so it measures main rather than whatever was last deployed,
and the shared `tastr` app stays as it is. `.github/scripts/bench_smoke.py` compares the scores with two things:

- the last good run on the same tracks; it flags a drop of more than 0.05;
- the full scores the app quotes in `tastr/engine/bench_scores.json`; it flags a score more than 0.12 below.

It also flags a dataset whose run failed or lost more than 10% of its tracks.

A flagged run opens a `bench-drop` issue assigned to the owner, or adds to the open one, and goes red. A red run never
becomes "the last good run", so later runs keep comparing with the scores from before the drop. The next good run
closes the issue. No Claude runs: a drop needs a person to decide whether it is a bug or a trade-off.

- **Secrets:** `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`, the deploy workflow's. They are already set. The Modal
  secret `tastr-r2` (docs/cloud.md) must exist in the Modal workspace's main environment.
- **Try it:** Actions > bench-smoke > Run workflow. `limit` changes the tracks per dataset, but only runs with the
  same number are compared with each other. After an intended change that lowers a score, run it with `accept` on
  so its scores become the new baseline, then record a full run (docs/bench.md).
- **Cost:** about $0.15 of Modal compute a month, plus about 20 Actions minutes, which are free on a public
  repository.

## Auto-merge for docs and tests

`.github/workflows/automerge.yml` turns on auto-merge (squash) for pull requests that only touch docs and tests.
GitHub then merges them by itself once the required `test` check passes. A PR qualifies when all of these hold:

- it comes from a branch in this repository;
- its author is the owner or `claude[bot]`;
- every path it adds, changes, deletes or renames (old and new name) is under `docs/` or `tests/`, or is a Markdown
  file. `CLAUDE.md`, `AGENTS.md` and anything under `.github/` never qualify.

A later push to the PR that touches anything else turns auto-merge off again, and that PR waits for a review.
Dependabot PRs change manifests, so they never qualify.

- **Setting:** turn on **Settings > General > Pull Requests > Allow auto-merge**. Without it the run fails with
  "auto-merge is not allowed".
- **Note:** GitHub starts no workflows for a merge made with the workflow's token, so `tests` doesn't rerun on main
  after such a merge. It passed on the PR head, and nothing deploys, since docs and tests never ship.

## Deploy workflow

`.github/workflows/deploy.yml` ships the hosted app (Modal, then the Cloudflare Worker through `deploy-worker.yml`; [deploy.md](deploy.md#updating)). It
doesn't watch pushes itself: it runs on `workflow_run` when the `tests` workflow completes on `main`, and only
deploys when that run passed on a push (not a PR). So a merge that breaks the tests never reaches the app.

- **What counts as a change that ships:** files under `tastr/`, `engine-rs/` or `worker/`, `pyproject.toml`,
  `deploy.yml` and `deploy-worker.yml`, compared with the commit of the last run whose web server step succeeded
  ("Deploy the Worker"). A merge of only docs, tests, Markdown or other workflows is tested and not deployed.
  An allowlist rather than `paths-ignore`, because `workflow_run` has no path filter and the Worker and the Modal
  images are built from exactly these paths.
- **Out-of-order runs:** if `main` moved on by the time the run starts, it skips; the newer commit's run deploys,
  so an older run can't roll the app back.
- **Cost:** a skipped run is one short job (checkout and two API calls, under a minute). A deploy is about 10
  minutes: Modal builds its own images, and the runner builds the engine's WebAssembly, bundles and smoke-tests the
  Worker (`worker/check.sh --serve`), then deploys it.
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
