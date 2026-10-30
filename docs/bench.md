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
`--limit N` takes the first N tracks for a quick check. `--remote` runs it on Modal
with the datasets in an R2 bucket instead of on this machine (see [cloud.md](cloud.md)).

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
| `beatport-key` | global key | 1,486 two-minute EDM previews (1,275 with a plain major/minor label are scored) | Zenodo record 1101082: unzip `keys.zip` and `audio.zip` into one folder; no overlap with GiantSteps Key |
| `beatport-tempo` | Beatport's listed BPM | 774 of those previews with a bpm in `meta/` | same folder plus `original_metadata.zip` unzipped to `<root>/meta/*.json`; see "Tempo notes" for its drum & bass conventions |
| `harmonix` | BPM, beats, downbeats, sections | 912 pop/EDM tracks | github.com/urinieto/harmonixset; audio is not distributed, put your copies in `<root>/audio` named by track id |
| `manifest` | whatever you fill in | your tracks | a CSV or JSON file, below |
| `synth` | everything, exact | 12 generated tracks | built in; a smoke test, not a benchmark |

GiantSteps is the closest match to dance music libraries and is the main key
and tempo benchmark. Harmonix is the one with sections and downbeats.

Other public datasets worth adding, ranked, are in [datasets.md](datasets.md).

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

**Method.** `music.estimate_key` is a learned model: a 24-class logistic regression (one class per key,
index tonic * 2 + 1 for minor) on the 12-bin key profile, the mean of per-frame normalized chroma
(`music.key_profile` of `signals.chroma`). Its 24 x 12 weights and 24 biases live in
`tastr/engine/key_model.py`, generated by `tastr.engine.bench.train_key`; the engine applies them with
numpy (softmax of W x + b), so scikit-learn is needed only to retrain. Every training track is used at
all 12 transpositions, so the model learns what a key sounds like relative to its tonic rather than which
keys a dataset contains, and its predictions transpose with the input. Confidence is the top key's
probability minus the runner-up's (median 0.29); on the training data, accuracy by confidence quartile goes
from 32% to 75%.

