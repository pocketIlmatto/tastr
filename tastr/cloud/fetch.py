"""Fetch the vibe benchmark datasets (`tastr fetch`), on Modal straight into the bucket or into a local folder.

MTG-Jamendo ships its audio as 100 tar archives of about 1.6 GB (low quality), one per folder 00-99. Only
a few hundred tracks in each are labeled, so the archive is streamed and only labeled tracks are kept,
cut to the middle CLIP_SECONDS with ffmpeg (no re-encode). Nothing else from the archive is stored.
DEAM is one 1.3 GB zip of 45 s clips plus a small annotations zip; both are kept whole.

Both are for non-commercial research only (docs/vibes.md): keep them in the private bucket, and commit
only what's learned from them.

`acousticbrainz` isn't a benchmark: it's the 2022 AcousticBrainz rhythm and tonal feature dumps (CC0)
plus the SQLite index the AcousticBrainz lookup provider reads (tastr/lookup/acousticbrainz.py). The
index is built in a temporary folder and copied next to the archives, since the bucket mount can't take
SQLite's random writes.

`fma-keys` is FMA Keys (FMAK, Zenodo 10719860): expert key and mode labels for 5,489 Free Music Archive
songs across 17 genres, about half major. The audio is full songs in 10 zips of 1.7 to 5 GB, split by FMA
folder (track id // 1000); `archives` picks which (0 is `000-019.zip`, 695 labeled songs). Each zip is
checked against Zenodo's MD5 before it's unpacked. The record is CC BY 4.0, but the dataset's notes put
the labels to non-commercial research use, so they are treated like the vibe sets: bucket only.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

MTG_REPO = "https://raw.githubusercontent.com/MTG/mtg-jamendo-dataset/master"
MTG_LABEL_FILES = {  # saved under these names, which tastr.engine.bench.vibes reads
    "annotations-clean.tsv": "derived/music-classification-annotations/music-classification-annotations-clean.tsv",
    "moodtheme.tsv": "data/autotagging_moodtheme.tsv",
}
MTG_AUDIO = "https://cdn.freesound.org/mtg-jamendo/raw_30s/audio-low/raw_30s_audio-low-{:02d}.tar"
MTG_TAR_SHA256 = "data/download/raw_30s_audio-low_sha256_tars.txt"  # in MTG_REPO
DEAM_URLS = ["https://cvml.unige.ch/databases/DEAM/DEAM_audio.zip",
             "https://cvml.unige.ch/databases/DEAM/DEAM_Annotations.zip"]
CLIP_SECONDS = 60
FMAK_RECORD = "https://zenodo.org/api/records/10719860"
FMAK_LABELS = "fma_keys_metadata.csv"
FMAK_ZIPS = ["000-019.zip", "020-039.zip", "040-049.zip", "050-059.zip", "060-069.zip", "070-079.zip",
             "080-089.zip", "090-099.zip", "100-109.zip", "110-124.zip"]


def _get(url: str):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "tastr-fetch"}), timeout=120)


def _save(url: str, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with _get(url) as r, out.open("wb") as fh:
        shutil.copyfileobj(r, fh, 1 << 20)
    return out


class _Hashing:
    """A read-only stream wrapper that hashes what passes through it."""

    def __init__(self, raw):
        self.raw, self.sha = raw, hashlib.sha256()

    def read(self, n=-1):
        b = self.raw.read(n)
        self.sha.update(b)
        return b


def clip(src: Path, dst: Path, duration: float, seconds: int = CLIP_SECONDS) -> None:
    """The middle `seconds` of an mp3, copied without re-encoding (the whole file when it's shorter)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if duration <= seconds:
        shutil.copyfile(src, dst)
        return
    start = max(0.0, duration / 2 - seconds / 2)
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{start:.1f}", "-t", str(seconds),
                    "-i", str(src), "-c", "copy", str(dst)], check=True)


def fetch_mtg(dest: str | Path, archives: list[int], audio_url: str = MTG_AUDIO, repo: str = MTG_REPO,
              progress=print) -> dict:
    """Labels plus the labeled tracks of the given archives (folders), clipped, into `dest`."""
    from ..engine.bench import vibes

    dest = Path(dest)
    for name, src in MTG_LABEL_FILES.items():
        _save(f"{repo}/{src}", dest / name)
    labeled = vibes.mtg_labels(dest)
    wanted = {e["path"].replace(".mp3", ".low.mp3"): e for e in labeled.values()}
    with _get(f"{repo}/{MTG_TAR_SHA256}") as r:
        sums = {ln.split()[1]: ln.split()[0] for ln in r.read().decode().splitlines() if ln.strip()}
    kept = skipped = 0
    bad = []
    for n in archives:
        url = audio_url.format(n)
        progress(f"archive {n:02d}: streaming {url}")
        got = 0
        with _get(url) as r, tempfile.TemporaryDirectory() as tmp:
            stream = _Hashing(r)
            with tarfile.open(fileobj=stream, mode="r|") as tar:
                for m in tar:
                    e = wanted.get(m.name)
                    if not m.isfile() or e is None:
                        continue
                    out = dest / "audio" / e["path"]
                    if out.exists():
                        skipped += 1
                        continue
                    raw, cut = Path(tmp) / "track.mp3", Path(tmp) / "clip.mp3"
                    with tar.extractfile(m) as fh, raw.open("wb") as w:
                        shutil.copyfileobj(fh, w)
                    # Cut on local disk, then copy: the bucket mount takes only sequential writes, and
                    # ffmpeg seeks back to finish an mp3's header.
                    clip(raw, cut, e["duration"])
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(cut, out)
                    got += 1
            while stream.read(1 << 20):  # hash the tar's padding too
                pass
        name = Path(url).name
        if name in sums and stream.sha.hexdigest() != sums[name]:
            bad.append(name)  # a truncated stream: rerun to fill in (existing clips are skipped)
        kept += got
        progress(f"archive {n:02d}: kept {got} labeled tracks")
    return {"dataset": "mtg-moods", "kept": kept, "already_there": skipped, "checksum_mismatch": bad,
            "labeled_tracks": len(labeled)}


