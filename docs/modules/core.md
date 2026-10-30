# Module: core features (BPM, key, energy, beat grid)

## Goal
Replace the stub in `tastr/engine/stages/core.py` with real estimators for tempo,
beats, downbeats, key, energy and the shared scalar features.

## Files
- Edit: `tastr/engine/stages/core.py` (keep class `CoreStage`, `name = "core"`, `requires = ()`).
- Append-only: `tastr/engine/signals.py` (shared intermediates), `tastr/engine/music.py` (theory helpers).
- New: `tests/test_core.py`.

## Inputs
`ctx.y` (mono float32), `ctx.sr` (22050), `ctx.hop` (512). Use
`ctx.get("hpss", signals.hpss)`, `ctx.get("onset_env", signals.onset_envelope)`,
`ctx.get("chroma", signals.chroma)`, `ctx.get("rms", signals.rms)`.

## Outputs (all on `ctx.card`)
| Field | Type | How (suggested) |
|---|---|---|
| `core.bpm` | float, 2 decimals | `librosa.beat.beat_track` on the percussive onset envelope, refined as 60 / median inter-beat interval |
| `core.bpm_confidence` | 0..1 | mean of tempo stability (1 - 4·std/mean of IBIs, clipped) and `pulse_clarity` |
| `core.bpm_likely_correct` | 0..1 or null | `calibration.likely_correct("bpm", bpm_confidence)`: share of benchmark tracks with that score within 4% of the label (docs/bench.md "Confidence calibration") |
| `core.beat_times` | list[float] s | beat tracker output; if fewer than 8 beats, synthesize a grid from the tempo |
| `core.downbeat_times` | list[float] s | every `beats_per_bar`-th beat from the phase `_downbeat_phase` picks (kick, backbeat, timbre/chord, bass and bar-change cues; docs/rhythm.md) |
| `core.beats_per_bar`, `core.meter_set` | 3 or 4, bool | the caller's `options["beats_per_bar"]` (meter_set true), else the detected guess below; `TASTR_METER=4/4` keeps every track at 4 |
| `extras["core.meter_guess"]` | `{beats_per_bar: 3 or 4, score}` | what was detected, even when the user set the meter: `_meter_score` > `METER_THRESHOLD` reads as 3/4 (docs/rhythm.md "Meter") |
| `core.key` | `Key` | `music.estimate_key(music.key_profile(chroma))`, the learned key model (`key_model.py`, see docs/bench.md) → tonic/mode; confidence = top key probability minus the runner-up's; `likely_correct = calibration.likely_correct("key", confidence)`; `scale = music.refine_mode(...)`; `camelot = music.camelot(tonic, mode)` |
| `core.loudness_db` | float | already done in the stub (RMS dBFS) |
| `core.energy` | 0..1 | 0.35·clip((loudness_db+30)/22) + 0.25·clip(onset_rate/6) + 0.20·brightness + 0.20·clip(percussiveness/0.5) |
| `series["energy"]` | `Series` | one point per bar: time = downbeat, value = min-max normalised (5th-95th percentile) mean RMS dB of the bar, 0..1 |
| `features[...]` | floats | every name in the feature registry marked "core" in `docs/ARCHITECTURE.md` |

`music.py` already has `estimate_key`, `refine_mode`, `camelot`, `note_name`.
The draft `analysis.py` in the reference drafts implements all of the above in
`analyze_signal()` and `_swing()`; port from it.

## Tests (`tests/test_core.py`)
Use `tests/synth.py`: `make_track(bpm=..., tonic=..., mode=...)`, run the
pipeline with `stages=["core"]`.

## Acceptance criteria
1. Synth tracks at 100, 124 and 140 BPM: `abs(core.bpm - truth) <= 1.0` (or within 1 of half/double).
2. Synth `tonic=9, mode="minor"` gives A minor or its relative C major; `tonic=7, mode="major"` gives G major or E minor.
3. `len(downbeat_times) * 4 <= len(beat_times) + 4`, both sorted ascending, all within `[0, duration]`.
4. `series["energy"]` has one value per downbeat, every value in [0, 1]; the synth's drum section (bars 9-24) has higher mean energy than the intro (bars 1-8).
5. All registry features are present, finite, and within their stated ranges.
6. 4 s of silence or white noise does not raise; it may leave `bpm`/`key` as None.
7. A 3-minute track analyzes in under 30 s on a laptop CPU (first call excluded; numba JIT warms up).
8. `python -m pytest -q` passes.
