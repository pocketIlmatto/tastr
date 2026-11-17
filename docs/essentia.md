# Essentia classifier bench

Can MTG's pre-trained [Essentia classifier models](https://essentia.upf.edu/models.html#classifiers) rank
tracks the way listeners label them better than the engine's own vibe measures? They can (Results below), so
the app can add them as optional ML tags (In the app, below).

## Licenses

- MTG's weights (Discogs-EffNet, MSD-MusiCNN and every classification head) are for non-commercial use only:
  CC BY-NC-SA 4.0 per the models page, CC BY-NC-ND 4.0 per the [LICENSE](https://essentia.upf.edu/models/LICENSE)
  file. They are downloaded into the Modal image at build time and never committed.
- Essentia (`essentia-tensorflow`) is AGPL-3.0. It is installed only in that image, unmodified.

## What it runs

One Discogs-EffNet embedding per clip (16 kHz mono) feeds 16 heads: danceability, the five yes/no moods,
acoustic, electronic, voice/instrumental, gender, tonal/atonal, MTG-Jamendo mood/theme (56 tags) and
instruments (40), Discogs styles (400), engagement and approachability. One MSD-MusiCNN embedding feeds the
emoMusic, MuSe and DEAM arousal/valence heads. Each head's predictions are averaged over the clip. The same
clip also goes through the Rust engine, so every check compares a head and the engine on the same tracks.

Training-set overlap is kept out: the MTG-Jamendo heads were trained on its split-0 training set, and the clean
yes/no annotations cover only its split-0 test set, so only tracks with a yes/no label are used from
`mtg-moods`. The DEAM head is never scored on DEAM.

## In the app (optional, off by default)

With `TASTR_ML_TAGS=on`, a server offers ML tags for one track at a time. With `TASTR_ML_TAGS=auto` (the hosted site's
setting) every new upload gets them after its analysis, and the first start with it queues every track that has
none, once (`meta` key `ml_tags_backfill`). On Modal's 2-CPU container a track costs about $0.001, so about $1 per
1,000 tracks. A hosted server runs them on Modal's
`ml_tags` function (docs/hosting-map.md); a local one needs `pip install 'tastr[ml-tags]'` and downloads the
weights (about 40 MB) into `~/.tastr/essentia-models` on first use (`TASTR_ESSENTIA_MODELS` moves them).

- `POST /api/tracks/{id}/ml-tags` starts a run in the background; `GET` reports its state and the stored result.
  The card's JSON carries `ml_tags`, `ml_tags_available`, `ml_tags_state` and, after a failed run,
  `ml_tags_error`; `/api/tracks` gives each row's ML tag names as `ml_tags` (null before they're in).
- `tastr ml-tags run ID...` or `tastr ml-tags run --all` (every track without ML tags) from the command line.
- Results go in the `ml_tags` table, apart from the card, so re-analysis keeps them and copies share them. Each
  row has every head's output (the big heads cut to their top 10) and the tags drawn from them, comma-separated
  in `tags` for searches.

Tags (`tastr/engine/ml_tags.py`), with cut-offs from the full runs (2026-11-09, below):

| Group | Tags | Shown when |
| --- | --- | --- |
| style | Discogs styles, e.g. Deep House | up to 3 scoring 0.15 or more; the top one from 0.10 |
| mood | happy, aggressive | that head scores 0.7 or more (77% and 95% of shown tracks labeled yes) |
| mood | sad, party | 0.8 or more (57% and 71%) |
| mood | relaxed | 0.85 or more (79%) |
| mood | MTG-Jamendo mood/theme tags that describe music (energetic, uplifting, dark, deep, space ...) | up to 3 scoring 0.12 or more, about the top 5% |
| voice | vocal or instrumental | voice scores 0.7 or more (81% right), or 0.3 or less (99% right) |

