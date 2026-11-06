"""Tastr on a Cloudflare Python Worker (TAS-84; see docs/hosting-map.md).

Two parts:
- `Default`, the Worker: /static/*, /sqljs/* (sql.js) and /engine/* (the engine's WebAssembly build) come from
  Workers Static Assets (the files `build.py` copies to public/) without reaching it (wrangler.jsonc
  `run_worker_first`), and it hands out /pub/preview.png and /pub/favicon.svg from there too; everything else goes
  to the one Durable Object.
- `Library`, a SQLite-backed Durable Object: the library database lives in its SQL storage (tastr/do_sqlite.py)
  and it runs the FastAPI app (tastr/web/server.py), one request at a time.

Background work runs on the event loop (tastr/runner.py `Loop`): jobs as asyncio tasks, the repeating work
(expiry, the watch) from the Durable Object's alarm, which `arm()` sets after each request and each alarm.

The AcousticBrainz lookup reads its index from the D1 database bound as ACOUSTICBRAINZ (`d1_query`).

Outgoing requests (Modal over HTTPS, tastr/cloud/remote.py; the audio bucket, tastr/s3.py) go through
tastr/http.py, which here sends them with the Worker's own fetch (`fetch_request`).
"""

import errno
import os
import sys
import time
from urllib.parse import urlparse


def quiet_unraisable(hook=sys.unraisablehook):
    """sys.unraisablehook without one known harmless line: a ZipFile from the runtime's start-up snapshot whose
    file is gone when it is garbage collected (EBADF in ZipFile.__del__). It went to the Worker's logs as an
    error, hundreds a day, and buried the real ones (T31). Since Python 3.14 (the Workers runtime's Python) a
    failing __del__ arrives with no `object`, only the message "Exception ignored while calling deallocator
    <function ZipFile.__del__ at 0x...>", so both are checked."""

    def unraisable(u):
        if isinstance(u.exc_value, OSError) and u.exc_value.errno == errno.EBADF and (
                getattr(u.object, "__qualname__", "") == "ZipFile.__del__"
                or "<function ZipFile.__del__ at " in (getattr(u, "err_msg", None) or "")):
            return
        hook(u)

    return unraisable


# First thing, before the big imports below: the snapshot's stale ZipFile can be collected at any point after start.
sys.unraisablehook = quiet_unraisable()

os.environ.setdefault("TASTR_HOME", "/tmp/tastr")  # the in-memory file system: a cache, nothing kept
try:
    from tastr_build import ENGINE_WASM_ID  # build.py: the hash of the engine build in public/engine/
except ImportError:
    ENGINE_WASM_ID = None
if ENGINE_WASM_ID:
    os.environ.setdefault("TASTR_ENGINE_WASM_ID", ENGINE_WASM_ID)

from workers import DurableObject, WorkerEntrypoint, asgi, fetch  # noqa: E402

from tastr import blobs, db, http, runner  # noqa: E402
from tastr.lookup import acousticbrainz  # noqa: E402
from tastr.web.hosted import AccessVerifier, Settings  # noqa: E402
from tastr.web.server import create_app  # noqa: E402

# Cloudflare runs this module's top level at deploy time and snapshots the memory, so a new isolate starts with
# everything imported here already loaded. These are otherwise imported on first use, which on the Worker means on
# the first request of every new isolate (docs/hosting-map.md, Cold starts): PyJWT with cryptography for the
# Access login (the biggest), the review list, ML tags and Starlette's sync-endpoint runner.
import anyio._backends._asyncio  # noqa: E402, F401
import jwt  # noqa: E402
import jwt.algorithms  # noqa: E402, F401

from tastr import labels  # noqa: E402, F401
from tastr.engine import ml_tags  # noqa: E402, F401