def fetch_deam(dest: str | Path, urls: list[str] = DEAM_URLS, progress=print) -> dict:
    """The DEAM audio and annotation zips, unpacked into `dest`."""
    dest = Path(dest)
    files = 0
    for url in urls:
        progress(f"downloading {url}")
        with tempfile.TemporaryDirectory() as tmp:
            z = _save(url, Path(tmp) / "deam.zip")
            with zipfile.ZipFile(z) as zf:
                for m in zf.infolist():
                    if m.is_dir() or "__MACOSX" in m.filename or Path(m.filename).name.startswith("._"):
                        continue
                    out = dest / m.filename
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(m) as fh, out.open("wb") as w:
                        shutil.copyfileobj(fh, w)
                    files += 1
    return {"dataset": "deam", "files": files}


def fetch_acousticbrainz(dest: str | Path, base: str | None = None, progress=print) -> dict:
    """The rhythm and tonal feature archives (skipped when already there) and the lookup index built from them."""
    from ..lookup import acousticbrainz as ab

    dest = Path(dest)
    base = (base or ab.DUMP_BASE).rstrip("/")
    archives = []
    for name in ab.DUMP_FILES:
        out = dest / name
        if out.is_file() and out.stat().st_size > 0:
            progress(f"have {name}")
        else:
            progress(f"downloading {base}/{name}")
            _save(f"{base}/{name}", out)
        archives.append(out)
    with tempfile.TemporaryDirectory() as tmp:
        result = ab.build_index(archives, Path(tmp) / ab.INDEX_NAME, progress=progress)
        shutil.copyfile(result["index"], dest / ab.INDEX_NAME)
    sizes = {p.name: p.stat().st_size for p in [*archives, dest / ab.INDEX_NAME]}
    return {"dataset": "acousticbrainz", **result, "index": str(dest / ab.INDEX_NAME), "bytes": sizes}


def fetch_fma_keys(dest: str | Path, archives: list[int], record: str = FMAK_RECORD, progress=print) -> dict:
    """The FMA Keys labels plus the songs in the given zips (indexes into FMAK_ZIPS), unpacked into `dest`/audio."""
    import json

    dest = Path(dest)
    with _get(record) as r:
        files = {f["key"]: f for f in json.load(r)["files"]}
    _save(files[FMAK_LABELS]["links"]["self"], dest / FMAK_LABELS)
    kept = skipped = 0
    bad = []
    for n in archives:
        name = FMAK_ZIPS[n]
        progress(f"downloading {name} ({files[name]['size'] / 1e9:.2f} GB)")
        with tempfile.TemporaryDirectory() as tmp:
            # Download to local disk first: unzipping needs seeks, which the bucket mount can't do.
            z = _save(files[name]["links"]["self"], Path(tmp) / name)
            md5 = hashlib.md5()
            with z.open("rb") as fh:
                for b in iter(lambda: fh.read(1 << 20), b""):
                    md5.update(b)
            if files[name].get("checksum") != f"md5:{md5.hexdigest()}":
                bad.append(name)  # a cut-off download: rerun this archive
                continue
            with zipfile.ZipFile(z) as zf:
                for m in zf.infolist():
                    if m.is_dir() or not m.filename.endswith(".mp3") or Path(m.filename).name.startswith("._"):
                        continue
                    out = dest / "audio" / Path(*Path(m.filename).parts[-2:])  # <folder>/<track id>.mp3
                    if out.exists():
                        skipped += 1
                        continue
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(m) as fh, out.open("wb") as w:
                        shutil.copyfileobj(fh, w)
                    kept += 1
        progress(f"{name}: unpacked")
    return {"dataset": "fma-keys", "kept": kept, "already_there": skipped, "checksum_mismatch": bad}


def fetch(dataset: str, dest: str | Path, archives: list[int] | None = None, progress=print) -> dict:
    """Fetch `dataset` into `dest` and write its item index (tastr/cloud/jobs.py) for fast remote runs."""
    from .jobs import write_index

    if dataset == "acousticbrainz":  # a lookup index, not a benchmark: no item index
        return fetch_acousticbrainz(dest, progress=progress)
    if dataset == "mtg-moods":
        result = fetch_mtg(dest, archives if archives is not None else list(range(6)), progress=progress)
    elif dataset == "deam":
        result = fetch_deam(dest, progress=progress)
    elif dataset == "fma-keys":
        result = fetch_fma_keys(dest, archives if archives is not None else [0], progress=progress)
    else:
        raise ValueError(f"unknown dataset {dataset!r}; choose from mtg-moods, deam, acousticbrainz, fma-keys")
    result["index"] = str(write_index(dataset, dest))
    return result