When the cut-offs change, `ml_tags.TAGS_PASS` changes with them, and the next server start draws every stored
result's tags again from its stored outputs (`meta` key `ml_tags_cuts`), with no new classifier run.

The mood/theme cut-off is the weakest: the uploader tags it was checked against are sparse.

On the page (`vibeTags` in `static/app/core.js`), a track that has ML tags shows them with a ✦, in a new Style row
and in Mood and Sound, and they replace the engine's weakest vibes: driving → energetic, euphoric → uplifting,
hypnotic → meditative, chill → relaxed, bright → happy. The engine keeps "dark" and the rest. Until a track's ML
tags are in, or when they fail (with Try again), it shows the engine's vibes as before. The engine's `tags` table
is unchanged, so SQL searches see the engine's vibes; Find's vibe search also reads `ml_tags.tags`.

## Running it

```
export TASTR_R2_ENDPOINT=https://<account id>.r2.cloudflarestorage.com
modal run tastr/cloud/modal_app.py --kind essentia --dataset mtg-moods --path mtg-moods --limit 200 --out essentia-mtg.json
modal run tastr/cloud/modal_app.py --kind essentia --dataset deam --path deam --limit 200 --out essentia-deam.json
```

Each run fans out over 8 containers (4 CPUs each). Without `--limit` it runs the whole set. `--limit` takes a fixed sample (by a hash of the id), so
reruns score the same clips. The code is in `tastr/engine/bench/essentia_heads.py`.

## Results

### 2026-11-04: 200 DEAM clips + 200 MTG-Jamendo test-set clips (superseded by the full sets below)

Both runs scored 200 of 200 clips with none failing. Wall time was 155 s for DEAM, including the first image build, and 135 s for MTG.
Each clip took 1.1 s for both embeddings on 4 CPUs, for clips of 50-60 s, so a 5-minute track takes about 6 s. The heads add 0.1 s.
The subsets are small: 86-165 labeled tracks per MTG label. Treat differences under about 0.05 as noise.

MTG-Jamendo, ROC AUC. Each head is compared with the engine measure that answers the same question, on the same tracks:

| Head | Label | n | Head | Engine measure | Engine |
| --- | --- | --- | --- | --- | --- |
| mood_party | party | 147 | 0.995 | danceability | 0.809 |
| mood_aggressive | aggressive | 159 | 0.989 | energy / tag: high energy | 0.854 |
| mood_relaxed | relaxed | 112 | 0.957 | tag: chill | 0.661 |
| moodtheme: uplifting | mood: uplifting | 86 | 0.941 | tag: euphoric | 0.435 |
| mood_sad | sad | 106 | 0.873 | tag: melancholic | 0.798 |
| danceability | danceable | 88 | 0.844 | danceability | 0.823 |
| moodtheme: energetic | mood: energetic | 86 | 0.840 | tag: driving | 0.455 (energy 0.767) |
| mood_happy | happy | 105 | 0.815 | tag: bright / euphoric | 0.597 / 0.542 |
| moodtheme: relaxing | mood: relaxing | 86 | 0.753 | tag: chill | 0.567 |
| moodtheme: meditative | mood: meditative | 86 | 0.693 | tag: hypnotic / atmospheric | 0.439 / 0.624 |
| moodtheme: dark | mood: dark | 86 | 0.675 | tag: dark | 0.677 |
| mood_electronic | electronic | 133 | 0.996 | none | |
| voice_instrumental | voice | 164 | 0.982 | none | |
| mood_acoustic | acoustic | 130 | 0.971 | none | |
| tonal_atonal | tonal | 133 | 0.965 | none | |

DEAM, Spearman against listener ratings:

| Head | Rating | Head | Engine measure | Engine |
| --- | --- | --- | --- | --- |
| emoMusic arousal | arousal | +0.766 | energy | +0.672 |
| emoMusic valence | valence | +0.601 | tag: bright | +0.487 |
| mood_happy | valence | +0.518 | tag: bright | +0.487 |
| mood_sad | valence | -0.500 | tag: melancholic | -0.407 |
| moodtheme: dark | valence | -0.131 | tag: dark | -0.511 |
| MuSe arousal / valence | arousal / valence | +0.230 / +0.327 | | |

