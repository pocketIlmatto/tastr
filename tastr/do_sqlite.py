"""The library database on a Cloudflare Durable Object (TAS-84, option D of docs/fly-exit): a `sqlite3.Connection`
look-alike over the Durable Object's SQL storage, so db.py runs unchanged there.

What it bridges (Durable Object SQL vs Python's sqlite3):
- `storage.exec(query, *bindings)` returns a cursor (column names, rows); several statements may run in one exec,
  with the bindings going to the last.
- No BEGIN/COMMIT/SAVEPOINT: each statement commits by itself, and writes with no await between them are committed
  together. `storage.transactionSync(fn)` is the only real transaction, so `with conn:` gives no rollback here and
  code that needs all-or-nothing calls `db.atomic`.
- Foreign keys are always on. ATTACH is not allowed.

`storage` is anything with `exec(sql, *args)` and `transactionSync(fn)`: the Worker's wrapper around ctx.storage, or
`FakeStorage` (Python's sqlite3 held to the same rules) in tests and `TASTR_TEST_DO_SQLITE=1` test runs.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from typing import Any, Iterable

_FK_PRAGMA = re.compile(r"^\s*PRAGMA\s+foreign_keys\s*=", re.I)
_WRITE = ("INSERT", "UPDATE", "DELETE", "REPLACE")


class Row(tuple):
    """sqlite3.Row's interface: by position, by column name, keys()."""

    _names: list[str]

    def __new__(cls, values, names):
        r = super().__new__(cls, values)
        r._names = names
        return r

    def __getitem__(self, k):
        if isinstance(k, str):
            try:
                return tuple.__getitem__(self, self._names.index(k))
            except ValueError:
                raise IndexError(f"No item with that key: {k}") from None
        return tuple.__getitem__(self, k)

    def keys(self):
        return list(self._names)


class Cursor:
    def __init__(self, conn: Connection):
        self.conn = conn
        self.rows: list = []
        self.description = None
        self.lastrowid: int | None = None
        self.rowcount = -1
        self._i = 0

    def execute(self, sql: str, params: Iterable[Any] | dict = ()) -> Cursor:
        self.rows, self._i = [], 0
        if _FK_PRAGMA.match(sql):  # always on in a Durable Object
            return self
        if isinstance(params, dict):  # named parameters, as positional ones
            names = re.findall(r"[:@$](\w+)", sql)
            sql, args = re.sub(r"[:@$]\w+", "?", sql), tuple(params[n] for n in names)
        else:
            args = tuple(params)
        storage = self.conn.storage
        cur = storage.exec(sql, *args)
        names = list(cur.columnNames)
        self.description = tuple((n, None, None, None, None, None, None) for n in names) if names else None
        as_row = self.conn.row_factory is not None
        self.rows = [Row(r, names) if as_row else tuple(r) for r in cur.raw()]
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if head in _WRITE:
            # The cursor's own write count includes index writes, so ask SQLite like sqlite3 does.
            changes, rowid = next(iter(storage.exec("SELECT changes(), last_insert_rowid()").raw()))
            self.rowcount = changes
            if head in ("INSERT", "REPLACE"):
                self.lastrowid = rowid
        else:
            self.rowcount = -1
        return self

    def executemany(self, sql: str, seq) -> Cursor:
        n = 0
        for params in seq:
            self.execute(sql, params)
            n += max(self.rowcount, 0)
        self.rowcount = n
        return self

    def fetchone(self):
        if self._i < len(self.rows):
            self._i += 1
            return self.rows[self._i - 1]
        return None

    def fetchmany(self, size: int = 1):
        out = self.rows[self._i:self._i + size]
        self._i += len(out)
        return out

    def fetchall(self):
        out = self.rows[self._i:]
        self._i = len(self.rows)
        return out

    def __iter__(self):
        while (r := self.fetchone()) is not None:
            yield r

    def close(self) -> None:
        pass


class Connection:
    """What db.py uses of sqlite3.Connection. commit/rollback do nothing (each statement commits by itself) and
    `with conn:` is only a block; `atomic` is the real transaction."""

    row_factory = None

    def __init__(self, storage):
        self.storage = storage

    def cursor(self) -> Cursor:
        return Cursor(self)

    def execute(self, sql, params=()) -> Cursor:
        return Cursor(self).execute(sql, params)

    def executemany(self, sql, seq) -> Cursor:
        return Cursor(self).executemany(sql, seq)

    def executescript(self, script: str) -> Cursor:
        body = "\n".join(line for line in script.splitlines()
                         if not re.match(r"^\s*(BEGIN|COMMIT|PRAGMA\s+foreign_keys)\b", line, re.I))
        self.storage.exec(body)
        return Cursor(self)

    def atomic(self, fn):
        """fn() in a real transaction: all of its writes, or none if it raises."""
        return self.storage.transactionSync(fn)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---- Durable Object SQL storage, as close as Python's sqlite3 gets (tests, TASTR_TEST_DO_SQLITE=1) --------------

_REFUSED = re.compile(r"^\s*(BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE|ATTACH|DETACH)\b", re.I)


class _FakeCursor:
    def __init__(self, cur: sqlite3.Cursor):
        self.columnNames = [d[0] for d in cur.description] if cur.description else []
        self._rows = cur.fetchall() if cur.description else []

    def raw(self):
        return iter([list(r) for r in self._rows])


class FakeStorage:
    """exec() and transactionSync() over a SQLite file, with the Durable Object's rules: no transaction statements,
    no ATTACH, foreign keys always on, each statement committed by itself."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA foreign_keys = ON")
        # A Durable Object runs one thing at a time; the tests' background jobs are threads, so take turns.
        self.lock = threading.RLock()

    def exec(self, sql: str, *bindings):
        with self.lock:
            return self._exec(sql, *bindings)

    def _exec(self, sql: str, *bindings):
        stmts = split_statements(sql)
        for s in stmts:
            if _REFUSED.match(s) or _FK_PRAGMA.match(s):
                raise sqlite3.OperationalError(f"not allowed in a Durable Object: {s.strip()[:40]}")
        if not stmts:
            return _FakeCursor(self.db.execute("SELECT 1 WHERE 0"))
        for s in stmts[:-1]:
            self.db.execute(s)
        return _FakeCursor(self.db.execute(stmts[-1], bindings))

    def transactionSync(self, fn):
        with self.lock:
            return self._transaction(fn)

    def _transaction(self, fn):
        self.db.execute("SAVEPOINT txn")
        try:
            out = fn()
        except BaseException:
            self.db.execute("ROLLBACK TO txn")
            self.db.execute("RELEASE txn")
            raise
        self.db.execute("RELEASE txn")
        return out

    def close(self) -> None:
        self.db.close()


def split_statements(sql: str) -> list[str]:
    """`sql`'s statements, without their semicolons (a semicolon inside a string doesn't split)."""
    out, cur = [], ""
    for part in sql.split(";"):
        cur += part + ";"
        if sqlite3.complete_statement(cur):
            if cur.strip(" \n\t;"):
                out.append(cur.strip().rstrip(";").strip())
            cur = ""
    if cur.strip(" \n\t;"):
        out.append(cur.strip().rstrip(";").strip())
    return out
