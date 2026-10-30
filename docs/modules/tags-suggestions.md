# Module: vibe tags + "Try this in Ableton" suggestions

Two stages, one spec. Tags is pure rules over numbers (small model friendly);
suggestions also analyses audio for drum grids, bass notes and chords.

## Files
- Edit: `tastr/engine/stages/tags.py` (`TagsStage`, `requires = ("core", "sections")`).
- Edit: `tastr/engine/stages/suggestions.py` (`SuggestionsStage`, `requires = ("core", "sections", "tags")`).
- Append-only: `tastr/engine/music.py`, `tastr/engine/signals.py`.
- New: `tests/test_tags.py`, `tests/test_suggestions.py`.

## Part 1: tags
Input: `card.core` (bpm, key.mode, energy), `card.features` (registry in `docs/ARCHITECTURE.md`), `card.sections`.
Output: `card.tags`, list of `Tag(tag, score)`, score 0..1, sorted by score descending, only scores ≥ 0.2, no duplicate tags.

Tag vocabulary (exactly these strings) and rules, from the draft `tags.py`:

| Tag | Score formula (clip to 0..1; skip if ≤ 0) |
|---|---|
| `high energy` | (energy - 0.6) / 0.3 |
| `chill` | (0.45 - energy) / 0.25 if bpm < 115 else (0.35 - energy) / 0.25 |
| `dark` | (0.45 - brightness) / 0.3 + (0.25 if minor else -0.2) |
| `bright` | (brightness - 0.5) / 0.3 + (0.15 if major else -0.15) |
| `melancholic` | minor and energy < 0.5: 0.5 + (0.4 - energy) |
| `euphoric` | major only: (energy - 0.55) / 0.25 + (brightness - 0.45) |
| `driving` | 115 ≤ bpm ≤ 150: (pulse_clarity - 0.45) / 0.35 |
| `danceable` | (danceability - 0.55) / 0.3 |
| `hypnotic` | (pulse_clarity - 0.4) / 0.4 - 3·section_contrast + 0.3 |
| `bass-heavy` | (low_end - 0.25) / 0.25 |
| `percussive` | (percussiveness - 0.35) / 0.25 |
| `atmospheric` | (0.2 - percussiveness) / 0.15 + (0.4 - pulse_clarity) |
| `dynamic` | (section_contrast - 0.2) / 0.15 |
| `swung` | swing_confidence > 0.3: (swing_percent - 54) / 8 |
| tempo hints, 0.5 each | `downtempo` 70-100, `hip-hop tempo` 80-100, `house tempo` 118-130, `techno tempo` 125-145, `dubstep/halftime tempo` 138-150, `drum & bass tempo` 160-180 (inclusive) |

A rule whose inputs are missing (None / absent feature) is skipped, not treated as 0.

Acceptance (tags), with hand-built cards (no audio needed):
1. energy 0.9, brightness 0.7, major, bpm 126 → includes `high energy`, `bright`, `euphoric`, `house tempo`, `techno tempo`; excludes `chill`, `dark`.
2. energy 0.2, brightness 0.2, minor, bpm 85 → includes `chill`, `dark`, `melancholic`, `downtempo`; excludes `high energy`.
3. A card with only `core.bpm` set (everything else None/empty) → no exception; only tempo tags.

## Part 2: suggestions
Input: everything above plus audio. Output: `card.suggestions`, list of
`Suggestion(category, title, body, data)`, `category` in
`setup | drums | bass | harmony | arrangement | sound`. `title` ≤ 80 chars and
says the idea; `body` is 1-3 sentences naming concrete Live devices/settings.
Also store analysis by-products in `card.extras`:
`"suggestions.drum_grid"`, `"suggestions.bass"`, `"suggestions.harmony"` (shapes below).

