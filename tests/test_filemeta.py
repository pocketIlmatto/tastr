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
    from mutagen.id3 import COMM, TALB, TBPM, TCON, TDRC, TIT2, TKEY, TPE1, TPE2, TPUB, TRCK

    w.tags.add(TIT2(encoding=3, text="Night Shift"))
    w.tags.add(TPE1(encoding=3, text="Okra Lane"))
    w.tags.add(TALB(encoding=3, text="Night Shift EP"))
    w.tags.add(TPE2(encoding=3, text="Various"))
    w.tags.add(TDRC(encoding=3, text="2024"))
    w.tags.add(TCON(encoding=3, text="Techno"))
    w.tags.add(TRCK(encoding=3, text="3"))
    w.tags.add(TPUB(encoding=3, text="Okra Records"))
    w.tags.add(TBPM(encoding=3, text="124"))
    w.tags.add(TKEY(encoding=3, text="Am"))
    w.tags.add(COMM(encoding=3, lang="eng", desc="", text="dark and rolling"))
    w.save()
    return dest


def test_reads_tags_and_audio_info(tagged_wav):
    tags, info = read_file_meta(tagged_wav)
    assert tags["title"] == "Night Shift" and tags["artist"] == "Okra Lane" and tags["album"] == "Night Shift EP"
    assert tags["album_artist"] == "Various" and tags["year"] == "2024" and tags["genre"] == "Techno"
    assert (tags["track"], tags["label"], tags["bpm"], tags["key"], tags["comment"]) == ("3", "Okra Records", "124", "Am", "dark and rolling")
    assert info["format"] == "WAV" and info["bit_depth"] == 16 and info["sample_rate"] and info["file_size"] > 0 and info["channels"]


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
    (source,) = resolve_inputs([str(synth_wav[0])])
    source.sha256 = "ab" * 32
    meta = describe(TrackCard(source=source, stages_run={"core": "0.3.0"}), "2026-10-29T01:12:00+00:00")
    assert list(meta) == ["File tags", "Audio", "Source", "Analysis"]
    assert meta["File tags"] == {}
    assert meta["Audio"]["Format"] == "WAV" and meta["Audio"]["Sample rate"].endswith("kHz")
    assert meta["Source"] == {"Kind": "file", "File": "synth_124_Am.wav", "SHA256": "ab" * 32}
    assert meta["Analysis"] == {"Analyzed at": "2026-10-29T01:12:00+00:00", "Stages": "core 0.3.0"}


# --- every common container, tagged through ffmpeg so the files look like ones real taggers write ---

import shutil
import subprocess

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

_FFMPEG_TAGS = {"title": "Night Shift", "artist": "Okra Lane", "album": "Night Shift EP", "album_artist": "Various",
                "date": "2024", "genre": "Techno", "track": "3", "comment": "dark and rolling"}
_CODECS = {"mp3": ["-c:a", "libmp3lame"], "m4a": ["-c:a", "aac"], "flac": ["-c:a", "flac"], "ogg": ["-c:a", "libvorbis"],
           "wav": ["-c:a", "pcm_s16le"]}


def _make(tmp_path, ext, name=None):
    dest = tmp_path / (name or f"raw.{ext}")
    meta = [a for k, v in _FFMPEG_TAGS.items() for a in ("-metadata", f"{k}={v}")]
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=2", *meta, *_CODECS[ext], str(dest)], check=True)
    return dest


@needs_ffmpeg
@pytest.mark.parametrize("ext", ["mp3", "m4a", "flac", "ogg"])
def test_common_tags_read_for_every_format(tmp_path, ext):
    tags, info = read_file_meta(_make(tmp_path, ext))
    assert tags["title"] == "Night Shift" and tags["artist"] == "Okra Lane" and tags["album"] == "Night Shift EP"
    assert tags["album_artist"] == "Various" and tags["year"] == "2024" and tags["genre"] == "Techno"
    assert tags["track"] == "3" and tags["comment"] == "dark and rolling"
    assert info["format"] == ext.upper() and info["sample_rate"] and info["channels"] and info["duration"] > 1


@needs_ffmpeg
def test_m4a_audio_info(tmp_path):
    _, info = read_file_meta(_make(tmp_path, "m4a"))
    assert info["codec"].startswith("AAC") and info["bitrate"] > 0 and info["sample_rate"] == 44100
    assert "bit_depth" not in info  # AAC has no real bit depth


@needs_ffmpeg
def test_m4a_extra_atoms_label_key_bpm(tmp_path):
    from mutagen.mp4 import MP4, MP4FreeForm

    p = _make(tmp_path, "m4a")
    m = MP4(p)
    m["tmpo"] = [124]
    m["----:com.apple.iTunes:LABEL"] = [MP4FreeForm(b"Okra Records")]
    m["----:com.apple.iTunes:initialkey"] = [MP4FreeForm(b"Am")]
    m.save()
    tags, _ = read_file_meta(p)
    assert (tags["bpm"], tags["label"], tags["key"]) == ("124", "Okra Records", "Am")


