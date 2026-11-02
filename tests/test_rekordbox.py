"""Reading Rekordbox's XML export and scoring it next to Tastr (tastr/engine/bench/rekordbox.py)."""

from __future__ import annotations

import pytest

from tastr.engine.bench import datasets, rekordbox
from tastr.engine.bench.datasets import Item, Truth

XML = """<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0">
  <PRODUCT Name="rekordbox" Version="7.0.0" Company="AlphaTheta"/>
  <COLLECTION Entries="4">
    <TRACK TrackID="1" Name="Night Shift" Artist="Someone" TotalTime="10" AverageBpm="120.00" Tonality="Am"
           Location="file://localhost/Users/pam/Music/Night%20Shift.mp3">
      <TEMPO Inizio="0.500" Bpm="120.00" Metro="4/4" Battito="3"/>
      <POSITION_MARK Name="" Type="0" Start="0.500" Num="-1"/>
      <POSITION_MARK Name="" Type="0" Start="2.500" Num="0"/>
      <POSITION_MARK Name="" Type="4" Start="4.0" End="6.0" Num="1"/>
    </TRACK>
    <TRACK TrackID="2" Name="Drift" Artist="" TotalTime="8" AverageBpm="100.00" Tonality="8B"
           Location="file://localhost/Users/pam/Music/10001.LOFI.mp3">
      <TEMPO Inizio="0.000" Bpm="100.00" Metro="4/4" Battito="1"/>
      <TEMPO Inizio="3.000" Bpm="120.00" Metro="4/4" Battito="1"/>
    </TRACK>
    <TRACK TrackID="3" Name="Waltz" TotalTime="6" AverageBpm="90.00" Tonality="F#m"
           Location="file://localhost/C:/Music/waltz.wav">
      <TEMPO Inizio="0.000" Bpm="90.00" Metro="3/4" Battito="1"/>
    </TRACK>
    <TRACK TrackID="4" Name="Not analyzed" AverageBpm="0.00" Tonality=""
           Location="file://localhost/Users/pam/Music/new.mp3"/>
  </COLLECTION>
</DJ_PLAYLISTS>
"""


@pytest.fixture
def xml(tmp_path):
    p = tmp_path / "rekordbox.xml"
    p.write_text(XML)
    return p


def test_parse_xml(xml):
    tracks = rekordbox.parse_xml(xml)
    assert len(tracks) == 3  # the unanalyzed one is skipped
    a, b, c = tracks
    assert a.location == "/Users/pam/Music/Night Shift.mp3"
    assert (a.bpm, a.tonic, a.mode) == (120.0, 9, "minor")
    assert a.beats[:3] == [0.5, 1.0, 1.5]
    # the anchor is beat 3 of its bar, so the first downbeat is two beats later
    assert a.downbeats[:2] == [1.5, 3.5]
    assert a.memory_cues == [0.5] and a.hot_cues == [2.5]  # the loop is left out
    assert (b.tonic, b.mode) == (0, "major")  # 8B is C major
    assert b.grid_anchors == 2
    assert b.beats[:6] == [0.0, 0.6, 1.2, 1.8, 2.4, 3.0] and b.beats[6] == 3.5
    assert c.location == "C:/Music/waltz.wav" and c.beats_per_bar == 3
    assert c.downbeats[:2] == [0.0, 2.0]


def test_match_by_file_name(xml):
    tracks = rekordbox.parse_xml(xml)
    found = rekordbox.match(tracks, ["/data/gs/audio/10001.LOFI.mp3", "/elsewhere/night shift.mp3", None, "/x/y.mp3"])
    assert found[0].name == "Drift" and found[1].name == "Night Shift"
    assert set(found) == {0, 1}


def test_score_next_to_tastr(xml):
    items = [
        Item(id="a", path="/d/Night Shift.mp3", truth=Truth(bpm=120, tonic=9, mode="minor")),
        Item(id="b", path="/d/10001.LOFI.mp3", truth=Truth(bpm=50, tonic=7, mode="major")),
        Item(id="c", path="/d/missing.mp3", truth=Truth(bpm=128)),
    ]
    tastr_rows = [{"id": "a", "acc1": 0.0, "key_correct": 1.0}, {"id": "b", "acc1": 1.0, "key_correct": 0.0},
                  {"id": "c", "acc1": 1.0}]
    cmp = rekordbox.score(items, xml, tastr_rows)
    assert (cmp["matched"], cmp["unmatched"]) == (2, 1)
    rb = cmp["rekordbox"]
    assert rb["acc1"] == 0.5 and rb["acc2"] == 1.0  # 100 for 50 is a double-time error
    assert rb["key_categories"] == {"correct": 1, "fifth": 1}
    assert cmp["tastr"]["acc1"] == 0.5 and cmp["tastr"]["acc1_n"] == 2  # track c isn't compared
    text = rekordbox.format_comparison(cmp)
    assert "Rekordbox vs Tastr on the 2 tracks" in text and "BPM within 4%" in text


def test_rekordbox_dataset_finds_audio_by_name(tmp_path, xml):
    (tmp_path / "audio").mkdir()
    (tmp_path / "audio" / "Night Shift.mp3").write_bytes(b"")
    items = datasets.load("rekordbox", str(tmp_path))  # a folder holding rekordbox.xml
    assert len(items) == 1
    it = items[0]
    assert it.path.endswith("Night Shift.mp3")
    assert it.truth.bpm == 120 and it.truth.downbeats[0] == 1.5
    assert it.meta["rekordbox_key"] == "Am"
