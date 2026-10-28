import sys
import types

import pytest
from synth import make_track, write_wav

from tastr.ingest import IngestError, resolve_inputs
from tastr.resolvers import ytdlp

SITE = "https://example.com"


def _info(vid, title="Song", artist="Artist"):
    return {
        "id": vid,
        "extractor": "testsite",
        "title": title,
        "artist": artist,
        "webpage_url": f"{SITE}/track/{vid}",
    }


@pytest.fixture
def fake_yt_dlp(monkeypatch):
    """Install a fake yt_dlp module. Metadata calls return `catalog[url]`; download calls write a wav
    to the outtmpl path, or raise if the url is in `fail_urls`. `downloads` records each download."""
    state = types.SimpleNamespace(catalog={}, fail_urls=set(), downloads=[])
    audio, _ = make_track(bpm=120, bars_per_section=(1, 1, 1))

    class FakeYoutubeDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            if not download:
                return state.catalog[url]
            if url in state.fail_urls:
                raise RuntimeError("HTTP Error 403")
            state.downloads.append(url)
            write_wav(self.opts["outtmpl"].replace("%(ext)s", "wav"), audio)
            return state.catalog.get(url, {})

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    return state


def test_single_track_link(fake_yt_dlp, tmp_path):
    fake_yt_dlp.catalog[f"{SITE}/track/1"] = _info("1", title="Song", artist="Artist")

    sources = resolve_inputs([f"{SITE}/track/1"], cache_dir=tmp_path)

    assert len(sources) == 1
    src = sources[0]
    assert src.kind == "url"
    assert src.uri == f"{SITE}/track/1"
    assert src.local_path and (tmp_path / "testsite-1.wav").exists()
    assert (src.title, src.artist) == ("Song", "Artist")


def test_playlist_skips_failing_entry(fake_yt_dlp, tmp_path):
    entries = [_info(v) for v in ("a", "b", "c")]
    fake_yt_dlp.catalog[f"{SITE}/playlist/1"] = {"_type": "playlist", "id": "1", "entries": entries}
    fake_yt_dlp.fail_urls.add(f"{SITE}/track/b")

    sources = resolve_inputs([f"{SITE}/playlist/1"], cache_dir=tmp_path)

    assert [s.uri for s in sources] == [f"{SITE}/track/a", f"{SITE}/track/c"]


def test_playlist_is_capped(fake_yt_dlp, tmp_path, monkeypatch):
    monkeypatch.setattr(ytdlp, "MAX_PLAYLIST", 2)
    entries = [_info(v) for v in ("a", "b", "c")]
    fake_yt_dlp.catalog[f"{SITE}/playlist/1"] = {"_type": "playlist", "id": "1", "entries": entries}

    sources = resolve_inputs([f"{SITE}/playlist/1"], cache_dir=tmp_path)

    assert len(sources) == 2


def test_second_call_does_not_download_again(fake_yt_dlp, tmp_path):
    fake_yt_dlp.catalog[f"{SITE}/track/1"] = _info("1")

    first = resolve_inputs([f"{SITE}/track/1"], cache_dir=tmp_path)
    second = resolve_inputs([f"{SITE}/track/1"], cache_dir=tmp_path)

    assert len(fake_yt_dlp.downloads) == 1
    assert second[0].local_path == first[0].local_path


def test_existing_file_is_not_downloaded(fake_yt_dlp, tmp_path):
    fake_yt_dlp.catalog[f"{SITE}/track/1"] = _info("1")
    write_wav(tmp_path / "testsite-1.wav", make_track(bpm=120, bars_per_section=(1, 1, 1))[0])

    sources = resolve_inputs([f"{SITE}/track/1"], cache_dir=tmp_path)

    assert fake_yt_dlp.downloads == []
    assert sources[0].local_path == str(tmp_path / "testsite-1.wav")


@pytest.mark.parametrize(
    "url, service",
    [("https://open.spotify.com/track/abc", "Spotify"), ("https://music.apple.com/us/album/x/1", "Apple Music")],
)
def test_streaming_links_are_refused(fake_yt_dlp, tmp_path, url, service):
    with pytest.raises(IngestError, match=service):
        resolve_inputs([url], cache_dir=tmp_path)
    assert fake_yt_dlp.downloads == []


def test_missing_yt_dlp_asks_for_extra(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "yt_dlp", None)  # makes `import yt_dlp` fail

    with pytest.raises(IngestError, match=r"pip install 'tastr\[links\]'"):
        resolve_inputs([f"{SITE}/track/1"], cache_dir=tmp_path)
