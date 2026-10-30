"""AcousticBrainz (https://acousticbrainz.org): BPM and key from its 2022 dump, found through MusicBrainz.

AcousticBrainz stopped taking submissions in 2022 and its API has since shut down, but its data is
public domain (CC0) and was published as dumps. Tastr reads two of them, the "rhythm" and "tonal"
low-level feature CSVs (one row per submission: mbid, submission_offset, bpm / key_key, key_scale), and
keeps each recording's first submission in a small SQLite index (`build_index`, run by
`tastr fetch acousticbrainz`). The data is frozen: anything released after mid-2022 isn't in it.

A lookup is two steps:

1. MusicBrainz recording search by title and first-listed artist (https://musicbrainz.org/doc/MusicBrainz_API;
   a whole multi-artist credit rarely matches theirs word for word). No key, but at most one request a
   second and a user agent that names the app; the guardrails space this provider's requests 1.1 s
   apart. Hits must match the title and the whole artist credit in any order (and the length, within
   LENGTH_SLACK, when both sides know it).
2. The matching recording ids (closest length first) in the index. The first one with a BPM or key is
   the answer. No network for this step.
3. MusicBrainz often lists one song as several recordings and AcousticBrainz analyzed only some of
   them, so when none is in the index, one more search finds the other recordings with the same ISRCs.

Without an index, ACOUSTICBRAINZ_API_BASE can name a server that still speaks the AcousticBrainz API
(its bulk /api/v1/low-level endpoint); then step 2 is one request for up to 25 ids.

Settings: ACOUSTICBRAINZ_INDEX (default $TASTR_HOME/acousticbrainz-index.sqlite),
ACOUSTICBRAINZ_API_BASE, MUSICBRAINZ_API_BASE. Both MusicBrainz's core data and the AcousticBrainz
dumps are CC0, so answers are stored.
"""

from __future__ import annotations

import codecs
import csv
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import uuid
from pathlib import Path
from typing import Iterable, Iterator

from . import register
from .base import LookupResult, Provider, ProviderError, TrackQuery, artist_names, num, same_artists, same_text

MUSICBRAINZ_BASE = "https://musicbrainz.org/ws/2"
DUMP_BASE = "https://data.metabrainz.org/pub/musicbrainz/acousticbrainz/dumps/acousticbrainz-lowlevel-features-20220623"
DUMP_FILES = ["acousticbrainz-lowlevel-features-20220623-rhythm.tar.zst",
              "acousticbrainz-lowlevel-features-20220623-tonal.tar.zst"]
INDEX_NAME = "acousticbrainz-index.sqlite"
LENGTH_SLACK = 15.0  # seconds a MusicBrainz recording's length may differ from the file's
SEARCH_LIMIT = 25
BULK_MAX = 25  # the AcousticBrainz API's limit per bulk request
FIELDS = ("bpm", "key_key", "key_scale")


def default_index() -> Path:
    home = Path(os.environ.get("TASTR_HOME", Path.home() / ".tastr"))
    return Path(os.environ.get("ACOUSTICBRAINZ_INDEX") or home / INDEX_NAME).expanduser()


# --- the index -------------------------------------------------------------------------------------------

SCHEMA = """CREATE TABLE IF NOT EXISTS recordings (
    mbid BLOB PRIMARY KEY,  -- the recording MBID's 16 bytes
    bpm REAL,
    key_key TEXT,
    key_scale TEXT
) WITHOUT ROWID"""


def _open_text(path: Path) -> Iterator[tuple[str, Iterable[str]]]:
    """(member name, text lines) for each CSV in a .csv, .tar, .tar.gz/.bz2/.xz or .tar.zst file."""
    name = path.name.lower()
    if name.endswith(".csv"):
        with path.open(newline="", encoding="utf-8") as fh:
            yield path.name, fh
        return
    if name.endswith((".zst", ".zstd")):
        yield from _tar_members(_zstd_reader(path))
        return
    with tarfile.open(path, "r|*") as tar:
        yield from _tar_csvs(tar)


def _tar_members(raw) -> Iterator[tuple[str, Iterable[str]]]:
    with raw, tarfile.open(fileobj=raw, mode="r|") as tar:
        yield from _tar_csvs(tar)


def _tar_csvs(tar: tarfile.TarFile) -> Iterator[tuple[str, Iterable[str]]]:
    for m in tar:
        if m.isfile() and m.name.lower().endswith(".csv"):
            # A streamed tar member isn't seekable, which io.TextIOWrapper wants; decode line by line.
            yield m.name, codecs.iterdecode(tar.extractfile(m), "utf-8")


