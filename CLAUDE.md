# Notes for Claude

Rules for anyone changing this repository, Claude in GitHub Actions included. The repository is public.

## Pull requests

- Open every pull request against `main`. Pull requests that target another branch get stranded.
- Run the fast tests before pushing (`python -m pytest -q -n auto`, plus `cargo test --release --lib` in `engine-rs/`
  when the engine changed).
  Never skip, weaken or delete a test to get a green run.
- Never commit secrets, keys, tokens or real people's emails. [docs/settings.md](docs/settings.md) says where each
  secret lives; when you add, remove or rename one, update that page in the same change.
- Keep licences in mind: the code is MIT, Strudel is AGPL (Tastr doesn't bundle it, only opens strudel.cc), the drum model's weights
  are CC BY-NC-SA, and each benchmark dataset has its own licence ([README.md](README.md#licenses)). Don't copy
  code or data in under a licence that doesn't fit, and update the README's licence list when you add a dependency
  or dataset with terms of its own.

## Hosting

- When a change touches hosting (`worker/`, `tastr/cloud/modal_web.py`,
  `.github/workflows/deploy.yml`, `.github/workflows/deploy-worker.yml`, the settings in `tastr/web/hosted.py`), update `docs/hosting-map.md` in the
  same change, diagrams included. `tests/test_hosting_map.py` catches missing names, not a stale diagram.
- Worker secrets must be type Secret; a plain Text variable is wiped by the next deploy.
- Don't change live settings or secrets, deploy by hand, or merge. Say in the pull request what the owner needs to
  set.

## Engine and benchmarks

- Calibration lives in two files that must agree: `tastr/engine/calibration_table.py` and
  `engine-rs/src/consts.rs`. Change both.
- Every benchmark must be able to run on Modal (`--remote`, [docs/cloud.md](docs/cloud.md)). Iterate on small
  subsets (`--limit N`); run the full set only before keeping a change. Write what an experiment taught you into
  the docs ([docs/bench.md](docs/bench.md) or [docs/engine-report.md](docs/engine-report.md)), not just the
  pull request.
- New datasets go in the R2 bucket `tastr-data`, never in the repository.
- Ask the owner before anything that downloads a lot (more than a few GB) or uses a lot of compute (a full
  benchmark, a GPU run, a large Modal job). Explain the size and cost and wait for a yes.

## App and UI

- A new folder under `tastr/web/static/` needs its own glob in `pyproject.toml`'s package-data, or the live site
  404s on it (`tests/test_package_data.py` checks).
- App JavaScript is ES modules under `tastr/web/static/app/`.
- UI changes need a design note: describe what changes and why in the pull request, with before and after
  screenshots at desktop and phone width, and wait for the owner's review. Don't redesign beyond what was asked.
- Keep README.md short; details go in [docs/features.md](docs/features.md) and [docs/usage.md](docs/usage.md).
