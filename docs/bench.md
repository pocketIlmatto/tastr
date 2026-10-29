# Accuracy benchmark

`tastr bench` runs the engine over tracks with known labels and scores how
close it gets. Run it before and after any change to the analysis stages, so
tuning is guided by numbers rather than a few hand-picked tracks.

```
tastr bench synth                                   # smoke test, no downloads
tastr bench giantsteps-key ~/data/giantsteps-key --jobs 4 --out key-baseline.json
tastr bench giantsteps-key ~/data/giantsteps-key --jobs 4 --baseline key-baseline.json
tastr bench manifest ~/music/labels.csv             # your own tracks
```

By default only the `core` and `sections` stages run (`--stages` to change).
`--limit N` takes the first N tracks for a quick check.

## What gets scored

| Metric | Meaning |
|---|---|
| BPM within 4% (Acc1) | estimate within 4% of the labeled tempo |
| BPM incl. x2 / half (Acc2) | also counts double, half, triple and third; the gap between Acc1 and Acc2 is octave errors |
| Key (MIREX weighted) | 1 exact, 0.5 off by a fifth, 0.3 relative major/minor, 0.2 parallel, 0 otherwise |
| Key exactly right | share of tracks with tonic and mode both right |
| Beat / downbeat F | beats within 70 ms, ignoring the first 5 s |
| Sections F (0.5 s / 3 s) | section starts within 0.5 s or 3 s of a labeled one, track start and end excluded |

Each metric is averaged over the tracks that have that label. The summary also
counts key results by category (so "relative 140" shows the minor/major
confusion directly) and BPM octave errors by factor.

The report's `review` list is the tracks most worth a human look: confident
wrong answers first (they usually point at a systematic bug), then right
answers the engine was unsure of. Fixing a label or confirming one in that list
and adding it to your manifest is the cheapest way to grow the labeled set.

## Datasets

Nothing is downloaded by `tastr bench`; fetch each dataset with its own scripts
and point the command at the folder. Check each dataset's license before
redistributing its labels or audio.

| Name | Labels | Size | Where |
|---|---|---|---|
| `giantsteps-key` | global key | 604 two-minute EDM previews | github.com/GiantSteps/giantsteps-key-dataset for the labels; see "Getting the GiantSteps audio" |
| `giantsteps-tempo` | global BPM | 664 EDM previews | github.com/GiantSteps/giantsteps-tempo-dataset (uses `annotations_v2` when present) |
| `harmonix` | BPM, beats, downbeats, sections | 912 pop/EDM tracks | github.com/urinieto/harmonixset; audio is not distributed, put your copies in `<root>/audio` named by track id |
| `manifest` | whatever you fill in | your tracks | a CSV or JSON file, below |
| `synth` | everything, exact | 12 generated tracks | built in; a smoke test, not a benchmark |

GiantSteps is the closest match to dance music libraries and is the main key
and tempo benchmark. Harmonix is the one with sections and downbeats.

### Getting the GiantSteps audio

The datasets' `audio_dl.sh` scripts no longer work: the JKU mirror is gone from most networks and
Beatport's backup returns 404. The key dataset's audio is also published on Zenodo as part of
GiantSteps+ (record 4153506, `audio.zip`, 600 of the 604 tracks, named `<id> <artist> - <title>.mp3`).
The loaders want `<id>.LOFI.mp3`, so stage a folder with the labels from the dataset repo and one
symlink per track:

```
<root>/annotations/key/<id>.LOFI.key      # from the GiantSteps repo
<root>/audio/<id>.LOFI.mp3                # symlink to the Zenodo file for that id
```

That audio covers only 43 of the 664 tempo tracks, so tempo scores from it are a small sample.
Keep the data outside the repo and never commit audio.

### Your own tracks

A CSV (or a JSON list of objects) with these columns, all optional except `path`:

```
path,bpm,key,boundaries,labels
Night Shift (Extended Mix).mp3,124,A minor,"0:00 1:01 2:02 3:34",intro build drop breakdown
```

`path` is relative to the manifest file. `key` accepts "A minor", "Am",
"F# maj" or Camelot codes like "8A". `boundaries` are section start times in
seconds or m:ss.

## Key detection notes

Measured on GiantSteps Key (599 tracks, labels from the original dataset):

- Most key errors are not relative major/minor swaps. Before the minor prior, 12% of tracks were
  parallel-mode errors (right tonic, wrong mode), 11% were a fifth off and only 4.5% were relative swaps.
- The mean of per-frame normalized chroma scores better than the plain mean, and adding a small bonus
  (`music.MINOR_PRIOR`) to minor keys scores much better than either, because 85% of the labels are minor.
  The prior is flat from about 0.08 to 0.25, so its exact value is not delicate. If you analyze mostly
  major-key music, lower it.