Reading it:

- The yes/no mood heads beat the engine on every mood label, by 0.13-0.30 AUC on relaxed, happy, party
  and aggressive. Caveat: the clean labels keep only tracks where all three annotators agreed, which are the
  easy cases. The engine is scored on the same tracks, though, so the comparison is fair.
- The mood/theme head fixes the tags that are at chance today. "energetic" gets 0.84 where "driving" gets
  0.46, "uplifting" 0.94 where "euphoric" gets 0.44, and "meditative" 0.69 where "hypnotic" gets 0.44.
- "dark" is the exception. On MTG the head and the engine tie, and on DEAM's valence the engine's tag
  does much better (-0.51 against -0.13). Keep the engine's "dark" (the full sets below make this mixed).
- Danceability is a tie (0.84 against 0.82), so the refit engine formula holds up.
- emoMusic's arousal and valence beat energy and "bright" on DEAM, a dataset neither head saw in training.
  MuSe is weak.
- Voice, tonal, electronic and acoustic are near perfect (0.97-1.00). The engine has nothing like them.
- `gender:female` scores 0.09, which is 0.91 read the other way round. The metadata's class order is probably
  reversed. It isn't needed for any card field.
- "melancholic" and "party" in the mood/theme tags had no tagged track among the 86, so they weren't scored.
- The top Discogs styles look right for these mixed-genre datasets (Ambient, Folk, Indie Rock,
  Downtempo). These datasets can't judge style on house music; Pam's own library has to.

### 2026-11-09: the full sets, 1,191 MTG-Jamendo test-set clips + 1,802 DEAM clips

Every clip scored, none failing. Wall time was 735 s for MTG and 1,035 s for DEAM (8 containers of 4 CPUs each);
per clip, 1.1 s for the embeddings and 0.12 s for the heads, as before. MTG has 1,191 clips, not the 3,069 the
engine's vibe bench uses, because only tracks with a clean yes/no label are kept (What it runs, above). The scoring
runs on the machine that starts the run and needs scipy, which `tastr[cloud]` now installs.

MTG-Jamendo, ROC AUC, head / engine, with the 200-clip figures for comparison (n is 487-962 per label):

| Head | Label | Full: head / engine | 200 clips |
| --- | --- | --- | --- |
| mood_party | party | 0.978 / 0.802 | 0.995 / 0.809 |
| mood_aggressive | aggressive | 0.966 / 0.848 | 0.989 / 0.854 |
| mood_relaxed | relaxed | 0.926 / 0.631 | 0.957 / 0.661 |
| mood_sad | sad | 0.839 / 0.674 | 0.873 / 0.798 |
| mood_happy | happy | 0.813 / 0.656 (euphoric), 0.644 (bright) | 0.815 / 0.597 |
| danceability | danceable | 0.851 / 0.815 | 0.844 / 0.823 |
| moodtheme: relaxing | mood: relaxing | 0.853 / 0.662 | 0.753 / 0.567 |
| moodtheme: meditative | mood: meditative | 0.790 / 0.412 (hypnotic), 0.733 (atmospheric) | 0.693 / 0.439, 0.624 |
| moodtheme: dark | mood: dark | 0.781 / 0.544 | 0.675 / 0.677 |
| moodtheme: energetic | mood: energetic | 0.755 / 0.419 (driving), 0.704 (energy) | 0.840 / 0.455, 0.767 |
| moodtheme: uplifting | mood: uplifting | 0.705 / 0.517 | 0.941 / 0.435 |
| moodtheme: party | mood: party | 0.899 / 0.763 | not scored |
| moodtheme: melancholic | mood: melancholic | 0.586 / 0.429 | not scored |
| mood_electronic | electronic | 0.985 | 0.996 |
| mood_acoustic | acoustic | 0.979 | 0.971 |
| voice_instrumental | voice | 0.955 | 0.982 |
| tonal_atonal | tonal | 0.918 | 0.965 |