def _zstd_reader(path: Path):
    """A binary stream of the decompressed file: the zstandard package when installed, else the zstd tool."""
    try:
        import zstandard

        return zstandard.ZstdDecompressor().stream_reader(path.open("rb"), closefd=True)
    except ImportError:
        pass
    exe = shutil.which("zstd")
    if exe is None:
        raise RuntimeError(f"{path.name} is zstd-compressed: `pip install zstandard` or install the zstd tool")
    proc = subprocess.Popen([exe, "-dc", str(path)], stdout=subprocess.PIPE)
    return proc.stdout


def _mbid_bytes(s: str) -> bytes | None:
    try:
        return uuid.UUID(s.strip()).bytes
    except (ValueError, AttributeError):
        return None


def build_index(sources: Iterable[str | Path], out: str | Path, progress=print, batch: int = 50_000) -> dict:
    """Read rhythm / tonal feature CSVs (bare or in their tar archives) into the index at `out`.

    Only each recording's first submission (submission_offset 0) is kept. Columns are matched by name, so
    the files can come in any order; a file adds its columns to rows another file already made.
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(out)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute(SCHEMA)
    rows_read = 0
    for src in sources:
        for member, fh in _open_text(Path(src)):
            reader = csv.DictReader(fh)
            cols = [c for c in FIELDS if c in (reader.fieldnames or [])]
            if "mbid" not in (reader.fieldnames or []) or not cols:
                progress(f"skipping {member}: no mbid or {'/'.join(FIELDS)} columns")
                continue
            progress(f"indexing {member} ({', '.join(cols)})")
            sql = (f"INSERT INTO recordings (mbid, {', '.join(cols)}) VALUES (?{', ?' * len(cols)}) "
                   f"ON CONFLICT(mbid) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols)}")
            pending = []
            for row in reader:
                rows_read += 1
                if (row.get("submission_offset") or "0").strip() != "0":
                    continue
                mbid = _mbid_bytes(row["mbid"])
                if mbid is None:
                    continue
                pending.append((mbid, *(_cell(c, row.get(c)) for c in cols)))
                if len(pending) >= batch:
                    with conn:
                        conn.executemany(sql, pending)
                    pending.clear()
            with conn:
                conn.executemany(sql, pending)
    recordings = conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]
    conn.close()
    return {"index": str(out), "rows_read": rows_read, "recordings": recordings}


def _cell(col: str, v: str | None):
    v = (v or "").strip().strip('"')
    if col == "bpm":
        return num(v)
    return v or None


def read_index(path: Path, mbids: list[str]) -> dict[str, dict]:
    """{mbid: {bpm, key_key, key_scale}} for the ids the index has."""
    keys = {b: m for m in mbids if (b := _mbid_bytes(m))}
    if not keys:
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        q = f"SELECT mbid, bpm, key_key, key_scale FROM recordings WHERE mbid IN ({', '.join('?' * len(keys))})"
        return {keys[r[0]]: {"bpm": r[1], "key_key": r[2], "key_scale": r[3]} for r in conn.execute(q, list(keys))}
    finally:
        conn.close()


# --- the provider ----------------------------------------------------------------------------------------

def _lucene(s: str) -> str:
    return '"' + re.sub(r'([\\"])', r"\\\1", s) + '"'


def _credit(rec: dict) -> str:
    return "".join((c.get("name") or (c.get("artist") or {}).get("name") or "") + (c.get("joinphrase") or "")
                   for c in rec.get("artist-credit") or [])


def _usable(f: dict | None) -> bool:
    return bool(f) and (num(f.get("bpm")) is not None or bool(f.get("key_key")))


class AcousticBrainz(Provider):
    name = "acousticbrainz"
    label = "AcousticBrainz"
    homepage = "https://acousticbrainz.org"
    key_env = None
    # MusicBrainz allows one request a second on average; stay a little under it.
    min_interval = 1.1
    daily_limit = 1000
    quota_headers = False  # MusicBrainz's x-ratelimit-* headers are its global load, not ours

    def index(self) -> Path:
        return default_index()

    def api_base(self) -> str | None:
        b = os.environ.get("ACOUSTICBRAINZ_API_BASE", "").strip()
        return b.rstrip("/") or None

    def configured(self) -> bool:
        return self.index().is_file() or self.api_base() is not None

    def setup_hint(self) -> str:
        return f"no index at {self.index()} (run `tastr fetch acousticbrainz`, see docs/lookup.md)"

    def search(self, query: str) -> list[dict]:
        """One MusicBrainz recording search. A dropped or cut-off connection, or a 503 (MusicBrainz is busy;
        it sends Retry-After: 0), is tried once more through the guardrails, which space it 1.1 s on."""
        base = os.environ.get("MUSICBRAINZ_API_BASE", MUSICBRAINZ_BASE).rstrip("/")
        params = {"query": query, "fmt": "json", "limit": str(SEARCH_LIMIT)}
        try:
            _, body = self.get_json(f"{base}/recording", params, source="MusicBrainz")
        except ProviderError as e:
            if e.status not in (None, 503):
                raise
            _, body = self.get_json(f"{base}/recording", params, source="MusicBrainz")
        recs = (body or {}).get("recordings") if isinstance(body, dict) else None
        if not isinstance(recs, list):
            raise ProviderError("MusicBrainz sent an unexpected search response")
        return recs

    def candidates(self, q: TrackQuery) -> list[dict]:
        """MusicBrainz recordings that match the track ({mbid, artist, title, isrcs}), closest length first."""
        first = (artist_names(q.artist) or [q.artist])[0]
        recs = self.search(f"recording:{_lucene(q.title)} AND artist:{_lucene(first)}")
        hits = []
        for r in recs:
            if not (r.get("id") and same_text(r.get("title"), q.title) and same_artists(_credit(r), q.artist)):
                continue
            length = num(r.get("length"))
            diff = abs(length / 1000 - q.duration) if length and q.duration else None
            if diff is not None and diff > LENGTH_SLACK:
                continue
            hits.append((diff if diff is not None else LENGTH_SLACK, len(hits),
                         {"mbid": r["id"], "artist": _credit(r), "title": r.get("title"), "isrcs": r.get("isrcs") or []}))
        return [h for *_, h in sorted(hits, key=lambda h: h[:2])]

    def features(self, mbids: list[str]) -> dict[str, dict]:
        if self.index().is_file():
            return read_index(self.index(), mbids)
        _, body = self.get_json(f"{self.api_base()}/api/v1/low-level", {
            "recording_ids": ";".join(mbids[:BULK_MAX]), "features": "rhythm.bpm;tonal.key_key;tonal.key_scale"})
        out = {}
        for m, subs in (body if isinstance(body, dict) else {}).items():
            doc = subs.get("0") if isinstance(subs, dict) else None
            if isinstance(doc, dict):
                out[m] = {"bpm": num((doc.get("rhythm") or {}).get("bpm")), **{
                    k: (doc.get("tonal") or {}).get(k) for k in ("key_key", "key_scale")}}
        return out

    def same_isrc(self, hits: list[dict]) -> list[dict]:
        """Other MusicBrainz recordings sharing an ISRC with the hits: the same audio, listed again."""
        isrcs = sorted({i for h in hits for i in h["isrcs"]})[:10]
        if not isrcs:
            return []
        seen = {h["mbid"] for h in hits}
        recs = self.search("isrc:(" + " OR ".join(isrcs) + ")")
        return [{"mbid": r["id"], "artist": _credit(r), "title": r.get("title"), "isrcs": r.get("isrcs") or []}
                for r in recs if r.get("id") and r["id"] not in seen and set(r.get("isrcs") or []) & set(isrcs)]

    def _read(self, mbids: list[str]) -> dict[str, dict]:
        try:
            return self.features(mbids)
        except sqlite3.Error as e:
            raise ProviderError(f"Couldn't read the AcousticBrainz index at {self.index()}: {e}") from None

    def lookup(self, q: TrackQuery) -> LookupResult:
        hits = self.candidates(q)
        if not hits:
            return LookupResult(self.name, "not_found")
        found = self._read([h["mbid"] for h in hits])
        more: list[dict] = []
        if not any(_usable(found.get(h["mbid"])) for h in hits):
            more = self.same_isrc(hits)
            if more:
                hits += more
                found |= self._read([h["mbid"] for h in more])
        mbids = [h["mbid"] for h in hits]
        for h in hits:
            m = h["mbid"]
            f = found.get(m)
            if not _usable(f):
                continue
            key = " ".join(x for x in (f.get("key_key"), f.get("key_scale")) if x) or None
            return LookupResult(self.name, "found", bpm=num(f.get("bpm")), key=key,
                                matched_artist=h["artist"] or None, matched_title=h["title"],
                                url=f"https://musicbrainz.org/recording/{m}",
                                extra={"mbid": m, "candidates": len(mbids)})
        # On MusicBrainz but never analyzed by AcousticBrainz (or released after 2022).
        return LookupResult(self.name, "not_found", extra={"mbids": mbids[:3], "tried": len(mbids),
                                                           "same_isrc": len(more)})


register(AcousticBrainz())