@needs_ffmpeg
def test_m4a_tags_survive_upload_style_path(tmp_path):
    d = tmp_path / "uploads" / "ab12cd34ef"
    d.mkdir(parents=True)
    (source,) = resolve_inputs([str(_make(d, "m4a", "My Song.m4a"))])
    assert (source.title, source.artist, source.album) == ("Night Shift", "Okra Lane", "Night Shift EP")


@needs_ffmpeg
def test_refresh_file_meta_fixes_old_cards(tmp_path, db_path):
    from tastr.engine.card import TrackCard

    p = _make(tmp_path, "m4a", "My Song.m4a")
    (source,) = resolve_inputs([str(p)])
    source.file_tags, source.title, source.artist, source.album = {}, "My Song", None, None  # what the old reader stored
    conn = db.connect(db_path)
    track_id = db.save_card(conn, TrackCard(source=source))
    assert db.refresh_file_meta(conn) == 1
    card = db.load_card(conn, track_id)
    assert (card.source.title, card.source.artist, card.source.album) == ("Night Shift", "Okra Lane", "Night Shift EP")
    assert card.source.file_tags["genre"] == "Techno"
    assert tuple(conn.execute("SELECT artist, album FROM tracks WHERE id = ?", (track_id,)).fetchone()) == ("Okra Lane", "Night Shift EP")
    assert db.refresh_file_meta(conn) == 0


@needs_ffmpeg
def test_refresh_only_if_stale_runs_once_per_reader_version(tmp_path, db_path):
    from tastr.engine.card import TrackCard

    (source,) = resolve_inputs([str(_make(tmp_path, "m4a"))])
    source.artist = None
    conn = db.connect(db_path)
    track_id = db.save_card(conn, TrackCard(source=source))
    assert db.refresh_file_meta(conn, only_if_stale=True) == 1
    conn.execute("UPDATE tracks SET artist = NULL WHERE id = ?", (track_id,))
    card = db.load_card(conn, track_id)
    card.source.artist = None
    conn.execute("UPDATE cards SET json = ? WHERE track_id = ?", (card.to_json(), track_id))
    assert db.refresh_file_meta(conn, only_if_stale=True) == 0  # reader unchanged: skipped
    assert db.refresh_file_meta(conn) == 1  # `tastr retag` always runs


@needs_ffmpeg
def test_retag_track_refreshes_only_that_track(tmp_path, db_path):
    from tastr.engine.card import TrackCard

    a, b = (_make(tmp_path, "m4a", name) for name in ("A.m4a", "B.m4a"))
    conn = db.connect(db_path)
    ids = []
    for p in (a, b):
        (source,) = resolve_inputs([str(p)])
        source.file_tags, source.artist = {}, None  # what the old reader stored
        ids.append(db.save_card(conn, TrackCard(source=source)))
    assert db.retag_track(conn, ids[0]) == "changed"
    assert db.load_card(conn, ids[0]).source.artist == "Okra Lane"
    assert db.load_card(conn, ids[1]).source.artist is None  # the other track is untouched
    assert db.retag_track(conn, ids[0]) == "unchanged"
    a.unlink()
    assert db.retag_track(conn, ids[0]) == "no_file"
    assert db.retag_track(conn, 999) == "missing"


def test_id3_isrc_and_musicbrainz_recording(tagged_wav):
    from mutagen.id3 import TSRC, UFID
    from mutagen.wave import WAVE

    w = WAVE(tagged_wav)
    w.tags.add(TSRC(encoding=3, text="GBDUW0000059"))
    w.tags.add(UFID(owner="http://musicbrainz.org", data=b"11111111-1111-1111-1111-111111111111"))
    w.save()
    tags, _ = read_file_meta(tagged_wav)
    assert tags["isrc"] == "GBDUW0000059"
    assert tags["musicbrainz_recording"] == "11111111-1111-1111-1111-111111111111"


@needs_ffmpeg
def test_m4a_isrc_and_musicbrainz_recording(tmp_path):
    from mutagen.mp4 import MP4, MP4FreeForm

    p = _make(tmp_path, "m4a")
    m = MP4(p)
    m["----:com.apple.iTunes:ISRC"] = [MP4FreeForm(b"GBDUW0000059")]
    m["----:com.apple.iTunes:MusicBrainz Track Id"] = [MP4FreeForm(b"11111111-1111-1111-1111-111111111111")]
    m.save()
    tags, _ = read_file_meta(p)
    assert (tags["isrc"], tags["musicbrainz_recording"]) == ("GBDUW0000059", "11111111-1111-1111-1111-111111111111")
