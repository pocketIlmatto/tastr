"""The web UI's JavaScript source, for tests that look for a feature in it.

app.js was split into ES modules under static/app/ (TAS-36); tests read them as one text.
"""

from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "tastr" / "web" / "static" / "app"


def app_js() -> str:
    return "\n".join(p.read_text() for p in sorted(APP_DIR.glob("*.js")))
