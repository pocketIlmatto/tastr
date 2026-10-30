"""Fetch the vibe benchmark datasets (`tastr fetch`), on Modal straight into the bucket or into a local folder.

MTG-Jamendo ships its audio as 100 tar archives of about 1.6 GB (low quality), one per folder 00-99. Only
a few hundred tracks in each are labeled, so the archive is streamed and only labeled tracks are kept,
cut to the middle CLIP_SECONDS with ffmpeg (no re-encode). Nothing else from the archive is stored.
DEAM is one 1.3 GB zip of 45 s clips plus a small annotations zip; both are kept whole.

Both are for non-commercial research only (docs/vibes.md): keep them in the private bucket, and commit
only what's learned from them.
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


def fetch(dataset: str, dest: str | Path, archives: list[int] | None = None, progress=print) -> dict:
    """Fetch `dataset` into `dest` and write its item index (tastr/cloud/jobs.py) for fast remote runs."""
    from .jobs import write_index

    if dataset == "mtg-moods":
        result = fetch_mtg(dest, archives if archives is not None else list(range(6)), progress=progress)
    elif dataset == "deam":
        result = fetch_deam(dest, progress=progress)
    else:
        raise ValueError(f"unknown dataset {dataset!r}; choose from mtg-moods, deam")
    result["index"] = str(write_index(dataset, dest))
    return result