DB_PATH = "/tmp/tastr/tastr.db"  # only a name here: the database is the Durable Object's own storage
KEYS_SECONDS = 3600
def fetch_request(method: str, url: str, headers: dict, body, timeout: float = http.TIMEOUT) -> http.Response:
    """tastr/http.py's transport: the Worker's fetch, from synchronous code (run_sync waits on it while other
    requests run). The answer's body is read as it arrives. A `timeout` shorter than the default (a catalog lookup)
    cuts the request off after that long; the default leaves long Modal runs and downloads alone."""
    import js
    from pyodide.ffi import run_sync, to_js

    if hasattr(body, "read"):
        body = body.read()  # a file: the Worker's files are in memory anyway
    init = {"method": method, "headers": headers} | ({"body": to_js(body)} if body is not None else {})
    if timeout < http.TIMEOUT:
        init["signal"] = js.AbortSignal.timeout(int(timeout * 1000))
    try:
        resp = run_sync(js.fetch(url, to_js(init, dict_converter=js.Object.fromEntries)))
    except Exception as e:  # noqa: BLE001 - a JsException: unreachable, DNS, refused
        raise http.HTTPError(f"couldn't reach {urlparse(url).netloc}: {e}") from e
    names = {}
    resp.headers.forEach(lambda value, name, *_: names.__setitem__(name.lower(), value))
    reader = resp.body.getReader() if resp.body else None

    def chunks():
        while reader is not None:
            try:
                part = run_sync(reader.read())
            except Exception as e:  # noqa: BLE001 - the connection dropped, or the timeout's signal fired
                raise http.HTTPError(f"{urlparse(url).netloc} stopped answering: {e}") from e
            if part.done:
                return
            yield part.value.to_bytes()

    def close():
        if reader is not None:
            reader.releaseLock()

    return http.Response(resp.status, names, chunks(), close)


def sleep(seconds: float) -> None:
    """runner.Loop's sleep: pauses the calling work while other requests and jobs run."""
    import asyncio

    from pyodide.ffi import run_sync

    run_sync(asyncio.sleep(seconds))


http.use(fetch_request)
blobs.keep_copies = False  # the file system is memory: drop local copies of uploads once read
blobs.copy_limit = 1  # and read back one at a time
RUNNER = runner.Loop(sleep=sleep)
runner.use(RUNNER)  # before the app is built: its job queues take their lanes from it


class Storage:
    """ctx.storage as tastr/do_sqlite.py expects it: exec() with plain Python rows, transactionSync(fn)."""

    def __init__(self, storage):
        self.storage = storage

    def exec(self, sql, *args):
        return _Cursor(self.storage.sql.exec(sql, *args))

    def transactionSync(self, fn):  # noqa: N802 - the Durable Object API's name
        from pyodide.ffi import create_proxy

        proxy = create_proxy(fn)
        try:
            return self.storage.transactionSync(proxy)
        finally:
            proxy.destroy()


class _Cursor:
    def __init__(self, cur):
        self.columnNames = list(cur.columnNames)  # noqa: N815
        self._rows = [list(r) for r in cur.raw()]

    def raw(self):
        return iter(self._rows)


class AccessKeys:
    """Cloudflare Access's signing keys, fetched with the Worker's fetch (PyJWT's own client uses urllib, which
    can't open sockets here) before each request when older than an hour, so the app's verifier stays synchronous."""

    def __init__(self):
        self.by_kid: dict = {}
        self.at = 0.0

    async def refresh(self, team_domain: str) -> None:
        if self.by_kid and time.time() - self.at < KEYS_SECONDS:
            return
        r = await fetch(f"https://{team_domain}/cdn-cgi/access/certs")
        data = await r.json()
        self.by_kid = {k["kid"]: jwt.PyJWK(k).key for k in data["keys"]}
        self.at = time.time()

    def get_signing_key_from_jwt(self, token: str):
        kid = jwt.get_unverified_header(token).get("kid")
        if kid not in self.by_kid:
            raise jwt.PyJWTError("unknown signing key")
        return _Key(self.by_kid[kid])


