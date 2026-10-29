"""Embedded tag reading and the metadata groups the API exposes."""

import pytest

from tastr import db
from tastr.engine.card import TrackCard
from tastr.filemeta import describe, read_file_meta
from tastr.ingest import resolve_inputs

mutagen = pytest.importorskip("mutagen")


@pytest.fixture
def tagged_wav(synth_wav, tmp_path):
    from mutagen.wave import WAVE

    dest = tmp_path / "raw_name.wav"
    dest.write_bytes(synth_wav[0].read_bytes())
    w = WAVE(dest)
    w.add_tags()
    w.save()
    from mutagen.id3 import TALB, TCON, TDRC, TIT2, TPE1, TPE2

    w.tags.add(TIT2(encoding=3, text="Night Shift"))
    w.tags.add(TPE1(encoding=3, text="Okra Lane"))
    w.tags.add(TALB(encoding=3, text="Night Shift EP"))
    w.tags.add(TPE2(encoding=3, text="Various"))
    w.tags.add(TDRC(encoding=3, text="2024"))
    w.tags.add(TCON(encoding=3, text="Techno"))
    w.save()
    return dest


def test_reads_tags_and_audio_info(tagged_wav):
    tags, info = read_file_meta(tagged_wav)
    assert tags["title"] == "Night Shift" and tags["artist"] == "Okra Lane" and tags["album"] == "Night Shift EP"
    assert tags["album_artist"] == "Various" and tags["year"] == "2024" and tags["genre"] == "Techno"
    assert info["format"] == "WAV" and info["sample_rate"] and info["file_size"] > 0 and info["channels"]


def test_untagged_file_falls_back_to_file_name(synth_wav):
    (source,) = resolve_inputs([str(synth_wav[0])])
    assert source.title == "synth_124_Am" and source.artist is None and source.album is None
    assert source.file_tags == {} and source.audio_info["format"] == "WAV"


def test_tags_fill_source(tagged_wav):
    (source,) = resolve_inputs([str(tagged_wav)])
    assert (source.title, source.artist, source.album) == ("Night Shift", "Okra Lane", "Night Shift EP")


def test_unreadable_file_yields_empty(tmp_path):
    p = tmp_path / "junk.mp3"
    p.write_bytes(b"not audio")
    tags, info = read_file_meta(p)
    assert tags == {} and info.get("format") != "MP3"


def test_album_stored_and_old_db_migrated(tagged_wav, db_path):
    import sqlite3

    old = sqlite3.connect(db_path)
    old.executescript(db.SCHEMA.replace("    album           TEXT,\n", ""))
    old.commit()
    old.close()
    conn = db.connect(db_path)
    assert "album" in {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}
    conn.close()


def test_describe_groups_empty_when_no_tags(synth_wav):
    from tastr.engine.card import Source

    (source,) = resolve_inputs([str(synth_wav[0])])
    source.sha256 = "ab" * 32
    meta = describe(TrackCard(source=source, stages_run={"core": "0.3.0"}), "2026-10-29T01:12:00+00:00")
    assert list(meta) == ["File tags", "Audio", "Source", "Analysis"]
    assert meta["File tags"] == {}
    assert meta["Audio"]["Format"] == "WAV" and meta["Audio"]["Sample rate"].endswith("kHz")
    assert meta["Source"] == {"Kind": "file", "File": "synth_124_Am.wav", "SHA256": "ab" * 32}
    assert meta["Analysis"] == {"Analyzed at": "2026-10-29T01:12:00+00:00", "Stages": "core 0.3.0"}
