"""The Cloudflare Worker's entry (worker/src/entry.py, TAS-84) under CPython, with the Workers runtime stubbed:
Access keys fetched with the Worker's fetch, the Durable Object storage wrapper, the env loading. The Worker
itself runs under `pywrangler dev` (worker/README.md)."""

import asyncio
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

from tastr import db, do_sqlite, runner
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
    spec = importlib.util.spec_from_file_location("tastr_worker_entry", ENTRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.calls, mod.fake_fetch = calls, fake_fetch
    yield mod
    db.use_storage(None)
    runner.use(before)  # loading the entry picks the Worker's runner for the whole process


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
