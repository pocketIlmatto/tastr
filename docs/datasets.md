# Candidate datasets

Public labeled datasets that could extend the benchmarks in [bench.md](bench.md), surveyed 2026-10-29.
None of these are wired into `tastr bench` yet. Sizes are the Zenodo file sizes where the data is on Zenodo.

Cloud sessions can download from Zenodo and PyPI and clone GitHub repos added to the session, but GitHub
release downloads and most audio hosts are blocked. "Local" below means the audio has to be fetched on your
own machine.

## Ranked by expected payoff

| # | Dataset | Labels | Size | Genre fit | License | Audio | Gap it targets |
|---|---|---|---|---|---|---|---|
| 1 | Beatport EDM Key (Zenodo 1101082) | global key, annotator confidence and comments | 1,486 two-minute previews, `audio.zip` 2.1 GB | EDM subgenres | CC BY-SA 4.0 | Zenodo (cloud) | keys a fifth off; a held-out check for key changes tuned on GiantSteps |
| 2 | HJDB in the Beat This! release (Zenodo 13922116, `hjdb.zip`) | beats, downbeats (tempo derivable) | 235 excerpts (17-181 s), each with 21 pitch-shifted / time-stretched copies, 2.7 GB; 120-171 BPM, median 154 | hardcore, jungle, drum & bass | CC BY 4.0 (release) | spectrograms only, Zenodo (cloud) | 2/3-tempo locks on fast breakbeats (`bench.tempo_spec`); few tracks near 174 |
| 3 | StemGMD (Zenodo 7860223) | per-piece drum stems: kick, snare, closed/open hat, 3 toms, crash, ride | 1,224 h, two parts | acoustic kits, grooves | CC BY 4.0 | Zenodo (cloud), take a slice | tom and perc lanes, overlapping hits |
| 4 | MDB Drums (github.com/CarlSouthall/MDBDrums) | 21 drum classes incl. toms and percussion | 23 songs | mixed (MedleyDB) | CC BY-NC-SA | in the repo (clone) | tom/perc on real mixes |
| 5 | E-GMD (magenta.tensorflow.org/datasets/e-gmd) | drum MIDI aligned to audio | 444 h on 43 electronic kits | electronic kits | CC BY 4.0 | large zip, check the host | tom lane on electronic kit sounds |
| 6 | MTG-Jamendo, mood/theme subset (mtg.github.io/mtg-jamendo-dataset) | 56 mood/theme tags | about 18,500 full tracks | electronic-heavy | CC music, uploader tags | MTG server via script (local) | vibe tags vs. human mood tags |
| 7 | DEAM | valence and arousal, static and per second | 1,802 songs/excerpts | mixed | CC audio | outside Zenodo (local) | energy vs. human arousal |

Notes:

- Beatport EDM Key is by the same author as GiantSteps Key (Faraldo). Check its track ids against the 604
  GiantSteps tracks before scoring and drop any overlap. The confidence field shows whether fifth misses
  cluster on tracks the annotators found ambiguous. Its `original_metadata.zip` also lists Beatport's BPM for
  774 of the tracks (`beatport-tempo`), a larger real-audio tempo check than the 43 GiantSteps tempo tracks.
- The Beat This! release holds mel spectrograms, not audio, so it fits the downbeat bench as is; tempo goes
  through the adapter in `bench.tempo_spec`. Each npz also holds augmented copies (`_ps±N`, `_ts±N` keys);
  score only `<name>/track`. Its other zips: Ballroom 4.8 GB, Hainsworth 2.7 GB, SMC 2.1 GB, Harmonix 45 GB.
- StemGMD and E-GMD are far too big to download whole; take a subset of kits or sessions.

## Considered, lower priority

- Extended Ballroom (4,180 tempo/beat excerpts): ballroom genres, not dance music.
- SALAMI (1,359 tracks, sections, CC0 annotations): mostly non-electronic, audio must be gathered yourself.
  Harmonix stays the section benchmark.
- ENST-Drums: real kits with toms, access by request form, non-commercial.
- ADTOF: large drum transcription set from game charts, audio not distributed.
- Isophonics, McGill Billboard, RWC: pop/rock keys and chords, audio not distributed or paid.

## Gaps no public dataset covers

- Downbeats in loops whose bars are identical: the answer is ambiguous, so this stays a synthetic case.
- No public EDM set with section or drop labels and downloadable audio turned up.
- Tastr's own vibe tag names: only labels from your own library (corrections in the UI) cover these.
