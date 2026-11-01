"""Uploaded audio and stems in an S3-compatible bucket (Cloudflare R2), with the local disk as a cache.

Off unless TASTR_AUDIO_BUCKET is set: then the server keeps every file under $TASTR_HOME/uploads and
$TASTR_HOME/stems in the bucket too, under the same path (`uploads/ab12cd34/song.mp3`), so a lost or replaced
disk loses no audio and the server no longer needs a volume for it.

- A file's name in the database (`tracks.local_path`) stays the path it would have on disk; `key()` maps it to
  the bucket.
- `push()` copies a new file up; `ensure()` brings a file back down when something here has to read it (a
  re-read of its tags, local analysis); `evict()` deletes local copies that are safely in the bucket.
- Browsers and strudel.cc play audio and stems from short-lived signed links (`signed_url()`); Modal reads the
  audio from one too, and writes stems to signed upload links (`put_url()`), so audio never passes through
  the server twice.

Settings (docs/hosting.md): TASTR_AUDIO_BUCKET, TASTR_AUDIO_ENDPOINT (for R2,
https://<account id>.r2.cloudflarestorage.com) and TASTR_AUDIO_ACCESS_KEY_ID / TASTR_AUDIO_SECRET_ACCESS_KEY,
a token that can read and write that bucket only. Needs boto3 (`pip install 'tastr[hosted]'`).
"""

from __future__ import annotations

import logging
import mimetypes
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

FOLDERS = ("uploads", "stems")  # what lives in the bucket, by its folder under TASTR_HOME
LINK_SECONDS = 3600  # signed links for playback and for Modal; a page asks again for a new one
LISTING_SECONDS = 60  # how long a bucket listing is reused (the storage cap, the admin view)
# Types the stdlib doesn't know everywhere; browsers need them to decode the stream.
AUDIO_TYPES = {".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".opus": "audio/ogg",
               ".ogg": "audio/ogg", ".aif": "audio/aiff", ".aiff": "audio/aiff", ".wav": "audio/wav", ".mp3": "audio/mpeg"}

_client = None
_client_lock = threading.Lock()
_listing: dict[str, tuple[float, dict[str, tuple[int, float]]]] = {}
_listing_lock = threading.Lock()


def bucket() -> str:
    return os.environ.get("TASTR_AUDIO_BUCKET", "").strip()


def enabled() -> bool:
    return bool(bucket())


def endpoint() -> str:
    return os.environ.get("TASTR_AUDIO_ENDPOINT", "").strip().rstrip("/")


def origin() -> str | None:
    """Where signed links point (scheme://host), for the page's Content-Security-Policy; None when off."""
    if not enabled() or not endpoint():
        return None
    parts = urlsplit(endpoint())
    return f"{parts.scheme}://{parts.netloc}"


def home() -> Path:
    return Path(os.environ.get("TASTR_HOME", Path.home() / ".tastr"))


def content_type(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    return AUDIO_TYPES.get(suffix) or mimetypes.guess_type(Path(path).name)[0] or "application/octet-stream"


def key(path: str | Path) -> str | None:
    """The bucket key for a file under TASTR_HOME's uploads or stems folder, else None (not kept in the bucket)."""
    try:
        rel = Path(path).resolve().relative_to(home().resolve())
    except ValueError:
        return None
    return rel.as_posix() if rel.parts and rel.parts[0] in FOLDERS and len(rel.parts) > 1 else None


def client():
    """The bucket's S3 client (boto3, made once)."""
    global _client
    with _client_lock:
        if _client is None:
            import boto3
            from botocore.config import Config

            _client = boto3.client(
                "s3", endpoint_url=endpoint() or None, region_name=os.environ.get("TASTR_AUDIO_REGION", "auto"),
                aws_access_key_id=os.environ.get("TASTR_AUDIO_ACCESS_KEY_ID") or None,
                aws_secret_access_key=os.environ.get("TASTR_AUDIO_SECRET_ACCESS_KEY") or None,
                config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                              retries={"max_attempts": 4, "mode": "standard"}))
        return _client


def reset() -> None:
    """Forget the client and listings (tests, or after the settings change)."""
    global _client
    with _client_lock:
        _client = None
    with _listing_lock:
        _listing.clear()


def reset_listings() -> None:
    """Read the bucket afresh next time (after something else, like Modal, wrote to it)."""
    _forget_listings()


def _forget_listings() -> None:
    with _listing_lock:
        _listing.clear()


# ---- one file ---------------------------------------------------------------------------------------------

def push(path: str | Path) -> bool:
    """Copy a local file up to its key. False (and nothing done) when off or the file isn't kept in the bucket."""
    k = key(path)
    if not enabled() or k is None:
        return False
    client().upload_file(str(path), bucket(), k, ExtraArgs={"ContentType": content_type(path)})
    _forget_listings()
    return True


