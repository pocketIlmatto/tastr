"""Tastr on a Cloudflare Python Worker (TAS-84, option D of the Fly exit plan; see docs/hosting-map.md).

Two parts:
- `Default`, the Worker: /static/* comes from Workers Static Assets (the files `build.py` copies to public/);
  everything else goes to the one Durable Object.
- `Library`, a SQLite-backed Durable Object: the library database lives in its SQL storage (tastr/do_sqlite.py)
  and it runs the same FastAPI app as Fly does, one request at a time.

Background work runs on the event loop (tastr/runner.py `Loop`): jobs as asyncio tasks, the repeating work
(expiry, the watch) from the Durable Object's alarm, which `arm()` sets after each request and each alarm.

Outgoing requests (Modal over HTTPS, tastr/cloud/remote.py; the audio bucket, tastr/s3.py) go through
tastr/http.py, which here sends them with the Worker's own fetch (`fetch_request`).
"""

import os
import time
from urllib.parse import urlparse

os.environ.setdefault("TASTR_HOME", "/tmp/tastr")  # the in-memory file system: a cache, nothing kept

from workers import DurableObject, WorkerEntrypoint, asgi, fetch  # noqa: E402

from tastr import db, http, runner  # noqa: E402
from tastr.web.hosted import AccessVerifier, Settings  # noqa: E402
from tastr.web.server import create_app  # noqa: E402

DB_PATH = "/tmp/tastr/tastr.db"  # only a name here: the database is the Durable Object's own storage
KEYS_SECONDS = 3600
def fetch_request(method: str, url: str, headers: dict, body) -> http.Response:
    """tastr/http.py's transport: the Worker's fetch, from synchronous code (run_sync waits on it while other
    requests run). The answer's body is read as it arrives."""
    import js
    from pyodide.ffi import run_sync, to_js

    if hasattr(body, "read"):
        body = body.read()  # a file: the Worker's files are in memory anyway
    init = {"method": method, "headers": headers} | ({"body": to_js(body)} if body is not None else {})
    try:
        resp = run_sync(js.fetch(url, to_js(init, dict_converter=js.Object.fromEntries)))
    except Exception as e:  # noqa: BLE001 - a JsException: unreachable, DNS, refused
        raise http.HTTPError(f"couldn't reach {urlparse(url).netloc}: {e}") from e
    names = {}
    resp.headers.forEach(lambda value, name, *_: names.__setitem__(name.lower(), value))
    reader = resp.body.getReader() if resp.body else None

    def chunks():
        while reader is not None:
            part = run_sync(reader.read())
            if part.done:
                return
            yield part.value.to_bytes()

    def close():
        if reader is not None:
            reader.releaseLock()

    return http.Response(resp.status, names, chunks(), close)


http.use(fetch_request)
RUNNER = runner.Loop()
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
        import jwt

        r = await fetch(f"https://{team_domain}/cdn-cgi/access/certs")
        data = await r.json()
        self.by_kid = {k["kid"]: jwt.PyJWK(k).key for k in data["keys"]}
        self.at = time.time()

    def get_signing_key_from_jwt(self, token: str):
        import jwt

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


APP = None
SETTINGS: Settings | None = None
KEYS = AccessKeys()


class Library(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        global APP, SETTINGS
        storage = Storage(ctx.storage)
        db.use_storage(lambda path: storage)
        if APP is None:
            load_env(env)
            SETTINGS = Settings.from_env()
            verifier = AccessVerifier(SETTINGS.access_team_domain, SETTINGS.access_aud, keys=KEYS) \
                if SETTINGS.hosted else None
            APP = create_app(DB_PATH, SETTINGS, verifier=verifier, threads=False)
            APP.state.startup()

    async def fetch(self, request):
        if SETTINGS.hosted:
            try:
                await KEYS.refresh(SETTINGS.access_team_domain)
            except Exception as e:  # noqa: BLE001 - keys unreachable: the known keys (or none: every login fails) stand
                print(f"couldn't fetch the Access signing keys: {type(e).__name__}: {e}")
        response = await asgi.fetch(APP, request, self.env)
        await self.arm()
        return response

    async def alarm(self, *_):
        RUNNER.run_due()
        await self.arm()

    async def arm(self):
        """Set the alarm for when the repeating work is next due (the alarm outlives the object, and wakes it)."""
        due = RUNNER.next_due()
        if due is not None and due != getattr(self, "_armed", None):
            await self.ctx.storage.setAlarm(int(due * 1000))
            self._armed = due


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if urlparse(request.url).path.startswith("/static/"):
            return await self.env.ASSETS.fetch(request)
        return await self.env.LIB.getByName("library").fetch(request)
