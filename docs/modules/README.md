# Module specs

Each file here is a self-contained work order for one module. They are
written so each can be done in parallel by a separate session, including on
smaller models.

| Spec | Owns | Depends on (at runtime) | Suggested model / effort |
|---|---|---|---|
| [core.md](core.md) | `tastr/engine/stages/core.py`, `tastr/engine/signals.py` (append) | nothing | Sonnet, medium (DSP judgment) |
| [sections.md](sections.md) | `tastr/engine/stages/sections.py` | core's `downbeat_times` (fake it in tests) | Sonnet, medium |
| [tags-suggestions.md](tags-suggestions.md) | `tastr/engine/stages/tags.py`, `tastr/engine/stages/suggestions.py` | core, sections (fake in tests) | Sonnet, low-medium; tags part alone is fine for Haiku |
| [links.md](links.md) | `tastr/resolvers/` | nothing | Haiku or Sonnet, low |
| [ui.md](ui.md) | `tastr/web/` | the card + DB as they are now | Sonnet, medium |

## Rules for every module

1. **Only edit the files you own** plus new test files named `tests/test_<module>.py`.
   If you need a change elsewhere (a new card field, a shared helper), say so in
   your PR description instead of making it. Exception: appending a new function
   to `tastr/engine/signals.py` or `tastr/engine/music.py` is fine.
2. **Don't wait on other modules.** Write to the interfaces in `docs/ARCHITECTURE.md`.
   In tests, use `tests/synth.py` (`make_track()` gives audio with known BPM/key/sections)
   and the `truth_core` fixture (replaces the core stage with ground truth) or build a
   `TrackCard` by hand.
3. **Tolerate missing inputs.** Upstream fields can be `None`/empty (stub or failed
   stage). Never crash on that; produce less instead.
4. **Bump your stage's `version`** (e.g. `"0.1.0"`), and keep the stub's class name.
5. **Done means**: `python -m pytest -q` passes, your acceptance criteria pass, and
   `tastr analyze <some file>` still runs end to end.
6. Setup: `pip install -e '.[dev]'`. ffmpeg is needed for m4a/aac only.

## Reference drafts

Working (but un-integrated) prototype code for the analysis modules is in the
project files at `/mnt/project-files/drafts/feature-code/` (`analysis.py`,
`tags.py`, `suggestions.py`). It predates this skeleton and returns plain
dicts; port the pieces you need into your stage rather than importing it.
