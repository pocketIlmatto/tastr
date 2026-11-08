"""How the card's "mode uncertain" flag (engine-rs music.rs `mode_uncertain`) does on key-profile files from
`tastr key-profiles` (which runs on Modal; `--limit N` for a small subset). No audio needed:

    python -m tastr.engine.bench.key_mode fmak-key-profiles.json fmak-new-key-profiles.json gs-key-profiles.json \\
        --tags fmak-tags.json --genres Rock,Blues,Punk

For each group it prints how many tracks get the flag and, of those whose tonic the model gets right, how often
the mode is wrong with and without the flag. `--tags` is a JSON object of track id -> {"genre": ...} (read from
the mp3s' ID3 tags); tracks it names are grouped by genre too. See docs/bench.md "Mode uncertain".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

from . import train_key

MUSIC_RS = Path(__file__).resolve().parents[3] / "engine-rs" / "src" / "music.rs"


def thresholds(path: Path = MUSIC_RS) -> tuple[float, float]:
    """(MODE_UNCERTAIN_THIRD, MODE_UNCERTAIN_MARGIN) as music.rs sets them."""
    src = path.read_text()
    get = lambda name: float(re.search(rf"pub const {name}: f64 = ([0-9.]+);", src).group(1))  # noqa: E731
    return get("MODE_UNCERTAIN_THIRD"), get("MODE_UNCERTAIN_MARGIN")


def third_contrast(profile, tonic: int) -> float:
    """music.rs third_contrast: |major 3rd - minor 3rd| / their sum, above `tonic`."""
    maj, mnr = profile[(tonic + 4) % 12], profile[(tonic + 3) % 12]
    return 0.0 if maj + mnr <= 0 else abs(maj - mnr) / (maj + mnr)


def estimate(profile, weights, bias, limits: tuple[float, float]) -> dict:
    """The key model's answer with music.rs's mode_margin and mode_uncertain."""
    profile = np.asarray(profile, dtype=float)
    p = train_key.key_probabilities(profile, weights, bias)
    best = int(np.argmax(p))
    tonic, minor = best // 2, best % 2
    margin = float(p[best] - p[best ^ 1])
    tc = third_contrast(profile, tonic)
    return {
        "tonic": tonic,
        "mode": "minor" if minor else "major",
        "mode_margin": margin,
        "third_contrast": tc,
        "mode_uncertain": bool(tc < limits[0] and margin < limits[1]),
    }


def rows(files: list[Path], tags: dict) -> list[dict]:
    weights, bias = train_key.model()
    limits = thresholds()
    out = []
    for f in files:
        tracks = [
            {**t, "profile": np.asarray(t["profile"], dtype=float)} for t in json.loads(Path(f).read_text())["tracks"]
        ]
        for t in train_key.usable([t for t in tracks if t.get("tonic") is not None and t["tonic"] >= 0]):
            est = estimate(t["profile"], weights, bias, limits)
            out.append(
                {
                    "id": t["id"],
                    "dataset": t["dataset"],
                    "genre": tags.get(t["id"], {}).get("genre"),
                    "ref_tonic": t["tonic"],
                    "ref_mode": t["mode"],
                    **est,
                }
            )
    return out


def summary(rs: list[dict]) -> dict:
    flagged = [r for r in rs if r["mode_uncertain"]]
    right = [r for r in rs if r["tonic"] == r["ref_tonic"]]
    f_right = [r for r in right if r["mode_uncertain"]]
    u_right = [r for r in right if not r["mode_uncertain"]]
    wrong = lambda s: sum(r["mode"] != r["ref_mode"] for r in s)  # noqa: E731
    return {
        "tracks": len(rs),
        "flagged": len(flagged),
        "tonic_right": len(right),
        "mode_wrong_flagged": wrong(f_right),
        "tonic_right_flagged": len(f_right),
        "mode_wrong_unflagged": wrong(u_right),
        "tonic_right_unflagged": len(u_right),
        "major_read_minor": sum(r["ref_mode"] == "major" and r["mode"] == "minor" for r in rs),
        "major_read_minor_flagged": sum(r["ref_mode"] == "major" and r["mode"] == "minor" for r in flagged),
    }


def line(name: str, s: dict) -> str:
    pct = lambda a, b: f"{a}/{b} ({a / b:.0%})" if b else "-"  # noqa: E731
    return (
        f"{name:28s} flagged {pct(s['flagged'], s['tracks']):>16s}   tonic right, mode wrong: "
        f"flagged {pct(s['mode_wrong_flagged'], s['tonic_right_flagged']):>14s}, "
        f"not {pct(s['mode_wrong_unflagged'], s['tonic_right_unflagged']):>14s}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("profiles", nargs="+", type=Path, help="key-profile files from `tastr key-profiles`")
    ap.add_argument("--tags", type=Path, help="JSON: track id -> {genre: ...}")
    ap.add_argument("--genres", default="", help="comma-separated genres to report together as one group too")
    ap.add_argument("--json", type=Path, help="also write every track's row here")
    a = ap.parse_args(argv)
    rs = rows(a.profiles, json.loads(a.tags.read_text()) if a.tags else {})
    groups: dict[str, list[dict]] = {}
    for r in rs:
        groups.setdefault(r["dataset"], []).append(r)
    picked = {g.strip() for g in a.genres.split(",") if g.strip()}
    if picked:
        groups[" + ".join(sorted(picked))] = [r for r in rs if r["genre"] in picked]
        groups["other genres"] = [r for r in rs if r["genre"] and r["genre"] not in picked]
    for name, g in groups.items():
        if g:
            print(line(name, summary(g)))
    if a.json:
        a.json.write_text(json.dumps(rs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
