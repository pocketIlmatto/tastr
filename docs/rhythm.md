# Downbeats: design notes and learnings

How core picks which beat starts each bar (`_downbeat_phase` in
`tastr/engine/stages/core.py`), why, and what was tried and dropped. Written
2026-10-29.

## The problem

Core tracks beats, then picks one of 4 phases for bar 1 (it assumes 4/4).
The old picker chose the phase with the strongest kick plus onset. On
four-on-the-floor music every beat has the same kick, so it fell back to the
first tracked beat. That put bar 1 on the wrong beat on most dance tracks,
often half a bar late: the detected groove came out shifted by 8 steps.

## How it works now

Five cues each score the 4 phases. They are z-scored and summed:

| Cue | Measures | Alone on GTZAN |
|---|---|---|
| kick | low-band (30-150 Hz) onset strength on the beat | 0.46 |
| backbeat | mid-band (180 Hz-2.5 kHz) onsets on beats 2 and 4, minus 1 and 3 | 0.36 |
| change | timbre/chord change (60 Hz-5 kHz) from the previous beat | 0.49 |
| bass change | low (30-250 Hz) change from the previous beat | 0.50 |
| bar change | the next 4 beats vs the previous 4 | 0.43 |

All of them read one 128-band log-mel spectrogram of the full mix
(`signals.log_mel`), laid out like the Beat This! spectrograms so the
labeled sets below apply directly. Kick and backbeat can't tell bar 1 from
beat 3. The change cues can, because chords and bass usually move on the
downbeat.

Two guards:
- Change cues within a bar of near-silence are ignored. Going in or out of
  silence is a big change wherever it falls, and it threw off loops with a
  silent tail.
- When no phase wins by more than 1 (in summed z-scores), bar 1 starts on
  the first beat, as most full tracks start on a downbeat. On GTZAN this
  made no measurable difference (thresholds from 0 to 1.5 all scored
  0.645 to 0.655).

## Results

GTZAN, 933 tracks labeled 4/4, using the labeled beats so that only the
phase choice is scored (`python -m tastr.engine.bench.downbeats`):

| | Bar 1 right | Half a bar off |
|---|---|---|
| Old picker | 0.43 | 0.30 |
| Now | 0.64 | 0.17 |

By genre now: pop 0.76, hip-hop 0.72, country 0.70, rock 0.69, metal 0.68,
disco 0.65 (was 0.28), reggae 0.61, blues 0.58, classical 0.54, jazz 0.46.
Chance is 0.25.

## Known weaknesses

- **Perfectly repeating loops** (every bar identical, like the synthetic drum
  loops in `bench/synth.py`) have no bar-level information. A fill on beats 3
  and 4 can even pull bar 1 there. About half of those loops still land half
  a bar off. Real tracks change chords or bass at bar lines; loops don't.
- **Tempo errors come first.** When the tracker halves or misreads the tempo
  (synthetic breakbeats, 174 BPM read as 87), the phase can't be right.
  Tempo is a separate problem.
- **4/4 only.** Waltzes and other meters still get 4-beat bars. Core now
  guesses 3/4 (next section), but bars stay 4 beats until the rest of the
  app can draw 3-beat bars.
- GTZAN clips are 30-second excerpts. Nothing here measures full tracks from
  their start, where the first-beat prior helps most.

## Tried and dropped

- **Equal weights vs a grid search**: a 4,096-weight search scored 0.66 on its
  300 tuning tracks but 0.57 on the next 300. Equal weights scored 0.64 and
  0.62. Equal weights it is.
- **Onset strength as a cue**: on its own it picks the right phase only 15%
  of the time, worse than chance, because fills and pickups land before the
  downbeat. A negative weight didn't help overall either, so it was dropped.
- **Medians instead of means per phase**: robust to edge effects, but 0.60 on
  GTZAN.
- **Dropping the first and last bar**: fixed the silent-tail problem, but
  cost 0.02 on GTZAN. The near-silence mask fixes it without that cost.

## Meter: 3/4 or 4/4 (2026-10-30)

`_meter_score` in core guesses whether the beats group in 3s or 4s. It is
recorded as `card.extras["core.meter_guess"]` (`beats_per_bar`, `score`)
and changes nothing else yet. `_downbeat_phase(..., beats_per_bar=3)` picks
bar 1 among 3 phases, with "backbeat" meaning beats 2 and 3 against beat 1.
Both share the per-beat cues of the 4/4 picker (`_beat_cues`).

