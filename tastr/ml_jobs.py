"""Optional ML tags for the web server and CLI: "Add ML tags" runs MTG's Essentia classifiers
(engine/ml_tags.py, docs/essentia.md) on one stored track's audio in the background and stores the result in the
`ml_tags` table, which re-analysis leaves alone. With TASTR_ML_TAGS=auto every new upload gets them, and the
library's untagged tracks get them once. Off unless TASTR_ML_TAGS is on or auto, because MTG's weights are licensed
for non-commercial use only. A hosted server runs them on Modal (`ml_tags` in tastr/cloud/modal_web.py); a local
one needs Essentia installed (`pip install 'tastr[ml-tags]'`). Jobs run one at a time, like drum model runs
(drum_jobs.Jobs).
"""

from __future__ import annotations

import os
import sqlite3

from . import blobs, db

OFF_HINT = ("ML tags are off on this server: set TASTR_ML_TAGS=on to offer them (MTG's models are licensed for "
            "non-commercial use only, docs/essentia.md)")


def _on_modal() -> bool:
    return os.environ.get("TASTR_ANALYSIS_BACKEND", "local") == "modal"


def available() -> bool:
    """Whether this server offers ML tags: switched on, and on Modal (a hosted server) or with Essentia here."""
    from .engine import ml_tags

    if not ml_tags.switched_on():
        return False
    if _on_modal():
        from .cloud import remote

        return remote.available()
    return ml_tags.installed()


def automatic() -> bool:
    """Whether every new analysis gets ML tags (TASTR_ML_TAGS=auto) and they can run here."""
    from .engine import ml_tags

    return ml_tags.automatic() and available()


def untagged(conn: sqlite3.Connection) -> list[int]:
    """The tracks with no ML tags yet, oldest first."""
    return [r[0] for r in conn.execute("SELECT id FROM tracks WHERE id NOT IN (SELECT track_id FROM ml_tags) ORDER BY id")]


def why_not() -> str:
    from .engine import ml_tags

    return OFF_HINT if not ml_tags.switched_on() else ml_tags.INSTALL_HINT


def tag_track(conn: sqlite3.Connection, track_id: int) -> dict | None:
    """Run the classifiers on the track's audio and store the result; returns it, or None if there is no such
    track. Raises FileNotFoundError when its audio is gone."""
    card = db.load_card(conn, track_id)
    if card is None:
        return None
    path = card.source.local_path
    if not blobs.exists(path):
        raise FileNotFoundError(path or card.source.uri)
    if _on_modal():
        from .cloud import remote

        result = remote.ml_tags(path)
    else:
        from .engine import ml_tags

        result = ml_tags.analyze(blobs.ensure(path) or path)
    result.pop("seconds", None)
    db.save_ml_tags(conn, track_id, result)
    return result
