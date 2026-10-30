"""One-off Modal job: copy the Harmonix Set mel spectrograms and annotations into the R2 bucket.

Harmonix doesn't distribute audio; its authors share mel spectrograms on Dropbox
(Harmonix_melspecs.tgz, ~1.2 GB, linked from github.com/urinieto/harmonixset). Cloud sessions can't
reach Dropbox, so this runs in a Modal container, which downloads and unpacks it and writes

    harmonix/melspecs.npz         one array per track id (e.g. "0001_12step"), as shipped (pickled .npy files are skipped)
    harmonix/melspecs-info.json   the archive's info.json (sample rate, hop, bands)
    harmonix/Harmonix_melspecs.tgz the archive itself
    harmonix/annotations.json     {track id: downbeats, section boundaries, end} from the repo

Run it from the clone (no deploy needed; it uses the same `tastr-r2` secret as the app):

    TASTR_R2_ENDPOINT=https://<account id>.r2.cloudflarestorage.com modal run scripts/fetch_harmonix_mels.py
"""

from __future__ import annotations

import os

import modal

BUCKET = os.environ.get("TASTR_R2_BUCKET", "tastr-data")
ENDPOINT = os.environ.get("TASTR_R2_ENDPOINT", "")
if modal.is_local() and not ENDPOINT:
    raise SystemExit("set TASTR_R2_ENDPOINT to https://<account id>.r2.cloudflarestorage.com first")

MELS_URL = "https://www.dropbox.com/s/zxnqlx0hxz0lsyc/Harmonix_melspecs.tgz?dl=1"
REPO_URL = "https://codeload.github.com/urinieto/harmonixset/tar.gz/refs/heads/main"
OUT = "/data/harmonix"

app = modal.App("tastr-fetch-harmonix")
bucket = modal.CloudBucketMount(BUCKET, bucket_endpoint_url=ENDPOINT, secret=modal.Secret.from_name("tastr-r2"))


@app.function(image=modal.Image.debian_slim(python_version="3.12").pip_install("numpy<2.3"), cpu=2,
              memory=8192, timeout=3600, volumes={"/data": bucket})
def fetch() -> dict:
    import time
    from pathlib import Path

    t0 = time.time()
    out = Path(OUT)
    if (out / "melspecs.npz").exists():  # an earlier run got this far: only redo the annotations
        report = _describe(out)
    else:
        report = _mels(out)
    report |= {"annotation_files": _annotations(out), "total_s": round(time.time() - t0)}
    return report


def _mels(out) -> dict:
    import io
    import json
    import shutil
    import tarfile
    import time
    import urllib.request
    from pathlib import Path

    import numpy as np

    t0 = time.time()
    tgz = Path("/tmp/mels.tgz")
    req = urllib.request.Request(MELS_URL, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req) as r, tgz.open("wb") as fh:
        shutil.copyfileobj(r, fh, 1 << 22)
    t_download = time.time() - t0

    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tgz, out / "Harmonix_melspecs.tgz")  # kept so a rerun needn't hit Dropbox again

    arrays, info, other, pickled = {}, None, [], []
    with tarfile.open(tgz) as tar:
        for m in tar:
            if not m.isfile():
                continue
            name = Path(m.name).name
            if name.startswith("._"):  # macOS AppleDouble metadata next to each file, not arrays
                continue
            data = tar.extractfile(m).read()
            if name.endswith(".npy"):
                try:  # never unpickle: object arrays could run code
                    arrays[name.split(".")[0].removesuffix("-mel")] = np.load(io.BytesIO(data), allow_pickle=False)
                except ValueError:
                    pickled.append(m.name)
            elif name == "info.json":
                info = json.loads(data)
            else:
                other.append(m.name)
    tgz.unlink()

    local = Path("/tmp/melspecs.npz")
    np.savez(local, **arrays)
    shutil.copyfile(local, out / "melspecs.npz")
    (out / "melspecs-info.json").write_text(json.dumps(info, indent=1))

    shapes = sorted({(a.shape[0], str(a.dtype)) for a in arrays.values()})
    return {"tracks": len(arrays), "info": info, "bands_dtype": shapes[:5], "other_files": other[:10],
            "pickled": len(pickled), "pickled_examples": pickled[:5], "example": next(iter(arrays)) if arrays else None,
            "npz_mb": round(local.stat().st_size / 1e6, 1), "download_s": round(t_download)}


def _describe(out) -> dict:
    """What an earlier run left in the bucket (reads only the npz index and one array)."""
    import json

    import numpy as np

    npz = np.load(out / "melspecs.npz")
    first = npz[npz.files[0]] if npz.files else None
    return {"tracks": len(npz.files), "example": npz.files[0] if npz.files else None,
            "example_shape_dtype": [list(first.shape), str(first.dtype)] if first is not None else None,
            "info": json.loads((out / "melspecs-info.json").read_text())}


def _annotations(out) -> int:
    """Segments and downbeats from the repo's default branch, bundled as one annotations.json.

    The tarball is downloaded to local disk first: streaming it while writing to the bucket mount
    let the connection go idle and drop.
    """
    import json
    import shutil
    import tarfile
    import urllib.request
    from pathlib import Path

    from_repo = Path("/tmp/harmonixset.tgz")
    with urllib.request.urlopen(REPO_URL) as r, from_repo.open("wb") as fh:
        shutil.copyfileobj(r, fh, 1 << 20)
    beats, segments = {}, {}
    with tarfile.open(from_repo) as tar:
        for m in tar:
            parts = Path(m.name).parts
            if not m.isfile() or len(parts) != 4 or parts[1] != "dataset" or not parts[3].endswith(".txt"):
                continue
            if parts[2] in ("segments", "beats_and_downbeats"):
                text = tar.extractfile(m).read().decode()
                (segments if parts[2] == "segments" else beats)[Path(parts[3]).stem] = text
    bundle = {}
    for sid, text in segments.items():
        if sid not in beats:
            continue
        downbeats = [float(c[0]) for c in (line.split() for line in beats[sid].splitlines())
                     if len(c) >= 2 and c[1] in ("1", "1.0")]
        bounds, end = [], None
        for line in text.splitlines():
            cols = line.split(maxsplit=1)
            if len(cols) == 2:
                if cols[1].strip().lower() == "end":
                    end = float(cols[0])
                else:
                    bounds.append(float(cols[0]))
        bundle[sid] = {"downbeats": downbeats, "boundaries": bounds, "end": end}
    local = Path("/tmp/annotations.json")
    local.write_text(json.dumps(bundle))
    shutil.copyfile(local, out / "annotations.json")
    return len(bundle)


@app.local_entrypoint()
def main():
    import json

    print(json.dumps(fetch.remote(), indent=1))
