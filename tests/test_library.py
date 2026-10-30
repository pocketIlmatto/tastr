"""Moving a library to a hosted server: `tastr pack` and `tastr unpack` (tastr/library.py)."""

import io
import tarfile

import pytest

from tastr import db, library, stems
from tastr.cli import main
from test_foundation import full_card


def make_library(tmp_path, monkeypatch):
    home = tmp_path / "mac"
    home.mkdir()
    monkeypatch.setenv("TASTR_HOME", str(home))
    audio = tmp_path / "Music" / "song.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")
    conn = db.connect(home / "tastr.db")
    card = full_card()
    card.source.uri = card.source.local_path = str(audio)
    with_audio = db.save_card(conn, card)
    card = full_card()
    card.source.uri, card.source.local_path, card.source.sha256 = "/gone.wav", "/gone.wav", "def"
    without = db.save_card(conn, card)
    conn.close()
    folder = stems.stems_dir("abc", with_audio)
    folder.mkdir(parents=True)
    for n in stems.STEM_NAMES:
        stems.stem_path(folder, n).write_bytes(n.encode())
    return home / "tastr.db", with_audio, without


def test_pack_then_unpack_moves_tracks_audio_and_stems(tmp_path, monkeypatch, capsys):
    src_db, with_audio, without = make_library(tmp_path, monkeypatch)
    out = tmp_path / "library.tar"
    assert main(["--db", str(src_db), "pack", str(out), "--owner", "Pam@Example.com"]) == 0
    assert "packed 2 track(s) and 5 file(s)" in capsys.readouterr().out
    conn = db.connect(src_db)
    assert db.track_owner(conn, with_audio) == (True, None)  # your own library is left as it was
    conn.close()

    server = tmp_path / "server"
    monkeypatch.setenv("TASTR_HOME", str(server))
    server_db = server / "tastr.db"
    assert main(["--db", str(server_db), "unpack", str(out)]) == 0
    conn = db.connect(server_db)
    assert db.track_owner(conn, with_audio) == (True, "pam@example.com")
    card = db.load_card(conn, with_audio)
    path = conn.execute("SELECT local_path FROM tracks WHERE id = ?", (with_audio,)).fetchone()[0]
    assert card.source.local_path == path and path.startswith(str(server / "uploads"))
    assert open(path, "rb").read() == b"audio"
    assert db.load_card(conn, without).source.local_path is None  # its file wasn't on disk
    assert stems.is_ready(stems.stems_dir("abc", with_audio))
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
    # Only into an empty library: never over someone's tracks.
    assert main(["--db", str(server_db), "unpack", str(out)]) == 1
    assert "already has tracks" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["../evil", "/etc/evil", "uploads/../../evil", "other/x"])
def test_unpack_refuses_files_outside_its_folders(tmp_path, name):
    out = tmp_path / "bad.tar"
    with tarfile.open(out, "w") as tar:
        info = tarfile.TarInfo(name)
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="unexpected entry"):
        library.unpack(out, tmp_path / "home", tmp_path / "home.db")
    assert not (tmp_path / "evil").exists()
