# Tastr on Cloudflare Workers (in progress, TAS-84)

The hosted server as a Cloudflare Python Worker: the FastAPI app runs inside one SQLite-backed Durable Object
that also holds the library database. Fly still serves the live site; see the "Coming next" section of
[docs/hosting-map.md](../docs/hosting-map.md) for what works so far.

| File | What it is |
| --- | --- |
| `src/entry.py` | The Worker (`Default`: static files or the Durable Object) and the Durable Object (`Library`) |
| `build.py` | Copies the `tastr` package into `src/tastr/` and the browser files into `public/static/` |
| `wrangler.jsonc` | Bindings (`LIB`, `ASSETS`), the Durable Object migration, plain settings |
| `pyproject.toml` | The Python packages, as Pyodide builds |

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

Tests of the entry module run with the app's tests (`tests/test_worker_entry.py`, the Workers runtime stubbed),
and `TASTR_TEST_DO_SQLITE=1 pytest` runs the whole suite through the Durable Object database adapter.

Background work on the Worker has no threads: `tastr/runner.py` runs jobs as asyncio tasks and the repeating
work from the Durable Object's alarm. Synchronous code that has to wait on the network does it with
`pyodide.ffi.run_sync`, and other requests run while it waits, so no lock may be held across such a wait.
