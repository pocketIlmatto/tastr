# Vibe benchmark: grounding danceability, energy and vibe tags

Danceability, energy and the vibe tags are weighted sums and thresholds that were picked by hand when the
engine was first written and haven't been tuned since. This benchmark checks them against what listeners
said about real tracks, and refits the weights where the labels allow it.

| Number | Where | Checked against |
| --- | --- | --- |
| danceability = 0.55 pulse clarity + 0.25 tempo fit (bell around 122 BPM, width 30) + 0.20 percussiveness | `stages/core.py` | MTG-Jamendo danceable, party |
| energy = 0.35 loudness ((dB+30)/22) + 0.25 onset rate (/6 per s) + 0.20 brightness + 0.20 percussiveness (/0.5) | `stages/core.py` | DEAM arousal; MTG-Jamendo aggressive, energetic, relaxed |
| mood-like tags: high energy, chill, melancholic, dark, bright, euphoric, danceable, atmospheric | `stages/tags.py` | MTG-Jamendo yes/no moods and mood/theme tags, DEAM valence |
| descriptive tags: bass-heavy, percussive, swung, dynamic, driving, hypnotic | `stages/tags.py` | no public labels; meant to be set from the spread of your own library |
| show cut-off 0.20 | `stages/tags.py` | the share of tracks showing a tag that listeners labeled the same way |

The section length target (one section per 28 s) is handled separately, against Harmonix.

## Datasets

