"""Tastr on a Cloudflare Python Worker (TAS-84, option D of the Fly exit plan; see docs/hosting-map.md).

Two parts:
- `Default`, the Worker: /static/* comes from Workers Static Assets (the files `build.py` copies to public/);
  everything else goes to the one Durable Object.
- `Library`, a SQLite-backed Durable Object: the library database lives in its SQL storage (tastr/do_sqlite.py)
  and it runs the same FastAPI app as Fly does, one request at a time.

Not here yet (TAS-84 parts 3 to 5): background work (analysis jobs, expiry, the watch loop) needs threads, the
Modal client and boto3 don't run on Pyodide. Until then this serves the library, the cards and the admin pages.
"""

import os
import time
from urllib.parse import urlparse

os.environ.setdefault("TASTR_HOME", "/tmp/tastr")  # the in-memory file system: a cache, nothing kept

from workers import DurableObject, WorkerEntrypoint, asgi, fetch  # noqa: E402

from tastr import db  # noqa: E402
from tastr.web.hosted import AccessVerifier, Settings  # noqa: E402
from tastr.web.server import create_app  # noqa: E402

DB_PATH = "/tmp/tastr/tastr.db"  # only a name here: the database is the Durable Object's own storage
KEYS_SECONDS = 3600


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
        return await asgi.fetch(APP, request, self.env)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if urlparse(request.url).path.startswith("/static/"):
            return await self.env.ASSETS.fetch(request)
        return await self.env.LIB.getByName("library").fetch(request)
