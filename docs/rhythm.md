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
- **4/4 only.** Waltzes and other meters still get 4-beat bars.
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

## Data

The "Beat This!" release on Zenodo (record 13922116, CC BY 4.0) has
beat/downbeat labels for 16 public datasets, plus spectrograms in place of
the audio. `gtzan.zip` is 307 MB; Harmonix is 45 GB. Unzip
`beat_this_annotations.zip` and `<dataset>.zip` into one folder. Cloud
sessions can reach zenodo.org; the original audio hosts (mtg.upf.edu,
Hugging Face, GitHub archives) were refused there.
