# Analysis engine report

Every accuracy change to Tastr's analysis engine so far, grouped by what the track card shows. Each one
lists the dataset that exposed the problem, the dataset used to check the fix, the scores before and after,
and what is still open. Suggested next steps are at the end. Written 2026-10-31; the numbers are copied from
the detailed write-ups ([bench.md](bench.md), [rhythm.md](rhythm.md), [drums.md](drums.md),
[vibes.md](vibes.md)), which have the methods, commands and everything that was tried and dropped.

"Held out" means the tracks were not used to pick the change. Higher is better on every score here except
the Brier score.

## Highlights

| Area | Score | Before | Now | Dataset |
|---|---|---|---|---|
| Key | exact key, held out | 47.6% | 60.4% | GiantSteps Key, 599 EDM previews |
| Key | exact key, held out | 46.7% | 51.5% | Beatport EDM Key, 1,122 previews |
| Tempo | BPM within 4% | 0.488 | 0.744 | GiantSteps Tempo, 43 tracks |
| Tempo | BPM within 4% | 0.829 | 0.971 | hjdb (jungle / hardcore), 35 held-out tracks |
| Bar 1 | right phase | 0.43 | 0.64 | GTZAN, 933 songs in 4/4 |
| 3/4 time | waltzes found | none | 0.86 | GTZAN, 49 songs in 3/4 |
| Drums | tom onset F | 0.013 | 0.369 | MDB Drums, real mixes (optional model) |
| Sections | boundary F within 3 s | 0.477 | 0.577 | Harmonix Set, 912 songs |
| Danceability | AUC vs listeners' "danceable" | 0.702 | 0.815 | MTG-Jamendo, 510 tracks |

## Key

Two steps. First, per-frame normalized chroma plus a small bonus for minor keys fixed most "minor heard as
major" errors. Then a learned 24-key model replaced the Krumhansl-Kessler template. It is trained at all 12
transpositions, so it learns how a key sounds relative to its tonic rather than which keys a dataset holds.

- **Found with:** the synthetic suite showed relative-key swaps; the first GiantSteps Key run showed the real
  top error was parallel mode, then fifths. Beatport EDM Key (no overlap with GiantSteps) scored much lower
  (MIREX 0.537), so the method was partly fitted to GiantSteps.
- **Checked on:** alternating held-out halves of GiantSteps Key and Beatport's confidence-2 labels. The
  learned model wins on all four halves, by 0.025 to 0.054 MIREX.

| Score | Before | Now |
|---|---|---|
| GiantSteps, MIREX weighted (chroma + minor prior) | 0.570 | 0.650 |
| GiantSteps, MIREX weighted (learned model, held out) | 0.650 | 0.682 |
| Beatport, MIREX weighted (learned model, held out) | 0.562 | 0.606 |