The mood/theme labels are scored on the 487 tracks with any uploader mood tag; only 5 are tagged uplifting, 5
party and 7 melancholic, so those three are too thin to judge (the 200-clip 0.94 for uplifting rested on one track).

DEAM, Spearman, head / engine:

| Head | Rating | Engine measure | Full | 200 clips |
| --- | --- | --- | --- | --- |
| emoMusic | arousal | energy | +0.726 / +0.665 | +0.766 / +0.672 |
| emoMusic | valence | tag: bright | +0.605 / +0.483 | +0.601 / +0.487 |
| mood_happy | valence | tag: bright | +0.557 / +0.483 | +0.518 / +0.487 |
| mood_sad | valence | tag: melancholic | -0.486 / -0.408 | -0.500 / -0.407 |
| moodtheme: dark | valence | tag: dark | -0.162 / -0.481 | -0.131 / -0.511 |
| mood_aggressive | arousal | energy | +0.349 / +0.665 | |
| MuSe | arousal / valence | | +0.138 / +0.328 | +0.230 / +0.327 |

Precision of the yes/no moods on MTG at each cut-off (shown on / labeled yes / recall), base rate in brackets:

| Mood | 0.7 | 0.8 | 0.85 | 0.9 |
| --- | --- | --- | --- | --- |
| sad (15%) | 144 / 42% / 64% | 88 / 57% / 52% | 66 / 59% / 41% | 34 / 68% / 24% |
| relaxed (40%) | 359 / 72% / 93% | 328 / 75% / 89% | 291 / 79% / 83% | 241 / 82% / 72% |
| happy (27%) | 69 / 77% / 29% | 42 / 86% / 20% | 29 / 90% / 14% | 17 / 94% / 9% |
| party (10%) | 121 / 63% / 87% | 99 / 71% / 80% | 85 / 78% / 76% | 74 / 81% / 69% |
| aggressive (12%) | 56 / 95% / 50% | 42 / 93% / 37% | 32 / 100% / 30% | 23 / 100% / 22% |

On DEAM every mood tag moves the listener ratings the expected way at every cut-off: for example "happy" at 0.7
shows on 9% of clips with mean valence 6.0 against 4.8 for the rest, "relaxed" at 0.85 on 43% with arousal 4.1
against 5.4.

Reading it, and what changed in the app:

- The yes/no mood heads still beat the engine on every mood (by 0.15-0.30 AUC), and emoMusic still beats energy
  and "bright". Danceability is still a tie.
- "sad" was too loose at 0.7 (42% right) and now needs 0.8 (57%). "relaxed" showed on 60% of DEAM clips at 0.7
  and now needs 0.85 (79% right, 43% of DEAM). "party" moves to 0.8 (71% right, recall 80%). "happy" stays at
  0.7, where recall is already low (29%), and "aggressive" stays at 0.7 (95%).
- The mood/theme cut-off stays at 0.12: precision against the sparse uploader tags is flat from 0.10 to 0.15
  (relaxing 24%, dark 21%, energetic 13%, meditative 13%, at 2.3-3.6 times their base rates). The jumps at 0.20
  rest on 4-36 tracks.
- "dark" is now mixed: on MTG the head clearly beats the engine (0.78 against 0.54), on DEAM valence the engine
  still wins by far (-0.48 against -0.16). The app keeps the engine's "dark" until Pam's own library says otherwise.
- `gender:female` is 0.096 again, so its class order is reversed. Still unused.
- The style cut-off can't be checked here (no style labels); the top styles are again Ambient, Folk, Indie and
  Alternative Rock, Experimental and Downtempo.