class _Key:
    def __init__(self, key):
        self.key = key


def load_env(env) -> None:
    """The Worker's variables and secrets (wrangler.jsonc `vars`, `wrangler secret put`) into os.environ, where
    Settings.from_env reads them."""
    from js import Object

    raw = getattr(env, "_env", env)  # the SDK wraps the JS env object
    for name in Object.keys(raw):
        value = getattr(raw, name)
        if isinstance(value, str):
            os.environ[name] = value


def d1_query(binding):
    """tastr/lookup/acousticbrainz.py's D1 reader: SQL text in, rows (lists) out, from synchronous code."""
    from pyodide.ffi import run_sync

    def query(sql: str) -> list[list]:
        return [list(r) for r in run_sync(binding.prepare(sql).raw()).to_py()]

    return query


def use_bindings(env) -> None:
    """Hand the app the bindings it reads directly (only the AcousticBrainz database so far)."""
    raw = getattr(env, "_env", env)
    db_binding = getattr(raw, "ACOUSTICBRAINZ", None)
    acousticbrainz.use_d1(d1_query(db_binding) if db_binding is not None else None)


APP = None
SETTINGS: Settings | None = None
KEYS = AccessKeys()


async def refresh_keys() -> None:
    """Cloudflare Access's signing keys, when hosted and over an hour old (AccessKeys)."""
    if SETTINGS is None or not SETTINGS.hosted:
        return
    try:
        await KEYS.refresh(SETTINGS.access_team_domain)
    except Exception as e:  # noqa: BLE001 - keys unreachable: the known keys (or none: every login fails) stand
        print(f"couldn't fetch the Access signing keys: {type(e).__name__}: {e}")


class Library(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        global APP, SETTINGS
        storage = Storage(ctx.storage)
        db.use_storage(lambda path: storage)
        if APP is None:
            # A cold start: a new isolate, so the request or alarm that woke it waits for this. The Worker's clock
            # stands still while code runs (it moves only on I/O), so this can't be timed from here; the line marks
            # the invocation whose CPU time to read in the logs (docs/hosting-map.md, Cold starts).
            print("cold start: building the app")
            load_env(env)
            use_bindings(env)
            SETTINGS = Settings.from_env()
            verifier = AccessVerifier(SETTINGS.access_team_domain, SETTINGS.access_aud, keys=KEYS) \
                if SETTINGS.hosted else None
            APP = create_app(DB_PATH, SETTINGS, verifier=verifier, threads=False)
            APP.state.startup()

    async def fetch(self, request):
        await refresh_keys()
        response = await asgi.fetch(APP, request, self.env)
        await self.arm()
        return response

    async def alarm(self, *_):
        RUNNER.run_due()
        # An alarm often starts the isolate that a visitor then finds warm: fetching the keys here spares that visit.
        await refresh_keys()
        await self.arm()

    async def arm(self):
        """Set the alarm for when the repeating work is next due (the alarm outlives the object, and wakes it)."""
        due = RUNNER.next_due()
        if due is not None and due != getattr(self, "_armed", None):
            await self.ctx.storage.setAlarm(int(due * 1000))
            self._armed = due


ASSET_PATHS = ("/static/", "/sqljs/", "/engine/")  # the files build.py puts in public/
# The share pages' preview image and icon (routes/strudel.py): public, so served from public/static/ by their
# /pub/ names, which Cloudflare Access lets through, where /static/ needs the login.
PUBLIC_ASSETS = {"/pub/preview.png": "/static/preview.png", "/pub/favicon.svg": "/static/favicon.svg"}


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = urlparse(request.url)
        if url.path.startswith(ASSET_PATHS):
            return await self.env.ASSETS.fetch(request)
        if url.path in PUBLIC_ASSETS:
            return await self.env.ASSETS.fetch(url._replace(path=PUBLIC_ASSETS[url.path], query="").geturl())
        return await self.env.LIB.getByName("library").fetch(request)