Six cues, each above 0 when the music repeats every 3 beats more than every 4:

| Cue | Measures | AUC on GTZAN alone |
|---|---|---|
| timbre lag | beat spectra 3 and 6 beats apart vs 4 and 8 apart (log ratio of distances) | 0.95 |
| bass lag | the same on 30-250 Hz | 0.94 |
| kick | spread of the mean kick strength over the 3 phases of a 3-beat cycle, minus over 4 | 0.93 |
| snare | the same for the snare band | 0.95 |
| change | the same for beat-to-beat timbre change | 0.87 |
| bass change | the same for bass change | 0.86 |

Each is divided by its spread over GTZAN (`METER_CUE_SCALES`) and summed
with equal weights; above `METER_THRESHOLD` (1.0) reads as 3/4. A
cross-validated logistic regression did no better (AUC 0.97 to 0.996 vs
0.97 to 0.994 for equal weights across two halves), so equal weights it is.

Labeled meters in the Beat This! annotations (7 MB zip, MIT): of tracks
with a steady 3 or 4 beats per bar, GTZAN has 49 in 3/4 and 925 in 4/4,
Ballroom 173 and 512, RWC 55 and 144, ASAP (piano) 94 and 218, Hainsworth
11 and 203, Beatles 6 and 148, Harmonix 5 and 859 (plus 14 in 6/8).

### Results on labeled beats

`python -m tastr.engine.bench.meter <folder> --dataset gtzan` (or
`tastr spec-bench meter`), which scores the guess and the 3-beat phase on
the labeled beats, so tempo errors don't count:

| GTZAN | |
|---|---|
| 3/4 tracks found | 0.86 (42 of 49; halves 0.82 and 0.91) |
| 4/4 tracks kept as 4/4 | 0.98 (18 of 925 flip, 9 of them classical) |
| 3/4 bar 1 right, meter given | 0.78 |

The threshold is not sensitive: 0.75 to 1.25 all find 0.86 and flip 1.6
to 2.5%. The cues and threshold were tuned on GTZAN, so this is not a
held-out number. Ballroom (173 waltzes, `ballroom.zip` 4.8 GB) is the
obvious held-out set.

### On core's own beats, tempo comes first

Run on core's tracked beats (the tempo adapter of `bench.tempo_spec`, all
49 GTZAN 3/4 tracks plus 300 4/4 ones), the guess is only as good as the
tempo:

- 4/4 tracks with the right tempo: 1 of 177 flips to 3/4.
- 4/4 tracks read at 3/2 tempo (the tracker locks onto dotted eighths or
  triplets): 20 of 28 flip to 3/4. Those beats really do group in 3s.
  Overall 37 of 300 (12%) 4/4 tracks flip, far too many to ship.
- 3/4 tracks: 29 of 49 found (59%); with the right tempo 21 of 26.

Raising the threshold doesn't separate them (at 3.0: 29% found, 6.7%
flip), and neither does tempo confidence (above 0.45: 35% found, 2.7%
flip). Checking whether a grid at 2/3 of the tracked tempo lands on
stronger onsets separated them only weakly (median onset ratio 0.99 for
3/4 vs 1.10 for 4/4 flips), so it was dropped. Fixing the 3/2 tempo error
is the next step before the guess can change bars.

**Update (2026-10-30):** most of those 3/2 errors came from core's own 1.5x
re-track for drum & bass, which fired on 4/4 tracks. It now skips the switch
when the faster beats group in 3s (`THREE_TWO_MAX_METER`, see bench.md
"Tempo notes"). Same 49 + 300 GTZAN tracks on tracked beats: 4/4 flipped to
3/4 11 of 300 (3.7%, was 12%), 3/4 found 33 of 49 (67%, was 59%).


The "Beat This!" release on Zenodo (record 13922116, CC BY 4.0) has
beat/downbeat labels for 16 public datasets, plus spectrograms in place of
the audio. `gtzan.zip` is 307 MB; Harmonix is 45 GB. Unzip
`beat_this_annotations.zip` and `<dataset>.zip` into one folder. Cloud
sessions can reach zenodo.org; the original audio hosts (mtg.upf.edu,
Hugging Face, GitHub archives) were refused there.
