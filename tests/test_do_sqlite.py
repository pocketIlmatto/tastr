"""The library database on a Durable Object (tastr/do_sqlite.py, TAS-84), against FakeStorage: Python's sqlite3
held to the Durable Object's rules. The whole suite can run this way too: TASTR_TEST_DO_SQLITE=1 pytest."""

import sqlite3
from contextlib import closing

import pytest

from tastr import db, do_sqlite
from test_foundation import full_card


@pytest.fixture
def on_do(tmp_path):
    stores = {}
    db.use_storage(lambda path: stores.setdefault(str(path), do_sqlite.FakeStorage(path)))
    yield tmp_path / "lib.db"
    db.use_storage(None)
    for st in stores.values():
        st.close()


def test_storage_keeps_the_durable_object_rules(tmp_path):
    st = do_sqlite.FakeStorage(tmp_path / "x.db")
    for refused in ("BEGIN", "SAVEPOINT a", "ATTACH DATABASE 'y.db' AS y", "PRAGMA foreign_keys = OFF"):
        with pytest.raises(sqlite3.OperationalError):
            st.exec(refused)
    cur = st.exec("CREATE TABLE t (a, b); INSERT INTO t VALUES (1, 'x'); SELECT a, b FROM t WHERE a = ?", 1)
    assert cur.columnNames == ["a", "b"] and list(cur.raw()) == [[1, "x"]]
    assert do_sqlite.split_statements("SELECT ';'; SELECT 2;") == ["SELECT ';'", "SELECT 2"]


def test_cards_save_load_and_delete_through_the_adapter(on_do):
    with closing(db.connect(on_do)) as c:
        assert isinstance(c, do_sqlite.Connection)
        tid = db.save_card(c, full_card(), owner="a@b.c")
        assert db.save_card(c, full_card(), owner="a@b.c") == tid  # the same file again replaces it
        assert db.load_card(c, tid).core.bpm == full_card().core.bpm
        row = c.execute("SELECT id, title FROM tracks WHERE id = ?", (tid,)).fetchone()
        assert row["id"] == row[0] == tid and row.keys() == ["id", "title"]
        assert c.execute("UPDATE tracks SET title = 'x' WHERE id = ?", (tid,)).rowcount == 1
        assert db.delete_track(c, tid)
        assert c.execute("SELECT count(*) FROM sections WHERE track_id = ?", (tid,)).fetchone()[0] == 0  # cascade


def test_a_failed_save_leaves_nothing_behind(on_do, monkeypatch):
    with closing(db.connect(on_do)) as c:
        def broken(card, track_id):
            raise RuntimeError("disk full")

        monkeypatch.setattr(db, "_card_json", broken)
        with pytest.raises(RuntimeError):
            db.save_card(c, full_card())
        assert c.execute("SELECT count(*) FROM tracks").fetchone()[0] == 0  # atomic: rolled back


def test_library_snapshot_without_attach(on_do):
    with closing(db.connect(on_do)) as c:
        mine = db.save_card(c, full_card(), owner="me@x.com")
        other = full_card()
        other.source.sha256 = "f" * 64
        db.save_card(c, other, owner="you@x.com")
    data = db.library_snapshot(on_do, "me@x.com")
    snap = sqlite3.connect(":memory:")
    snap.deserialize(data)
    assert [r[0] for r in snap.execute("SELECT id FROM tracks")] == [mine]
    cols = {r[1] for r in snap.execute("PRAGMA table_info(tracks)")}
    assert "owner" not in cols and "local_path" not in cols and "bpm" in cols
    assert snap.execute("SELECT count(*) FROM sections").fetchone()[0] > 0