### Analysis it needs (port from the draft `analysis.py`)
- **16-step grid**: for each bar (consecutive downbeats), 16 step times by splitting each beat into 4.
- **Drum grid** (`_drum_grid`, lane detection in `tastr/engine/drums.py`): semi-adaptive NMF splits the percussive mel spectrogram into low, mid and high parts; leakage between parts is measured per track and subtracted; each hit is then sorted into a lane (kick or tom by pitch; snare, clap or perc by bursts, body and pitchedness; closed or open hat by how long the hat band rings). Lanes: `kick, snare, clap, hat, open_hat, tom, perc`. Activations sampled at steps (max within ±2 frames), averaged over bars with energy ≥ median (bar energy: `card.series["energy"]` if present, else mean RMS per bar computed here). Pattern string per lane, 16 chars: `x` ≥ 0.72 of lane max, `o` ≥ 0.5, `.` otherwise. `extras["suggestions.drum_grid"] = {lane: {"pattern": "x...x...x...x...", "velocity": [16 floats], "detected": bool}}` for every lane; `detected` = the lane's peak is at least 3x its level halfway between 16ths, and (for lanes other than kick, snare, hat) at least 0.2 of the loudest lane's peak; a mid lane that is a faint copy of the kick is not detected. Scores: `python -m tastr.engine.bench.drums` (docs/bench.md).
- **Bass** (`_bass_summary`): CQT of the harmonic part from C1, 36 bins; per bar the dominant pitch class. `extras["suggestions.bass"] = {"notes": [per bar, bars with no CQT energy left out], "bar_notes": [per bar, aligned to bars, null where the bar has no bass], "loop": [most common 4-bar window], "degrees": [...], "movement": "pedal|stepwise|fourths/fifths|leaps|static", "rhythm": 16-char pattern}`. A bar has no bass when its power in the two lowest octaves (C1-B2) is under `NO_BASS` (0.25, -6 dB) of the track's 90th-percentile bar; not lower, because a kick with no bass under it leaks about -8 dB there.
- **Chords** (`_harmony_summary`): per-bar mean chroma matched with `music.match_chord`; `extras["suggestions.harmony"] = {"progression": [4 names], "roman": [...], "chords": [{"name","bars","roman","scale"}], "bar_chords": [per bar, aligned to bars, null where the match score is under 0.6]}` using `music.roman_numeral` and `music.chord_scale`.

### Per-section data (`by_section`)
When the card has two or more sections with bar numbers, three suggestions carry a `data.by_section` list with one entry per such section, in order. Every entry has `idx` (the section's index in `card.sections`), `label`, `letter`, `start_bar`, `end_bar` (1-based, inclusive). The UI ("Drums, bass & chords" section, which shows the template pattern ideas in its drum Pattern picker) and the Strudel generator read these; keep the shape stable.
- "Detected groove" (drums): plus `energy` and `pattern` ({lane: 16-char}, detected lanes only).
- "Chord loop guess" (harmony): plus `progression` (the most common 4-bar window of the section's `bar_chords`, or its first bars if the section is shorter; `null` = unconfident bar; `[]` when the section has no confident chord) and `roman` (same length, `null` where the chord is `null`).
- "Bass loop" (bass): plus `loop` (same rule over `bar_notes`; `[]` when the section has no bass, i.e. its mean low-octave power is under `NO_BASS` of the track's), `degrees` (scale degrees like the global `degrees`, `null` for `null` notes) and `rhythm` (16-char pattern from the bass onset flux over the section's bars with bass, or `null` when it has no bass).
The global fields (`pattern`, `progression`, `loop`, `rhythm`) stay as before. Cards analyzed before stage version 0.2.0 have no harmony/bass `by_section`; Re-analyze adds it.

### Suggestions to emit (port `ableton_suggestions()` from the draft `suggestions.py`)
- setup: tempo + Scale (root, scale) + Camelot + warp mode (Beats if percussiveness > 0.3 else Complex Pro).
- drums: swing (Groove Pool) if swing_confidence > 0.3 and swing_percent ≥ 54; "detected groove" with the detected lanes; 1-2 pattern ideas for the tempo style (four-on-floor 118-136, dnb 160-185, halftime 136-152, hip-hop 80-105, else backbeat). `data = {"pattern": {lane: 16-char}, "pads": {"kick": "C1", "snare": "D1", "clap": "D#1", "hat": "F#1", "open_hat": "A#1", "tom": "A1", "perc": "G#1"}, "lane_help": {lane: what it measures}}`.
- bass: loop + movement tip; sidechain tip with release from the 8th-note length in ms.
- harmony: chord loop guess; per chord "Over X play Y"; safe pentatonic lead scale.
- arrangement: locator list from sections, `data = {"locators": [{"bar", "name"}]}`; build tip if a build exists.
- sound: dark (brightness < 0.35) or bright (> 0.6) mix tip.

Acceptance (suggestions):
1. With `truth_core` on the 124 BPM synth: the detected kick lane has `x` on steps 1, 5, 9, 13 (indices 0, 4, 8, 12); the hat lane hits indices 2, 6, 10, 14.
2. Synth A minor (i-VI-III-VII = Am F C G): progression contains at least 3 of those 4 chords; bass loop notes are a subset of {A, F, C, G}.
3. Every suggestion has a valid category, non-empty title/body, JSON-serialisable `data`; every 16-step pattern is exactly 16 chars of `x o .`.
4. With core empty (bpm None, key None, no downbeats) the stage returns a list (possibly empty) without raising.
5. `python -m pytest -q` passes.