- Tried and dropped (no gain on GiantSteps): bass-weighted chroma, band-split chroma with learned profiles,
  tuning-corrected 36-bin CQT chroma, 10 s windowed voting, Temperley profiles, learned per-mode profiles.
- Fifth errors (about 13% of tracks) did not respond to cheap changes. Checked on two disjoint 200-track
  subsets (every third track, offsets 0 and 1) so a gain had to hold on tracks it was not tuned on:
  Shaath/KeyFinder EDM profiles (+0.02 MIREX on one subset, -0.015 on the other, so noise), compressing the
  chroma (power 0.35 to 0.7), a bonus for tonic energy or for tonic+fifth+third energy, and a minor prior above
  0.10 (which just fits GiantSteps' 85% minor labels). Subset scores for the current method range from 0.605
  to 0.655, so differences under about 0.03 on 200 tracks are not evidence.
- Overtone-suppressed chroma (subtract the lower-octave and 3rd-harmonic contributions from a tuned 96-bin CQT
  of the harmonic part, weights 0.3 to 0.6) did not help on the 200-track subset: fifths 28 -> 25 to 30, MIREX
  0.581 -> 0.568 to 0.581 against the same CQT without suppression.
- Iterating quickly: extract the chroma once per track (`signals.chroma`) to `.npz`, then score `estimate_key`
  variants offline in seconds; run the full bench only for a candidate that wins on both subsets.
- Environment: librosa 1.0 with numba 0.68 and numpy 2.5 segfaults in `onset_detect`. These worked:
  numpy<2.3, numba 0.61.2, llvmlite 0.44.0, librosa 0.11.0, scipy<1.16.

## Tempo notes

Measured on the 43 GiantSteps tempo tracks that have audio in the Zenodo mirror (`annotations_v2`),
`--stages core`. With 43 tracks one track is 2.3 points, so only large effects are evidence.

- Baseline: BPM within 4% 0.488, incl. x2/half 0.605. Tracks at 165 BPM or faster (drum & bass) were right
  on 1 of 16, usually reported at 2/3 tempo (117 for 174) because the beat tracker prefers tempos near 120.
- Fix: when the onset tempogram is at least `core.THREE_TWO_RATIO` (0.9) as strong at 1.5x the tracked tempo,
  re-track at 1.5x. Straight 4/4 tracks have little periodicity at 1.5x their beat (ratio mostly 0.3 to 0.8),
  2/3-tempo drum & bass mostly 0.9 to 1.1. After: 0.698 / 0.861; fast tracks 9 of 16, slower tracks 21 of 27
  (two gained, one 121 BPM track lost to 161). 0.85 to 0.9 scored best; the threshold was picked on these
  same 43 tracks, so recheck it when more tempo-labeled audio is available.
- Still open: drum & bass reported at half tempo (86 for 174) and half-time tracks labeled ~86 reported at 172.
  The tempogram does not separate these, and the labels themselves are a judgment call.

## Drum lanes

The drum lanes behind the detected groove (kick, snare, clap, hat, open hat,
tom, perc; see `tastr/engine/drums.py`, and [drums.md](drums.md) for the design,
what was tried and dropped, and known weaknesses) have their own benchmark:

```
python -m tastr.engine.bench.drums synth                                  # generated loops, every lane
python -m tastr.engine.bench.drums idmt ~/data/IDMT-SMT-DRUMS-V2 --limit 20 # quick check while tuning
python -m tastr.engine.bench.drums idmt ~/data/IDMT-SMT-DRUMS-V2 --out drums.json
```

`idmt` is [IDMT-SMT-Drums](https://zenodo.org/records/7544164) (95 loops from
real, sampled and techno kits, CC BY-NC-ND): kick, snare and hi-hat onsets,
scored within 50 ms. Snare counts snare or clap hits, hat counts closed or open.
It has no toms or percussion, so it also reports how often those lanes fire
anyway. `synth` scores every lane step by step on generated loops, which is
exact but uses one synthetic kit. Iterate with `--limit`, run the full set
before keeping a change.

Scores on 2026-10-29 (onset F-measure; before is the old three-band split):

| | Kick | Snare | Hat |
|---|---|---|---|
| IDMT, before | 0.74 | 0.48 | 0.89 |
| IDMT, now | 0.96 | 0.94 | 0.90 |

Synth, now: kick 1.00, clap 1.00, hat 0.91, snare 0.75, open hat 0.69, tom
0.67, perc 0.64. Toms and percussion are found when they play alone but missed
when they land with a kick or snare; IDMT kits fire them on about 3 in 100 hits.

## Downbeats

`python -m tastr.engine.bench.downbeats <folder> --dataset gtzan [--limit N]`
scores which beat core picks as bar 1, on labeled beats, from the Beat This!
spectrogram release. See [rhythm.md](rhythm.md) for the data, scores and
design notes.