| Name | Labels | Audio | License |
| --- | --- | --- | --- |
| `mtg-moods`: [MTG-Jamendo](https://github.com/MTG/mtg-jamendo-dataset), [music-classification annotations](https://github.com/MTG/mtg-jamendo-dataset/blob/master/derived/music-classification-annotations/README.md) + mood/theme tags | yes/no for danceable (1,698 yes / 2,759 no), party, aggressive, relaxed, happy, sad, only where all three annotators agreed; 59 uploader mood/theme tags | 100 tar archives of about 1.6 GB (low quality); `tastr fetch` keeps only labeled tracks, cut to the middle 60 s | audio CC per track, metadata CC BY-NC-SA 4.0; non-commercial research only |
| `deam`: [DEAM](https://cvml.unige.ch/databases/DEAM/) | listener arousal and valence, 1-9, averaged over each clip | 1,802 clips, mostly 45 s, 1.3 GB | CC BY-NC |

Neither may be redistributed for commercial use. They live in the private bucket only; the repo holds
loaders, results and fitted numbers, never audio or labels.

Caveats:

- Jamendo is mixed genre, not mostly dance music. Your own corrections remain the final check for EDM.
- Danceability is the noisiest label (the annotators agreed on 44% of tracks), so its clean set is small.
- Uploader mood tags are sparse: a track without "dark" may still be dark. Those are only scored by ranking.
- DEAM's audio host isn't reachable from Claude's cloud sessions; Modal and your Mac reach it. The zip names
  in `tastr/cloud/fetch.py` (`DEAM_audio.zip`, `DEAM_Annotations.zip`) are the ones the download page used;
  if they move, pass the new URLs to `fetch_deam`.

## Running it

```
tastr fetch mtg-moods mtg-moods --archives 0-5 --remote   # about 9.4 GB streamed, about 1 GB kept
tastr fetch deam deam --remote                            # 1.3 GB
tastr vibe-bench mtg-moods mtg-moods --remote --out mtg.json
tastr vibe-bench deam deam --remote --out deam.json
tastr vibe-fit mtg.json deam.json --out fit.json
```

Without `--remote` the same commands work on a local folder. `fetch` writes the dataset's
`tastr-index-<dataset>.json`, so remote runs don't walk the bucket. A rerun of `fetch` skips clips already
there; it reports archives whose checksum didn't match (a cut-off download), and rerunning fills them in.

`vibe-bench` prints, for each measure and the labels it should agree with, the ROC AUC (yes/no labels: the
chance a "yes" track scores above a "no" one; 0.5 is chance) or Spearman correlation (arousal, valence).
For tags it also shows how many tracks show the tag at the 0.20 cut-off and what share of them listeners
labeled yes, next to the base rate.

`vibe-fit` refits danceability and energy as weighted sums of the same inputs, weights non-negative and
summing to 1 in steps of 0.05, plus the tempo bell's center and width for danceability. It fits on a fixed
70% of the tracks (split by a hash of the id) and reports both today's and the fitted weights on the other
30%. A refit only goes into the engine if it wins on the held-out tracks and makes musical sense.

## Results

### 2026-10-30: MTG-Jamendo subset, today's weights

1,567 labeled tracks from archives 00-05, middle 60 s of each, 0 failed (`vibe-bench` on Modal: 23 min wall,
16 min analysis). DEAM isn't in yet: its server (cvml.unige.ch) timed out from Modal and from a Mac.

| Measure | Label | n | AUC | Shown at 0.20 | Share of shown labeled yes (base rate) |
| --- | --- | --- | --- | --- | --- |
| energy | aggressive | 451 | 0.896 | | |
| energy | relaxed | 342 | 0.120 (ranks relaxed tracks low, as it should) | | |
| energy | mood: energetic | 1,200 | 0.737 | | |
| danceability | danceable | 244 | 0.670 | | |
| danceability | party | 438 | 0.609 | | |
| danceability | mood: party | 1,200 | 0.797 | | |
| tag: high energy | aggressive | 451 | 0.885 | 133 | 0.35 (0.11) |
| tag: chill | relaxed | 342 | 0.647 | 31 | 0.84 (0.38) |
| tag: melancholic | sad | 333 | 0.692 | 55 | 0.36 (0.12) |
| tag: euphoric | happy | 364 | 0.647 | 40 | 0.78 (0.29) |
| tag: bright | happy | 364 | 0.655 | 145 | 0.41 (0.29) |
| tag: atmospheric | mood: meditative | 1,200 | 0.703 | 845 | 0.05 (0.04) |
| tag: danceable | danceable | 244 | 0.515 | 7 | 0.57 (0.37) |
| tag: dark | mood: dark | 1,200 | 0.488 | 488 | 0.05 (0.06) |
| tag: driving | mood: energetic | 1,200 | 0.488 | 23 | 0.00 (0.06) |
| tag: hypnotic | mood: meditative | 1,200 | 0.484 | 28 | 0.00 (0.04) |

Reading it:

- Energy already ranks tracks the way listeners do (AUC 0.90 against "aggressive").
- Danceability is weaker (0.67 against "danceable").
- High energy, chill, melancholic and euphoric pick out the right tracks well above the base rate.
- "danceable" almost never shows, because 0.55 danceability is a high bar on this scale.
- "atmospheric" shows on 70% of tracks, so its cut-off is too low.
- "dark", "driving" and "hypnotic" do no better than chance against the closest labels. Uploader tags are sparse, though, so this is weak evidence against them.

### 2026-10-30: refit on the same tracks (`vibe-fit`, 70/30 split)

| Measure vs label | Held out | Today | Fitted | Fitted weights |
| --- | --- | --- | --- | --- |
| danceability vs danceable | 73 | 0.714 | 0.864 | pulse clarity 0, tempo fit 0.40, percussiveness 0.60; bell 135 +/- 45 |
| danceability vs party | 128 | 0.613 | 0.818 | pulse clarity 0, tempo fit 0.30, percussiveness 0.70; bell 135 +/- 15 |
| energy vs aggressive | 136 | 0.838 | 0.909 | loudness 0.60, onset rate 0, brightness 0.25, percussiveness 0.15 |
| energy vs mood: energetic | 354 | 0.757 | 0.780 | loudness 0.35, onset rate 0.05, brightness 0.50, percussiveness 0.10 |

- Both danceability fits drop pulse clarity and put about two thirds of the weight on percussiveness. They
  gain about 0.15-0.2 AUC on tracks the fit never saw. The tempo bell's width disagrees between the two
  labels and the held-out sets are small (73 and 128), so the bell is the least certain part.
- The energy fits disagree on the second input (brightness vs. loudness), and the gains are small. Today's
  energy weights are close enough to keep until DEAM's arousal ratings can settle it.
- None of this is in the engine yet. Jamendo is mixed genre, so check any change on dance music before
  shipping it.
