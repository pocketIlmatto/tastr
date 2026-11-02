"""Moving the hosted library from Fly to the Cloudflare Worker (tastr/library_move.py, TAS-51): the database goes
through the audio bucket and is imported once into the Durable Object's database."""

from contextlib import closing

import pytest

from tastr import blobs, db, do_sqlite, library_move
from test_foundation import full_card

pytest.importorskip("moto.server")
from test_blobs import s3_server, use_bucket  # noqa: E402, F401 - s3_server is a fixture


def fly_library(path):
    """A Fly database: two tracks, an admin log line and a job a restart cut off."""
    db.use_storage(None)
    with closing(db.connect(path)) as conn:
        ids = []
        for name in ("a", "b"):
            card = full_card()
            card.source.uri = card.source.local_path = f"/x/{name}.wav"
            card.source.sha256 = name * 64
            ids.append(db.save_card(conn, card, owner="a@b.c"))
        db.add_event(conn, user="a@b.c", kind="upload", detail="Uploaded song.wav")
        with conn:
            conn.execute("INSERT INTO job_markers (kind, key, data, updated_at) VALUES ('stems', 'x', '{}', 0)")
            conn.execute("UPDATE meta SET value = '0' WHERE key = 'db_schema_version'")  # never copied over
    return ids


@pytest.fixture
def worker_db(tmp_path):
    stores = {}
    db.use_storage(lambda path: stores.setdefault(str(path), do_sqlite.FakeStorage(path)))
    yield tmp_path / "worker.db"
    db.use_storage(None)
    for st in stores.values():
        st.close()


def test_rows_copy_into_an_empty_library(tmp_path, worker_db):
    ids = fly_library(tmp_path / "fly.db")
    data = (tmp_path / "fly.db").read_bytes()
    db.use_storage(lambda path, st=do_sqlite.FakeStorage(worker_db): st)
    with closing(db.connect(worker_db)) as conn:
        version = db.get_meta(conn, "db_schema_version")
        db.add_event(conn, user=None, kind="restart", detail="written at startup, before the import")
        counts = library_move.import_rows(conn, data)
        assert counts["tracks"] == 2 and counts["events"] == 1 and "job_markers" not in counts
        assert len(db.list_tracks(conn)) == 2
        assert db.load_card(conn, ids[0]).core.bpm == full_card().core.bpm
        assert [r[0] for r in conn.execute("SELECT detail FROM events")] == ["Uploaded song.wav"]
        assert conn.execute("SELECT COUNT(*) FROM job_markers").fetchone()[0] == 0
        assert db.get_meta(conn, "db_schema_version") == version


def test_import_from_the_bucket_runs_once(tmp_path, monkeypatch, s3_server, worker_db):  # noqa: F811
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    use_bucket(monkeypatch, s3_server)
    fly_library(tmp_path / "fly.db")
    assert library_move.to_bucket(tmp_path / "fly.db") > 0
    assert blobs.client().head(library_move.KEY)
    db.use_storage(lambda path, st=do_sqlite.FakeStorage(worker_db): st)
    with closing(db.connect(worker_db)) as conn:
        assert library_move.import_from_bucket(conn)["tracks"] == 2
        assert db.get_meta(conn, library_move.DONE)
        assert library_move.import_from_bucket(conn) is None  # once only
    blobs.reset()


def test_a_library_in_use_is_never_overwritten(tmp_path, monkeypatch, s3_server):  # noqa: F811
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    use_bucket(monkeypatch, s3_server)
    fly_library(tmp_path / "fly.db")
    library_move.to_bucket(tmp_path / "fly.db")
    with closing(db.connect(tmp_path / "in-use.db")) as conn:
        db.save_card(conn, full_card(), owner="x@y.z")
        assert library_move.import_from_bucket(conn) is None
        assert len(db.list_tracks(conn)) == 1
    blobs.reset()


def test_library_to_bucket_needs_the_bucket(tmp_path, monkeypatch, capsys):
    from tastr import cli

    monkeypatch.delenv("TASTR_AUDIO_BUCKET", raising=False)
    assert cli.main(["--db", str(tmp_path / "x.db"), "library-to-bucket"]) == 1
    assert "bucket is off" in capsys.readouterr().err
