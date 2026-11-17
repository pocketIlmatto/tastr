"""The first-load states (static/app/library.js firstLoad): while the first /api/tracks is in flight the list shows
skeleton rows and a loading line instead of "No tracks yet.", Explore, Files and Admin each have one loading line,
and the rest of those views is hidden until the library is in. Checked in a browser for the PR; these keep the
wiring from coming apart."""

import re
from pathlib import Path

from ui_src import app_js

STATIC = Path(__file__).resolve().parents[1] / "tastr" / "web" / "static"


def test_each_waiting_view_has_one_loading_line():
    html = (STATIC / "index.html").read_text()
    for view in ("explore", "files", "admin"):
        section = re.search(rf'<section id="view-{view}" hidden>\s*(<p [^>]*>)', html)
        assert section and "data-boot-line" in section.group(1) and 'role="status"' in section.group(1), view
    css = (STATIC / "style.css").read_text()
    for view in ("explore", "files", "admin"):
        assert f"html[data-loading] #view-{view} > :not(.boot-line)" in css
    assert "html[data-cfg-wait] #view-admin > :not(.boot-line)" in css  # Admin also waits for /api/config


def test_the_page_starts_with_the_first_load_and_its_states():
    js = app_js()
    find = (STATIC / "app" / "find.js").read_text()
    assert "firstLoad();" in find and "configDone();" in find
    assert "Loading your library…" in js and "Waking up the server." in js and "Couldn't load your library." in js
    lib = (STATIC / "app" / "library.js").read_text()
    # the loading rows take the place of "No tracks yet.", which must not flash before the list arrives
    assert lib.index("if (libLoad)") < lib.index('"No tracks yet."')
