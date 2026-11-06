"""The Cloudflare Worker's entry (worker/src/entry.py, TAS-84) under CPython, with the Workers runtime stubbed:
Access keys fetched with the Worker's fetch, the Durable Object storage wrapper, the env loading. The Worker
itself runs under `pywrangler dev` (worker/README.md)."""

import asyncio
import errno
import importlib.util
import json
import os
import sys
import time
import types
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from tastr import blobs, db, do_sqlite, http, runner
from tastr.web.hosted import AccessVerifier

ENTRY = Path(__file__).resolve().parent.parent / "worker" / "src" / "entry.py"
TEAM, AUD = "team.cloudflareaccess.com", "aud-tag"


@pytest.fixture
def entry(monkeypatch):
    calls = []

    class Resp:
        def __init__(self, data):
            self.data = data

        async def json(self):
            return self.data

    async def fake_fetch(url):
        calls.append(url)
        return Resp(fake_fetch.jwks)

    workers = types.SimpleNamespace(DurableObject=object, WorkerEntrypoint=object, asgi=None, fetch=fake_fetch)
    js = types.SimpleNamespace(Object=types.SimpleNamespace(keys=lambda o: list(vars(o))))
    ffi = types.SimpleNamespace(create_proxy=lambda f: types.SimpleNamespace(fn=f, destroy=lambda: None))
    monkeypatch.setitem(sys.modules, "workers", workers)
    monkeypatch.setitem(sys.modules, "js", js)
    monkeypatch.setitem(sys.modules, "pyodide", types.ModuleType("pyodide"))
    monkeypatch.setitem(sys.modules, "pyodide.ffi", ffi)
    before = runner.current()
    monkeypatch.setattr(blobs, "keep_copies", True)  # loading the entry sets them; put back after
    monkeypatch.setattr(blobs, "copy_limit", None)
    monkeypatch.setattr(sys, "unraisablehook", sys.unraisablehook)
    spec = importlib.util.spec_from_file_location("tastr_worker_entry", ENTRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.calls, mod.fake_fetch = calls, fake_fetch
    yield mod
    db.use_storage(None)
    runner.use(before)  # loading the entry picks the Worker's runner for the whole process
    http.use(None)  # and its fetch for outgoing requests


def test_access_keys_come_from_the_workers_fetch(entry):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "k1", "alg": "RS256"}
    entry.fake_fetch.jwks = {"keys": [jwk]}
    keys = entry.AccessKeys()
    asyncio.run(keys.refresh(TEAM))
    asyncio.run(keys.refresh(TEAM))  # kept for an hour
    assert entry.calls == [f"https://{TEAM}/cdn-cgi/access/certs"]
    verify = AccessVerifier(TEAM, AUD, keys=keys)
    now = int(time.time())
    claims = {"email": "Friend@Example.com", "aud": [AUD], "iss": f"https://{TEAM}", "iat": now, "exp": now + 600}
    assert verify(jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})) == "friend@example.com"
    with pytest.raises(jwt.PyJWTError):  # a key Access doesn't publish
        verify(jwt.encode(claims, key, algorithm="RS256", headers={"kid": "other"}))
    keys.at -= entry.KEYS_SECONDS + 1
    asyncio.run(keys.refresh(TEAM))
    assert len(entry.calls) == 2  # fetched again once an hour old


def test_storage_wrapper_and_env(entry, tmp_path, monkeypatch):
    fake = do_sqlite.FakeStorage(tmp_path / "lib.db")

    class JsCursor:  # what ctx.storage.sql.exec returns, roughly
        def __init__(self, cur):
            self.columnNames, self._raw = cur.columnNames, list(cur.raw())

        def raw(self):
            return iter(self._raw)

    ctx_storage = types.SimpleNamespace(
        sql=types.SimpleNamespace(exec=lambda sql, *a: JsCursor(fake.exec(sql, *a))),
        transactionSync=lambda proxy: fake.transactionSync(proxy.fn))
    store = entry.Storage(ctx_storage)
    db.use_storage(lambda path: store)
    conn = db.connect(tmp_path / "x.db")
    assert conn.execute("SELECT count(*) AS n FROM tracks").fetchone()["n"] == 0

    def half_done():
        conn.execute("INSERT INTO meta VALUES ('k', 'v')")
        raise RuntimeError("stopped")

    with pytest.raises(RuntimeError):
        db.atomic(conn, half_done)
    assert conn.execute("SELECT count(*) FROM meta WHERE key = 'k'").fetchone()[0] == 0  # rolled back
    monkeypatch.delenv("TASTR_SPIKE_X", raising=False)
    entry.load_env(types.SimpleNamespace(_env=types.SimpleNamespace(TASTR_SPIKE_X="1", LIB=object())))
    assert os.environ.pop("TASTR_SPIKE_X") == "1"  # strings only: LIB is a binding


