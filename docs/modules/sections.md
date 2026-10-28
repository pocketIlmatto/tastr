# Module: section detection

## Goal
Replace the stub in `tastr/stages/sections.py` with rough, bar-snapped
section boundaries and labels (intro / verse / build / drop / chorus /
breakdown / outro).

## Files
- Edit: `tastr/stages/sections.py` (keep class `SectionsStage`, `name = "sections"`, `requires = ("core",)`).
- Append-only: `tastr/signals.py`.
- New: `tests/test_sections.py`.

## Inputs
Audio via `ctx`, and `ctx.card.core.downbeat_times` (bar starts, seconds).
If `downbeat_times` is empty, fall back to fixed 8-second blocks as "bars".

## Outputs
`ctx.card.sections`: list of `Section`, contiguous, covering `[0, duration]`:
- `start`, `end`: seconds; first `start` is 0.0, last `end` is `card.duration`, each `end` equals the next `start`.
- `start_bar`, `end_bar`: 1-based, inclusive, bar index into `downbeat_times`.
- `label`: one of `intro, verse, chorus, build, drop, breakdown, outro, full`.
- `letter`: "A", "B"... sections that sound alike share a letter.
- `energy`: mean per-bar energy of the section, 0..1 relative to the track.
- `confidence`: optional 0..1.

Also `features["section_contrast"]` (std of section energies) and `features["section_count"]`.

## Suggested method (from the draft `_sections()` in the reference drafts)
1. Per-bar feature vectors: chroma (12) + MFCC 2-13 + mean RMS dB, z-scored per dimension.
2. `librosa.segment.agglomerative(z, k)` with `k = clip(round(duration/28), 3, 12)`, capped at `n_bars // 4`.
3. Snap boundaries to multiples of 4 bars, drop sections shorter than 4 bars (merge into previous).
4. Letters: compare each section's mean vector to earlier letters' prototypes; reuse a letter when RMS distance per dim < 0.55.
5. Labels from relative energy: first & low (<0.5) → intro; last & low → outro; ≥0.68 → drop if percussive (features `percussiveness` > 0.25 and bpm > 105) else chorus; next section jumps ≥0.2 into ≥0.6 → build; <0.38 → breakdown; else verse.
   Per-bar energy: use `card.series["energy"]` if core provided it, else compute RMS per bar yourself.

## Tests (`tests/test_sections.py`)
Use `make_track(bpm=124, bars_per_section=(8, 16, 8))` and the `truth_core`
fixture so downbeats are exact.

## Acceptance criteria
1. Sections are contiguous, ordered, cover `[0, duration]`, and each is ≥ 4 bars (except a track shorter than 8 bars, which gets one `full` section).
2. On the synth track, there is a boundary within 1 bar (≈1.94 s) of 15.5 s and of 46.5 s (the drum section's start and end).
3. The synth's drum section (bars 9-24) gets a higher `energy` than intro and outro, and is labeled `drop` or `chorus`; the first section is `intro`.
4. With `downbeat_times = []` the stage still returns valid sections.
5. `python -m pytest -q` passes.
