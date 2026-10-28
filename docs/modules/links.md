# Module: link and playlist ingestion

## Goal
Let `tastr analyze <url>` work for track and playlist links by downloading
audio, using yt-dlp (works for YouTube, SoundCloud, Bandcamp, Mixcloud and
many others).

## Files
- New: `tastr/resolvers/ytdlp.py`; import it from `tastr/resolvers/__init__.py`.
- New: `tests/test_links.py`.
- Edit `pyproject.toml` only to adjust the `links` extra if needed.

## Interface (`tastr/ingest.py`)
```python
class UrlResolver(Protocol):
    name: str
    def handles(self, url: str) -> bool: ...
    def resolve(self, url: str, cache_dir: Path) -> list[Source]: ...
register_url_resolver(resolver)
```
Return one `Source` per track: `kind="url"`, `uri` = the track's own page URL
(for playlists, each entry's URL, not the playlist's), `local_path` = the
downloaded audio file, `title` and `artist` from metadata.

## Behaviour
1. `handles()` returns True for any http(s) URL except Spotify/Apple Music (see 5) when `yt_dlp` is importable.
2. Download best audio only, no video, into `cache_dir / "<extractor>-<id>.<ext>"`. If the file already exists, skip the download (re-analysis of a cached link is offline).
3. Playlists: expand entries; download each; a failing entry is skipped and logged, not fatal. Cap at 200 entries (constant `MAX_PLAYLIST`).
4. Use the Python API (`yt_dlp.YoutubeDL`), not a subprocess. Keep `import yt_dlp` inside functions so Tastr works without it installed.
5. Spotify / Apple Music links: audio can't be downloaded. Register a second resolver that handles `open.spotify.com` / `music.apple.com` and raises `IngestError` with a clear message ("streaming links can't be analyzed directly; ... "). Do not attempt to search other sites for the track.

## Tests (`tests/test_links.py`), no network
Monkeypatch `yt_dlp.YoutubeDL` with a fake whose `extract_info` returns canned
dicts (single video; playlist with 3 entries, one raising) and writes a short
wav from `tests/synth.py` to the expected path.

## Acceptance criteria
1. Single-track URL → one Source with `kind="url"`, existing `local_path`, title/artist from metadata.
2. Playlist with one failing entry → 2 Sources, no exception.
3. Second call for the same URL does not call the downloader again.
4. Spotify URL → `IngestError` mentioning Spotify.
5. Without yt_dlp installed (simulate by making the import fail), an ordinary URL → `IngestError` telling the user to `pip install 'tastr[links]'`.
6. `python -m pytest -q` passes.
