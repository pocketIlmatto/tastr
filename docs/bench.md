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
| `giantsteps-key` | global key | 604 two-minute EDM previews | github.com/GiantStepsMIR/giantsteps-key-dataset (`audio_dl.sh` fetches the audio) |
| `giantsteps-tempo` | global BPM | 664 EDM previews | github.com/GiantStepsMIR/giantsteps-tempo-dataset (uses `annotations_v2` when present) |
| `harmonix` | BPM, beats, downbeats, sections | 912 pop/EDM tracks | github.com/urinieto/harmonixset; audio is not distributed, put your copies in `<root>/audio` named by track id |
| `manifest` | whatever you fill in | your tracks | a CSV or JSON file, below |
| `synth` | everything, exact | 12 generated tracks | built in; a smoke test, not a benchmark |

GiantSteps is the closest match to dance music libraries and is the main key
and tempo benchmark. Harmonix is the one with sections and downbeats.

### Your own tracks

A CSV (or a JSON list of objects) with these columns, all optional except `path`:

```
path,bpm,key,boundaries,labels
Night Shift (Extended Mix).mp3,124,A minor,"0:00 1:01 2:02 3:34",intro build drop breakdown
```

`path` is relative to the manifest file. `key` accepts "A minor", "Am",
"F# maj" or Camelot codes like "8A". `boundaries` are section start times in
seconds or m:ss.