**Training data.** GiantSteps Key (all 599 tracks with audio), Beatport EDM Key (the 1,122 tracks whose
annotators gave the label confidence 2; confidence 0 and 1 labels are left out) and 16 synthetic progression
profiles (I-V-vi-IV, I-IV-V-I, i-VI-III-VII and i-iv-v-i from `bench/synth.py`, tonics C and A, at 118 and 128
BPM, so not the tests' 124 BPM settings), each weighted 5 times a real track. C = 1.

**Held-out results.** Each dataset split into alternating halves A/B (loader order); train on both
datasets' A halves (plus the synthetic profiles) and score the B halves, and the reverse (`train_key --holdout`):

| | Krumhansl-Kessler + minor prior (before) | learned model |
|---|---|---|
| GiantSteps Key, 599 | 0.650 (A 0.637, B 0.664), exact 56.1% | 0.682 (A 0.675, B 0.689), exact 60.4% |
| Beatport EDM Key, confidence-2 labels, 1,122 | 0.562 (A 0.581, B 0.542), exact 46.7% | 0.606 (A 0.616, B 0.596), exact 51.5% |

It wins on all four halves, by 0.025 to 0.054 MIREX. Most of the gain is fewer "other" and parallel-mode
errors. Without the synthetic profiles it scored a little higher on real tracks (GiantSteps 0.684, Beatport
0.610, smallest gain +0.032) but misread 22 of 48 synthetic tracks; see the known weakness below. Training on
one dataset alone transfers less (measured without synthetic profiles): Beatport -> GiantSteps scores 0.658
(Krumhansl 0.650), GiantSteps -> Beatport 0.600 (0.562). That's why it is trained on both. Within GiantSteps
alone, 2-fold cross-fitting scores 0.673.

**Known weakness.** Major keys are sometimes read as their IV key (a fifth below) on root-heavy triad
progressions. The test synth's bass root is about 3 times louder than each chord tone, so its I-V-vi-IV in G
major has mostly the roots G, D, E and C in its chroma, which fit C major as well as G major; Krumhansl-Kessler
broke that tie toward G, the learned model breaks it toward C. 6 of 48 synthetic tracks (all 24 keys, 4/8/4
and 2/4/2-bar sections at 124 BPM) are read this way, all major keys a fifth off, and `test_core.test_key` for
G major is marked as an expected failure. We have no labeled pop set to measure how often real songs with
these progressions are affected. Things tried to fix it, scored on the held-out halves plus the synthetic suite
(smallest gain over Krumhansl on any half / synthetic failures): blending in the Krumhansl score,
log p + lambda * (correlation + 0.10 minor prior), at lambda 0.5, 1, 2, 4, 8, 16: +0.031/20, +0.027/19,
+0.024/17, +0.018/13, +0.021/9, +0.009/4; synthetic profiles weighted x1, x5, x20, x50: +0.032/17,
+0.025/6 (shipped), +0.019/1, -0.002/0; x5 with lambda 2 or 4: +0.023/3, +0.017/2; x20 with lambda 4: +0.001/0.
No variant read every synthetic track right and kept a +0.025 gain on every half.

**Full bench (in-sample: every track was in the training set, so these are not held-out numbers),
`--stages core`, 2026-10-29:** GiantSteps Key 599 tracks, MIREX 0.676, exact 59.9% (fifth 67, relative 23,
parallel 28, other 122), 408 s with `--jobs 8`. Beatport EDM Key, all 1,275 scored tracks, MIREX 0.586, exact 49.1%
(fifth 142, relative 73, parallel 143, other 291), 866 s; on its confidence-2 labels alone 0.612, exact 52.0%.
Krumhansl-Kessler scored 0.650 and 0.537 on the same sets. The GiantSteps in-sample score is a little below
its held-out one (0.682), because the strongly regularized fit on all tracks is dominated by Beatport's 1,122.

**Retraining.** Cache features once per dataset (about 5 s per track per worker), then fit in seconds:

```
python -m tastr.engine.bench.key_features giantsteps-key ~/data/giantsteps-key ~/data/features/giantsteps-key --jobs 8
python -m tastr.engine.bench.key_features beatport-key ~/data/beatport-key ~/data/features/beatport-key --jobs 8
python -m tastr.engine.bench.train_key \
    --data giantsteps-key ~/data/giantsteps-key ~/data/features/giantsteps-key \
    --data beatport-key ~/data/beatport-key ~/data/features/beatport-key --holdout   # check first
python -m tastr.engine.bench.train_key --data ... --data ... --write                 # writes key_model.py
```

`train_key` renders the synthetic progressions itself (about a minute; `--no-synth` leaves them out). The
cached `.npz` files also hold the 36-bin bass CQT and the label confidence, so other estimators can be
scored offline the same way. Bump the core stage version when the model changes.

**Tried and dropped on Beatport EDM Key** (confidence-2 labels, halves A/B, against Krumhansl-Kessler at 0.562):
Temperley (0.547) and Temperley 2005 (0.495) profiles trade parallel errors for many more fifth errors.
Shaath (0.580), Faraldo's EDMA (0.580) and bgate (0.582) profiles from essentia gained about 0.02 on both
halves, under the noise level, and only edma helped on GiantSteps (0.662; bgate 0.646). Mixing a bass chroma
(the 36-bin `signals.bass_cqt` folded to 12 pitch classes) into the profile at 0.25 or 0.5 made things worse
(0.557, 0.547). Letting the strongest bass pitch class pick the tonic was much worse (0.473), so in these
tracks it is often not the tonic. Adding the bass profile to the learned model's inputs changed nothing beyond
noise. The minor prior (bonus for minor keys in the Krumhansl correlation, 0.10) was flat from 0.08 to 0.15 on
Beatport (0.535 to 0.544) even though only 70% of its labels are minor, against 85% on GiantSteps.

**Before the learned model** (Krumhansl-Kessler correlation, measured on GiantSteps): most errors were not
relative swaps but parallel-mode (12% of tracks before the minor prior) and fifth errors (11 to 13%).
Per-frame normalized chroma beat the plain mean, and the model still uses it. Tried and dropped there:
band-split chroma with learned profiles, tuning-corrected 36-bin CQT chroma, 10 s windowed voting,
learned per-mode profiles, compressing the chroma (power 0.35 to 0.7), tonic or tonic+fifth+third energy
bonuses, and overtone-suppressed chroma. On 200-track subsets, differences under about 0.03 MIREX were noise.

- Environment: librosa 1.0 with numba 0.68 and numpy 2.5 segfaults in `onset_detect`. These worked:
  numpy<2.3, numba 0.61.2, llvmlite 0.44.0, librosa 0.11.0, scipy<1.16.
  A segfault in `beat_track` with matching versions came from a numba cache corrupted when parallel workers
  compiled into it at once. `--jobs` now analyzes one track before starting workers; if a cache is already
  broken, delete the `.nbi`/`.nbc` files under librosa's `__pycache__` or point `NUMBA_CACHE_DIR` at a fresh folder.

## Confidence calibration

The raw `bpm_confidence` and key `confidence` say how clear the evidence was in one track, not how often
the answer is right: a track with a steady, pronounced beat can score 0.94 and still be read at 2/3 tempo.
`calibration.likely_correct` maps each raw score to the share of benchmark tracks with that score that were
right (BPM: Acc1, within 4% of the label; key: the exact key), and the card stores it as
`core.bpm_likely_correct` and `key.likely_correct`. The UI shows it as "N% likely right"; cards analyzed
before calibration keep the raw "N% conf" until re-analyzed.

The map is an isotonic (non-decreasing) fit, pooled into blocks of at least 40 tracks, with each block's rate
nudged towards 1/2 by half a track. Tables live in `tastr/engine/calibration_table.py`, written by
`tastr.engine.bench.calibrate`. Key rows must be out-of-fold: the key model was trained on GiantSteps and
Beatport, so a plain bench report on those sets is in-sample and overconfident. `train_key --holdout
--calibration-rows` saves the alternating-half held-out confidence and result for every track.

```
python -m tastr.engine.bench.train_key --data giantsteps-key GS GS-FEATURES --data beatport-key BP BP-FEATURES \
    --holdout --calibration-rows results/key-oof.json
tastr bench beatport-tempo BP --stages core --out results/bp-tempo-core.json
tastr bench giantsteps-tempo GS-TEMPO --stages core --out results/gs-tempo-core.json
python -m tastr.engine.bench.tempo_spec BEAT-THIS --dataset hjdb --out results/hjdb-tempo.json
python -m tastr.engine.bench.calibrate --bpm results/bp-tempo-core.json results/gs-tempo-core.json \
    results/hjdb-tempo.json --key results/key-oof.json          # held-out check and reliability table
python -m tastr.engine.bench.calibrate ... --write             # then write calibration_table.py
```

**Fitted 2026-10-30.** BPM from 1,052 tracks, `--stages core`: Beatport tempo 774 (Acc1 0.872), GiantSteps
tempo 43 (0.721), hjdb 235 (0.919); 87.6% right overall. Key from 1,721 out-of-fold rows: GiantSteps 599,
Beatport 1,122; 54.6% exact overall. Held-out Brier (fit on one alternating half, scored on the other;
lower is better), raw score / flat overall rate / calibrated: BPM 0.284 / 0.109 / 0.095, key 0.280 / 0.249 /
0.224. Reliability, five equal-count bins of the raw score:

| BPM raw | Tracks | Predicted | Actual | | Key raw | Tracks | Predicted | Actual |
|---|---|---|---|---|---|---|---|---|
| 0.32-0.43 | 206 | 63% | 61% | | 0.00-0.09 | 339 | 33% | 34% |
| 0.43-0.44 | 207 | 92% | 93% | | 0.09-0.21 | 348 | 41% | 40% |
| 0.44-0.45 | 203 | 94% | 95% | | 0.21-0.36 | 346 | 57% | 57% |
| 0.45-0.46 | 212 | 95% | 94% | | 0.36-0.56 | 343 | 66% | 68% |
| 0.46-0.98 | 224 | 95% | 94% | | 0.56-0.97 | 345 | 76% | 74% |

The raw BPM score is bunched in 0.41-0.47 on all three sets, so it mostly separates the bottom fifth
(about 60% right) from the rest (about 94%); it says little among confident tracks.

Refit after changing the tempo or key logic (a new `THREE_TWO_RATIO`, a retrained key model). The
benchmarks are EDM-heavy (Beatport, GiantSteps) plus jungle / hardcore (hjdb), so the figures describe
those genres best.

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
- Retuned on more data (2026-10-30): `THREE_TWO_RATIO` 0.9 -> 0.8, picked on the 200 hjdb train tracks
  (0.75 to 0.85 score alike) and checked on the rest. Acc1 / Acc2, `--stages core` for audio:

  | Set | Tracks | Before (0.9) | After (0.8) |
  |---|---|---|---|
  | hjdb train (spectrograms, `bench.tempo_spec`) | 200 | 0.755 / 0.785 | 0.910 / 0.940 |
  | hjdb val | 35 | 0.829 / 0.829 | 0.971 / 0.971 |
  | `beatport-tempo` | 774 | 0.872 / 0.899 | 0.872 / 0.906 |
  | `giantsteps-tempo` | 43 | 0.698 / 0.861 | 0.721 / 0.884 |

  hjdb errors (est / ref) went from 2/3 x35, 4/5 x7, half x6 to 4/5 x7, half x6, 2/3 x3 on train. Beatport
  is unchanged in total but swaps 11 tracks each way: 110-120 BPM tracks pushed to 161-172 are lost, house and
  techno tracked at 83-92 are fixed (re-tracked at 1.5x to 123-144).
- hjdb (jungle / hardcore, in the Beat This! release) is not a 174 set: median 154 BPM, range 120-171, 35
  tracks below 140, 160 at 140-160, 37 at 160-170, 3 at 170 or more. Its main error was 2/3 tempo, not half.
  `bench.tempo_spec` rebuilds core's onset envelopes from the spectrograms and resamples them to core's
  43 fps (the tempo logic is not frame-rate invariant: the unchanged audio path at 50 fps flips 4 of the
  43 GiantSteps calls). On GiantSteps audio the adapter gives the audio path's BPM on 39 of 43 tracks, with
  the same Acc1; it is a proxy, not the audio path.
- Beatport's bpm is the DJ tempo and its drum & bass labels follow no one convention: of 40 tracks, 22 are
  listed at 84-92 (half time), 8 at 111-119 (about 2/3 of 174), 8 at 160-175, 2 elsewhere. So
  `beatport-tempo` Acc1 on drum & bass mostly measures the labeling (19 of 40 exact, 26 counting double / half).
  Dubstep is listed at 140, some hip-hop at 150-195 (double time).
- Tried and dropped (Acc1 counts as hjdb train / Beatport / GiantSteps, from 151 / 675 / 30 at 0.9):
  - Onsets from the full mix instead of the HPSS percussive part: 50:50 mix at 0.8 gives 186 / 668 / 34, full
    mix 184 / 659 / 34. Helps the fast sets, costs Beatport 7 to 16 tracks.
  - A lower ratio only below 115 BPM (0.75, keeping 0.9 for 115-120): 185 / 681 / 31. Better on Beatport, but
    the split was found by looking at Beatport's losses, so it is not a held-out result.
  - Moving the 1.5x ceiling (re-track only when 1.5x the tempo is below `MAX_BPM` 180): 170-175 loses 6-7
    GiantSteps tracks, 185-200 loses 6-17 Beatport tracks.
  - Doubling when 2x lands in 150-185, gated on tempogram strength at 2x (ratio 0.6-1.0) or on off-beat
    onset strength: fixes at most 5 half-tempo tracks on hjdb train and 4 on GiantSteps, costs Beatport 7-24.
    The losses are real 85-92 BPM hip-hop and downtempo as well as half-time drum & bass labels.
  - Re-tracking at 1.25x for the 4/5 locks (tracked 117-129, labeled 146-165): the 1.25x strength on those
    (0.78-1.01) overlaps correctly tracked 128 BPM tracks (0.55-1.01). Every rule did nothing or cost
    Beatport 5 to 48 tracks.
- Still open: drum & bass reported at half tempo (86 for 174) and half-time tracks labeled ~86 reported at 172.
  The tempogram and off-beat onsets do not separate them, and Beatport's labels show the convention itself
  varies, so a fix needs a stronger cue (e.g. snare placement within the bar) and labels that agree.

## Drum lanes

The drum lanes behind the detected groove (kick, snare, clap, hat, open hat,
tom, perc; see `tastr/engine/drums.py`, and [drums.md](drums.md) for the design,
what was tried and dropped, and known weaknesses) have their own benchmark:

```
tastr drum-bench synth                                          # generated loops, every lane
tastr drum-bench idmt ~/Development/tastr-data/idmt-smt-drums --limit 20   # quick check while tuning
tastr drum-bench mdb ~/Development/tastr-data/mdb-drums --out mdb.json
tastr drum-bench stemgmd-mix ~/Development/tastr-data/stemgmd-single-hits
tastr drum-bench mdb mdb-drums --remote                         # on Modal (docs/cloud.md)
```

`python -m tastr.engine.bench.drums` takes the same arguments.

- `synth` scores every lane step by step on generated loops: exact, but one synthetic kit.
- `idmt` is [IDMT-SMT-Drums](https://zenodo.org/records/7544164) (95 loops from real, sampled and
  techno kits, CC BY-NC-ND): kick, snare and hi-hat only. Snare counts snare or clap hits, hat counts
  closed or open. It also reports how often the lanes it has no labels for (tom, perc, clap) fire.
- `mdb` is [MDB Drums](https://github.com/CarlSouthall/MDBDrums) (23 MedleyDB songs, real full mixes,
  CC BY-NC-SA), from its subclass labels: kick; snare (incl. brushes, ghost notes, flams); hat (closed
  and pedal); open hat; tom (high, mid, two floor toms; 90 hits); perc (tambourine and side stick; 70
  hits). `hats` is either hat lane on any hi-hat, as IDMT scores it. Rides and crashes (1,002 hits) have no lane: hat-lane
  onsets on them aren't counted against the hat, and are reported. No clap. `--drums-only` scores the
  drum stems instead, to tell mixing losses from classification ones.
- `stemgmd-mix` builds grooves from the [StemGMD](https://zenodo.org/records/7882857) single hits (10
  acoustic kits, 9 classes, 10 velocity variants each, CC BY 4.0): 5 grooves x 10 kits x 96 and 124
  BPM, with toms in fills (alone) and on the kick and snare, open hats, ride and crash. StemGMD has no
  percussion or clap, so the `perc-layer` groove adds the synthetic cowbell from `bench/synth.py`: that
  row is optimistic, being the sample perc was tuned on. Labels are exact.

The onset suites match within 50 ms. A label's onsets are the peaks of its lanes above 0.4x the 99.5th
percentile of that lane family (kick and tom; snare, clap and perc; hat and open hat), so a lane a
track barely uses can't fire on its own leakage. Tom and perc recall is split by whether a kick or
snare lands within 30 ms, and the report says where each lane's false positives landed.

Scores on 2026-10-29 (onset F-measure; before is the old three-band split):

| | Kick | Snare | Hat |
|---|---|---|---|
| IDMT, before | 0.74 | 0.48 | 0.89 |
| IDMT, now | 0.96 | 0.94 | 0.90 |

Synth, now: kick 1.00, clap 1.00, hat 0.91, snare 0.75, open hat 0.69, tom
0.67, perc 0.64.

Real kits, 2026-10-29 (F, with P / R):

| | Kick | Snare | Hats | Open hat | Tom | Perc |
|---|---|---|---|---|---|---|
| MDB, full mix | 0.81 | 0.51 (0.72 / 0.39) | 0.65 | 0.24 (0.15 / 0.56) | 0.01 (0.01 / 0.02) | 0.03 (0.01 / 0.11) |
| MDB, drum stems | 0.96 | 0.60 (0.94 / 0.44) | 0.71 | 0.30 | 0.00 | 0.00 |
| stemgmd-mix | 0.91 | 0.43 (0.41 / 0.45) | 0.72 | 0.52 (0.37 / 0.84) | 0.02 (0.52 / 0.01) | 0.67 (0.99 / 0.50)* |

\* the synthetic cowbell. Real acoustic toms are almost never found, alone or with a kick or snare.
Against the lane's own loudest moment instead of its family's ("lenient" in the report) tom recall is
still 0.09 on stemgmd-mix and 0.16 on the MDB stems: the low part barely moves on a tom, whose attack
lands in the hat (921 false hats on stemgmd-mix toms) and snare (474) lanes instead. On MDB mixes the
tom and perc lanes fire mostly on snares. IDMT kits fire tom on 1 in 100 labeled hits, perc on 0.2
and clap on 0.1. StemGMD's variants span about 19 dB for snares (ghost-note soft) and kicks sit
about 9 dB above snares, part of why its snare recall is low.

Iterate with `--limit`, run the full set before keeping a change. With `--jobs 4` locally: IDMT 21 s,
MDB 22 s, stemgmd-mix 27 s (100 grooves), synth 9 s.

## Downbeats

`python -m tastr.engine.bench.downbeats <folder> --dataset gtzan [--limit N]`
scores which beat core picks as bar 1, on labeled beats, from the Beat This!
spectrogram release. See [rhythm.md](rhythm.md) for the data, scores and
design notes. Only the original recordings are scored; the release's pitch-shifted
and time-stretched copies (`<name>/track_ps-3`, `<name>/track_ts12`) are skipped.

## Tempo on spectrograms

`python -m tastr.engine.bench.tempo_spec <folder> --dataset hjdb [--out results.json]`
runs core's tempo logic on onset envelopes rebuilt from the same release's
spectrograms and scores it against 60 / the median labeled beat interval, per
train / val split. See "Tempo notes" for how close it is to the audio path.
