# Tastr on Cloudflare Workers (TAS-84)

The hosted server as a Cloudflare Python Worker: the FastAPI app runs inside one SQLite-backed Durable Object
that also holds the library database. It serves the live site since 2026-11-02 (TAS-51); see
[docs/hosting-map.md](../docs/hosting-map.md) for how it fits with Cloudflare Access, R2 and Modal, and
[docs/deploy.md](../docs/deploy.md) for setting one up.

| File | What it is |
| --- | --- |
| `src/entry.py` | The Worker (`Default`: static files or the Durable Object) and the Durable Object (`Library`) |
| `build.py` | Copies the `tastr` package into `src/tastr/`, the browser files into `public/static/`, sql.js into `public/sqljs/` and, when built, the engine's WebAssembly build into `public/engine/` |
| `wrangler.jsonc` | Bindings (`LIB`, `ASSETS`, the AcousticBrainz D1 database `ACOUSTICBRAINZ`), the Durable Object migration, plain settings |
| `pyproject.toml` | The Python packages, as Pyodide builds, and the pinned pywrangler |
| `package.json` | The pinned wrangler that pywrangler runs |
| `check.sh` | Build, bundle, size check and a local smoke test, as CI runs them |

## Run it on your machine

Needs Node (for wrangler) and [uv](https://docs.astral.sh/uv/) 0.13.4 or newer.

```sh
python worker/build.py          # with the app's own environment (it imports tastr.web.server)
cd worker
echo TASTR_HOSTED=0 > .dev.vars # local mode: no Access login
uv run pywrangler dev           # http://localhost:8787
```

`pywrangler` downloads the Pyodide packages the first time. The library starts empty; it lives in
`.wrangler/state` until you delete that folder.

`worker/check.sh` does what CI does: builds, bundles with `pywrangler deploy --dry-run`, checks the size limit
and, with `--serve`, starts the Worker on port 8799 in local mode and asks it a few pages. It installs the
pinned wrangler from `package.json` into `worker/node_modules` the first time.

To try analysis, stems and the audio bucket locally, point the Worker at stand-ins in `.dev.vars`:
`TASTR_ANALYSIS_BACKEND=modal`, `TASTR_STEMS_BACKEND=modal` and `TASTR_MODAL_URL` at a server running
`tastr/cloud/modal_web.py`'s `api()` app, and the four `TASTR_AUDIO_*` settings at any S3 server (moto's
`ThreadedMotoServer` works, as in `tests/test_blobs.py`).

Tests of the entry module run with the app's tests (`tests/test_worker_entry.py`, the Workers runtime stubbed),
and `TASTR_TEST_DO_SQLITE=1 pytest` runs the whole suite through the Durable Object database adapter.

Background work on the Worker has no threads: `tastr/runner.py` runs jobs as asyncio tasks and the repeating
work from the Durable Object's alarm. Synchronous code that has to wait on the network does it with
`pyodide.ffi.run_sync`, and other requests run while it waits, so no lock may be held across such a wait.