def test_the_acousticbrainz_database_binding(entry, monkeypatch):
    from tastr.lookup import acousticbrainz

    class Promise:
        def __init__(self, rows):
            self.rows = rows

        def to_py(self):
            return self.rows

    sent = []

    class D1:  # env.ACOUSTICBRAINZ, roughly: prepare(sql).raw() is a promise of rows
        def prepare(self, sql):
            sent.append(sql)
            return types.SimpleNamespace(raw=lambda: Promise([("ab" * 16, 120.0, "A", "minor")]))

    monkeypatch.setattr(sys.modules["pyodide.ffi"], "run_sync", lambda p: p, raising=False)
    try:
        entry.use_bindings(types.SimpleNamespace(_env=types.SimpleNamespace(ACOUSTICBRAINZ=D1())))
        mbid = "abababab-abab-abab-abab-abababababab"
        assert acousticbrainz.read_d1([mbid]) == {mbid: {"bpm": 120.0, "key_key": "A", "key_scale": "minor"}}
        assert sent == [f"SELECT lower(hex(mbid)), bpm, key_key, key_scale FROM recordings WHERE mbid IN (X'{'ab' * 16}')"]
        entry.use_bindings(types.SimpleNamespace(_env=types.SimpleNamespace()))  # not bound: off
        assert acousticbrainz._d1 is None
    finally:
        acousticbrainz.use_d1(None)


def test_the_alarm_is_set_for_the_next_repeating_work(entry):
    assert isinstance(runner.current(), runner.Loop)
    clock = [1000.0]
    entry.RUNNER.clock = lambda: clock[0]
    ran, alarms = [], []

    async def set_alarm(ms):
        alarms.append(ms)

    lib = types.SimpleNamespace(ctx=types.SimpleNamespace(storage=types.SimpleNamespace(setAlarm=set_alarm)))
    lib.arm = arm = types.MethodType(entry.Library.arm, lib)
    stop = entry.RUNNER.every("expiry", 3600, lambda: ran.append("expiry"), delay=60)
    asyncio.run(arm())
    asyncio.run(arm())  # unchanged: not set again
    assert alarms == [1060_000]
    clock[0] = 1061
    asyncio.run(types.MethodType(entry.Library.alarm, lib)())
    assert ran == ["expiry"] and alarms == [1060_000, 4661_000]
    stop()


def test_worker_pins_the_sqljs_the_app_names():
    """worker/build.py copies sql.js from npm; the version must be the one the page asks /sqljs/ for."""
    from tastr.web.routes.search import SQLJS_VERSION

    pkg = json.loads((ENTRY.parent.parent / "package.json").read_text())
    assert pkg["devDependencies"]["sql.js"] == SQLJS_VERSION


def test_static_files_skip_the_worker_and_the_versioned_ones_cache(entry):
    """wrangler.jsonc serves the Worker's static paths straight from Static Assets (a page load asks for ~50
    files, and a cold Python Worker took seconds to start), and build.py's _headers keeps the versioned ones."""
    import re

    sys.path.insert(0, str(ENTRY.parent.parent))
    try:
        import build
    finally:
        sys.path.pop(0)
    conf = (ENTRY.parent.parent / "wrangler.jsonc").read_text()
    first = json.loads(re.search(r'"run_worker_first": (\[[^\]]*\])', conf).group(1))
    assert "/*" in first
    assert sorted(p[1:-1] for p in first if p.startswith("!")) == sorted(entry.ASSET_PATHS)
    rules = build.asset_headers("abc123")
    assert "/static/app-abc123/*\n  Cache-Control: public, max-age=31536000, immutable\n" in rules
    assert "/engine/*\n  Cache-Control: public, max-age=31536000, immutable\n" in rules
    assert "/static/*\n" not in rules  # the favicon and the unversioned files are asked for again


def test_engine_build_id_can_come_from_the_environment(monkeypatch):
    """On the Worker the engine's WebAssembly file is a static asset, not on disk: build.py records its hash."""
    from tastr.engine import rust

    monkeypatch.setenv("TASTR_ENGINE_WASM_ID", "abc123")
    assert rust.wasm_id() == "abc123"


def test_only_the_snapshot_zipfile_noise_is_dropped(entry):
    seen = []
    hook = entry.quiet_unraisable(seen.append)

    def zip_del():
        pass
    zip_del.__qualname__ = "ZipFile.__del__"
    hook(types.SimpleNamespace(exc_value=OSError(errno.EBADF, "Bad file descriptor"), object=zip_del))
    assert seen == []
    other = types.SimpleNamespace(exc_value=OSError(errno.EBADF, "Bad file descriptor"), object=print)
    hook(other)
    hook(oops := types.SimpleNamespace(exc_value=ValueError("x"), object=zip_del))
    assert seen == [other, oops]
