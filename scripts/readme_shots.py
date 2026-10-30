"""Regenerate the README screenshots in docs/images/ from the current web UI.

Builds a throwaway library of three synthetic tracks (nothing touches ~/.tastr), starts
`tastr serve` on it, and has scripts/readme-shots.js take the shots with Playwright.

    python scripts/readme_shots.py            # writes docs/images/*.png

Needs ffmpeg (as for analysis), node, and the playwright npm package with its Chromium
(`npm install -g playwright && npx playwright install chromium`). Set TASTR_CHROMIUM to use
a Chromium that is already installed. The GitHub workflow readme-shots.yml runs this after a
PR labeled `readme-shots` merges and opens a PR with the new images.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"

# Title, BPM, tonic (semitones above C), mode. "Night Shift" is the one on screen.
TRACKS = [("Sunline", 118, 0, "major"), ("Low Orbit", 128, 2, "minor"), ("Night Shift", 124, 9, "minor")]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    from tastr.engine.bench.synth import make_track, write_wav

    tastr = [sys.executable, "-m", "tastr"]
    with tempfile.TemporaryDirectory(prefix="tastr-shots-") as tmp:
        env = {**os.environ, "TASTR_HOME": tmp, "TASTR_DB": str(Path(tmp) / "tastr.db")}
        files = []
        for title, bpm, tonic, mode in TRACKS:  # analyzed in this order, so the list shows the last first
            y, _ = make_track(bpm=bpm, tonic=tonic, mode=mode)
            files.append(Path(tmp) / f"{title}.wav")
            write_wav(files[-1], y)
            subprocess.run([*tastr, "analyze", str(files[-1])], env=env, check=True, stdout=subprocess.DEVNULL)

        port = free_port()
        base = f"http://127.0.0.1:{port}"
        server = subprocess.Popen([*tastr, "serve", "--port", str(port), "--log-level", "warning"], env=env)
        try:
            for _ in range(60):
                try:
                    urllib.request.urlopen(f"{base}/api/tracks", timeout=1)
                    break
                except OSError:
                    time.sleep(0.5)
            else:
                raise SystemExit("tastr serve didn't start")
            OUT.mkdir(parents=True, exist_ok=True)
            node_env = dict(os.environ)
            if "NODE_PATH" not in node_env and shutil.which("npm"):
                node_env["NODE_PATH"] = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
            subprocess.run(["node", str(ROOT / "scripts" / "readme-shots.js"), base, str(OUT)], env=node_env, check=True)
        finally:
            server.terminate()
            server.wait(timeout=10)
    for p in sorted(OUT.glob("*.png")):
        print(f"{p.relative_to(ROOT)}  {p.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