**Still open:** keys a fifth off (67 of 599 GiantSteps tracks) did not move with any profile or decision
tweak. The learned model also reads some root-heavy major progressions as their IV key (6 of 48 synthetic
tracks). Both point to new inputs such as bass-line and chord-progression cues. Details:
[bench.md, Key detection notes](bench.md#key-detection-notes).

## Tempo

Fast breakbeat music was usually read at 2/3 tempo (117 for 174), because the beat tracker prefers tempos
near 120. Core now re-tracks at 1.5x when the tempogram is nearly as strong there, and skips that switch when
the faster beats group in threes, which stopped 4/4 songs jumping to 1.5x.

- **Found with:** the 43 GiantSteps Tempo tracks with audio, where 1 of 16 tracks at 165 BPM or faster was
  right. GTZAN later showed the 1.5x switch firing wrongly on 101 of 999 tracks.
- **Checked on:** hjdb (235 jungle / hardcore tracks from the Beat This! release, train / val split),
  Beatport's listed BPMs (774), GiantSteps Tempo and GTZAN. The meter gate was picked on GTZAN only, so the
  other sets are held out for it.

| Set | Tracks | Before | Now |
|---|---|---|---|
| GiantSteps Tempo | 43 | 0.488 | 0.744 |
| hjdb val | 35 | 0.829 | 0.971 |
| hjdb train | 200 | 0.755 | 0.915 |
| Beatport listed BPM | 774 | 0.872 | 0.897 |
| GTZAN | 999 | 0.585 | 0.673 |

BPM within 4% (Acc1).

**Still open:** drum & bass read at half tempo (86 for 174), and half-time tracks labeled 86 read at 172.
Neither the tempogram nor off-beat onsets separate them, and Beatport's own drum & bass labels mix three
conventions. Five rules were tried; each cost 5 to 48 correct Beatport tracks. Details:
[bench.md, Tempo notes](bench.md#tempo-notes).

## Confidence

The card used to show raw evidence scores ("94% conf") that could be high on a wrong answer. It now shows
"N% likely right": each raw score maps to the share of benchmark tracks with that score that were right.

- **Checked on:** BPM from 1,052 tracks (Beatport listed BPM, GiantSteps Tempo, hjdb); key from 1,721
  out-of-fold rows (GiantSteps Key, Beatport EDM Key).

| Held-out Brier (lower is better) | Raw score | One flat rate | Calibrated |
|---|---|---|---|
| BPM | 0.284 | 0.109 | 0.095 |
| Key | 0.280 | 0.249 | 0.224 |

**Still open:** the raw BPM score bunches between 0.41 and 0.47, so it mostly separates the bottom fifth
(about 60% right) from the rest (about 94%). Details: [bench.md, Confidence calibration](bench.md#confidence-calibration).

## Bar 1 (downbeats)

On four-on-the-floor music every beat has the same kick, so the old picker often put bar 1 half a bar late
and shifted the groove by 8 steps. Bar 1 is now picked from five equally weighted cues: kick, backbeat,
timbre / chord change, bass change and bar-to-bar change, with near-silence masked out.

- **Checked on:** GTZAN (933 songs in 4/4) from the Beat This! release, using the labeled beats so only the
  bar-1 choice is scored. A weight grid search overfit (0.66 on its tuning tracks, 0.57 on the next 300), so
  equal weights shipped.

| GTZAN | Before | Now |
|---|---|---|
| Bar 1 right | 0.43 | 0.64 |
| Half a bar off | 0.30 | 0.17 |
| Disco, bar 1 right | 0.28 | 0.65 |

**Still open:** loops whose bars are identical carry no bar-level information. GTZAN clips are 30 s, so
nothing measures full tracks from their start. Details: [rhythm.md](rhythm.md).

## 3/4 time

Core guesses whether the beats group in 3s or 4s from six cues (timbre and bass repeating every 3 vs 4
beats, kick and snare patterns, change cues). A 3/4 track gets 3-beat bars, a 12-step groove, and 3/4 MIDI
and Strudel. It can be overruled per track, or turned off with `TASTR_METER=4/4`.

- **Checked on:** meter labels in the Beat This! annotations for GTZAN (49 in 3/4, 925 in 4/4). The cues
  were tuned on GTZAN, so these are not held-out numbers.

| GTZAN | Labeled beats | Core's own beats |
|---|---|---|
| 3/4 songs found | 0.86 | 0.67 |
| 4/4 songs called 3/4 | 2% | 3.7% (12% before the tempo fix above) |

**Still open:** no held-out check yet; 6/8 and songs that change meter are out of scope. Details:
[rhythm.md, Meter](rhythm.md#meter-34-or-44-2026-10-30).

## Drum lanes

The original three-band split became a semi-adaptive NMF with leak removal and per-hit sorting into seven
lanes (kick, snare, clap, hat, open hat, tom, perc). Real-kit benchmarks then showed real toms were almost
never found, so an optional pretrained model ([ADTOF](https://github.com/MZehren/ADTOF)) can supply the
kick, hat and tom lanes and add a cymbal lane. Snare, clap, perc and open hat stay built in. ADTOF's weights
are non-commercial (CC BY-NC-SA 4.0), so they download on first use and are not in this repo.

- **Found with:** IDMT-SMT-Drums for snare / hat bleed; MDB Drums (real mixes) and grooves built from
  StemGMD single hits for toms and perc.
- **Checked on:** StemGMD kits split 5 to tune and 5 to test, MDB held out, IDMT as a guardrail. None of these
  sets is in ADTOF's training data.

| Onset F | Before | Now |
|---|---|---|
| IDMT snare (built-in rewrite) | 0.48 | 0.94 |
| IDMT kick (built-in rewrite, then model) | 0.74 | 0.977 |
| MDB mixes, tom (model) | 0.013 | 0.369 |
| StemGMD test kits, tom (model) | 0.002 | 0.880 |
| MDB mixes, either hat (model) | 0.649 | 0.753 |
| MDB mixes, cymbal (model, new lane) | none | 0.832 |

**Still open:** ADTOF's snare is much better on MDB and StemGMD (0.74, 0.91) but worse on IDMT's sampled and
techno kits (0.88 vs 0.94), so the built-in snare stays. Model toms on MDB are found but imprecise (precision
0.25). Clap has only a synthetic check, and real perc (tambourine, side stick) scores near zero. Details:
[drums.md](drums.md).

## Sections

Sections were too long (one per 28 s) and snapped to 4-bar phrases, but only 51% of labeled boundaries fall
on a 4-bar phrase. The stage now asks for one section per 18 s and snaps to 2 bars.

- **Found with:** the Harmonix annotations alone: sections are 20.5 s at the median, and 28 s caps the best
  possible F at 0.79.
- **Checked on:** all 912 Harmonix songs, from its mel spectrograms, split at random into tune and check
  halves. Scores are the check half.

| Harmonix, check half | Before | Now |
|---|---|---|
| Boundary F within 3 s | 0.477 | 0.577 |
| Boundary F within 0.5 s | 0.302 | 0.381 |
| Sections per song vs labels | 3.1 too few | 0.09 too many |

**Still open:** 55 songs score 0 with about the right count, which looks like a systematic offset. Harmonix
is mostly pop, and no public dance-music set with drop labels and audio turned up. Details:
[bench.md, Sections](bench.md#sections).

## Danceability, energy and vibe tags

These were hand-picked weights. Danceability was refit to 0.75 percussiveness plus 0.25 tempo fit (a bell
around 135 BPM), dropping pulse clarity. Energy was checked and its hand-picked weights kept, because refits
on the two datasets pulled in opposite directions.

- **Checked on:** MTG-Jamendo listener labels (fit on 70% of 2,466 tracks, confirmed on 3,069 including
  about 600 the fit never saw) and DEAM listener arousal / valence (1,802 clips). AUC: 0.5 is chance.

| Score | Before | Now |
|---|---|---|
| Danceability vs "danceable", AUC | 0.702 | 0.815 |
| Danceability vs "party", AUC | 0.679 | 0.802 |
| "danceable" tag vs "danceable", AUC | 0.508 | 0.741 |
| Energy vs "aggressive", AUC | 0.848 | kept |
| Energy vs DEAM arousal, Spearman | 0.665 | kept |

**Still open:** "driving" and "hypnotic" are at chance, and "atmospheric" shows on most tracks (its cut-off
is too low). Descriptive tags (bass-heavy, percussive, swung) have no public labels. Details: [vibes.md](vibes.md).

## Catalog lookups

Not an engine change, but a second opinion on it: with lookups turned on, the card shows the BPM and key
that FreqBlog, GetSongBPM or AcousticBrainz list for a track, marked as matching, half or double tempo, a
related key, or different. They never replace Tastr's own numbers. AcousticBrainz uses a local index of its
2022 public-domain dump (7.56 million recordings), whose coverage of recent electronic releases is thin.
Details: [lookup.md](lookup.md).

## Datasets

None of these are in the repo. Non-commercial sets contribute only fitted numbers and scores.

| Dataset | Used for | Tracks | License |
|---|---|---|---|
| [GiantSteps Key](https://github.com/GiantSteps/giantsteps-key-dataset) (+ GiantSteps+ audio) | key: discovery, training, held-out halves | 599 | CC BY-SA |
| [GiantSteps Tempo](https://github.com/GiantSteps/giantsteps-tempo-dataset) | tempo (only 43 have audio) | 43 | CC BY-SA |
| [Beatport EDM Key](https://zenodo.org/records/1101082) | key held-out check, then training; listed BPMs for tempo | 1,275 / 774 | CC BY-SA 4.0 |
| [Beat This!](https://zenodo.org/records/13922116) GTZAN | bar 1, 3/4 meter, tempo meter gate | 933 to 999 | CC BY 4.0 |
| [Beat This!](https://zenodo.org/records/13922116) hjdb | fast breakbeat tempo, BPM calibration | 235 | CC BY 4.0 |
| [Harmonix Set](https://github.com/urinieto/harmonixset) (mel spectrograms) | section length and phrase snap | 912 | MIT (annotations) |
| [IDMT-SMT-Drums](https://zenodo.org/records/7544164) | kick, snare, hat; guardrail for drum changes | 95 loops | CC BY-NC-ND |
| [MDB Drums](https://github.com/CarlSouthall/MDBDrums) | toms, perc, open hats on real mixes | 23 songs | CC BY-NC-SA |
| [StemGMD](https://zenodo.org/records/7882857) single hits | grooves on 10 acoustic kits, tom tuning | 100 grooves | CC BY 4.0 |
| [MTG-Jamendo](https://github.com/MTG/mtg-jamendo-dataset) moods + yes/no labels | danceability refit, energy and tag checks | 3,069 | CC BY-NC-SA |
| [DEAM](https://cvml.unige.ch/databases/DEAM/) | energy vs arousal, tags vs valence | 1,802 | CC BY-NC |
| AcousticBrainz dump | catalog lookup index | 7.56 M | CC0 |
| Synthetic suite (`tastr bench synth`) | smoke tests; key model training profiles | 12 + 48 | this repo |

Most benchmarks are two-minute EDM previews or 30 to 60 s clips, so full-length tracks are not measured
directly.

## Next steps

Roughly in order of payoff for a dance-music library.

1. **Refresh the quoted scores and the BPM calibration.** The scores in `bench_scores.json` and the BPM
   "likely right" table predate the tempo meter gate (Beatport listed BPM 0.872 there, 0.897 now). A
   `--record` run and a calibration refit.
2. **Labels from your own library.** Use low-confidence tracks and "Listed elsewhere" disagreements as a
   review queue, save corrections as a `manifest` set, and score changes against it too. It is the only way
   to check drops, descriptive tags and drum & bass conventions on the music Tastr is used for.
3. **Key fifths with new inputs.** Profile tweaks are exhausted; try bass-line and chord-progression cues as
   model inputs, which may also fix the "major read as IV" weakness.
4. **A held-out check for 3/4 and tempo on Ballroom** (Beat This! release: 173 waltzes, 512 in 4/4; a 4.8 GB
   zip).
5. **Drum & bass half tempo.** Try a snare-placement cue (backbeat on 2 and 4 at 174 vs at 87). Needs labels
   that agree on a convention; hjdb has only 3 tracks at 170 BPM or faster.
6. **The 55 zero-score Harmonix songs.** About the right section count but no boundary within 3 s, which
   suggests an offset in the first downbeat or the bench's bar grid.
7. **Weak vibe tags.** Set the cut-offs for atmospheric, driving and hypnotic (and bass-heavy, percussive,
   swung) from a library's own spread, then rerun the MTG-Jamendo / DEAM bench to check nothing regresses.
8. **Drum-model trade-offs.** Measure "ADTOF snare only where the built-in snare also fires" on MDB and IDMT,
   and compare the model's open hats there.
9. **Faster lookups.** Read MusicBrainz recording ids from file tags when present, to skip the search.