def in_bucket(path: str | Path) -> bool:
    """Whether the bucket has the file (from the folder's listing, reused for a minute; push and remove refresh it)."""
    k = key(path)
    if not enabled() or k is None:
        return False
    return k in listing(k.split("/", 1)[0] + "/")


def exists(path: str | Path | None) -> bool:
    """Whether the file is here or in the bucket."""
    if not path:
        return False
    return Path(path).is_file() or in_bucket(path)


def ensure(path: str | Path | None) -> Path | None:
    """The file on local disk, downloaded from the bucket first if only the bucket has it; None if neither does."""
    if not path:
        return None
    p = Path(path)
    if p.is_file():
        return p
    if not in_bucket(p):
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    part = p.with_name(p.name + ".part")
    client().download_file(bucket(), key(p), str(part))
    part.replace(p)
    return p


def remove(path: str | Path | None) -> None:
    """Delete a file here and in the bucket."""
    if not path:
        return
    Path(path).unlink(missing_ok=True)
    k = key(path)
    if enabled() and k is not None:
        client().delete_object(Bucket=bucket(), Key=k)
        _forget_listings()


def remove_tree(folder: str | Path) -> None:
    """Delete every file under `folder` in the bucket (the local folder is the caller's)."""
    k = key(Path(folder) / "x")
    if not enabled() or k is None:
        return
    prefix = k[:-1]
    names = [{"Key": name} for name in listing(prefix, fresh=True)]
    for i in range(0, len(names), 1000):
        client().delete_objects(Bucket=bucket(), Delete={"Objects": names[i:i + 1000], "Quiet": True})
    _forget_listings()


def signed_url(path: str | Path, seconds: int = LINK_SECONDS) -> str | None:
    """A link anyone can GET the file from for `seconds` (Range requests work), or None when off."""
    k = key(path)
    if not enabled() or k is None:
        return None
    return client().generate_presigned_url("get_object", Params={"Bucket": bucket(), "Key": k},
                                           ExpiresIn=seconds)


def put_url(path: str | Path, seconds: int = LINK_SECONDS) -> str | None:
    """A link that can PUT the file's content once for `seconds` (Modal writes stems through these)."""
    k = key(path)
    if not enabled() or k is None:
        return None
    return client().generate_presigned_url("put_object", Params={"Bucket": bucket(), "Key": k,
                                                                 "ContentType": content_type(path)},
                                           ExpiresIn=seconds)


# ---- many files -------------------------------------------------------------------------------------------

def listing(prefix: str, fresh: bool = False) -> dict[str, tuple[int, float]]:
    """{key: (bytes, last modified as a Unix time)} for every object under `prefix`, reused for a minute."""
    if not enabled():
        return {}
    now = time.monotonic()
    with _listing_lock:
        hit = _listing.get(prefix)
        if hit and not fresh and now - hit[0] < LISTING_SECONDS:
            return hit[1]
    out: dict[str, tuple[int, float]] = {}
    for page in client().get_paginator("list_objects_v2").paginate(Bucket=bucket(), Prefix=prefix):
        for obj in page.get("Contents", []):
            out[obj["Key"]] = (obj["Size"], obj["LastModified"].timestamp())
    with _listing_lock:
        _listing[prefix] = (now, out)
    return out


def stored_bytes() -> int:
    """Bytes of audio and stems in the bucket."""
    return sum(size for f in FOLDERS for size, _ in listing(f + "/").values())


def evict(older_than: float = 3600, now: float | None = None) -> int:
    """Delete local copies under uploads/ and stems/ that the bucket has (same size) and that nobody has
    written for `older_than` seconds; returns how many. The disk is only a cache once the bucket is on."""
    if not enabled():
        return 0
    cutoff = (now or time.time()) - older_than
    kept = {k: size for f in FOLDERS for k, (size, _) in listing(f + "/", fresh=True).items()}
    gone, emptied = 0, set()
    for folder in FOLDERS:
        for f in (home() / folder).rglob("*") if (home() / folder).is_dir() else []:
            try:
                if not f.is_file() or f.stat().st_mtime >= cutoff or kept.get(key(f) or "") != f.stat().st_size:
                    continue
                f.unlink()
                emptied.add(f.parent)
                gone += 1
            except OSError as e:  # removed meanwhile, or in use; the next sweep tries again
                log.debug("can't evict %s: %s", f, e)
        for d in sorted((home() / folder).rglob("*"), reverse=True) if (home() / folder).is_dir() else []:
            try:  # folders left empty, unless one was just made (an upload or a separation about to write into it)
                if d.is_dir() and (d in emptied or d.stat().st_mtime < cutoff) and not any(d.iterdir()):
                    d.rmdir()
            except OSError as e:
                log.debug("can't remove %s: %s", d, e)
    return gone
