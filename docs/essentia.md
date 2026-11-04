# Essentia classifier bench

Research only. Can MTG's pre-trained [Essentia classifier models](https://essentia.upf.edu/models.html#classifiers)
rank tracks the way listeners label them better than the engine's own vibe measures? Nothing here is part of
the app.

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

## Running it

```
export TASTR_R2_ENDPOINT=https://<account id>.r2.cloudflarestorage.com
modal run tastr/cloud/modal_app.py --kind essentia --dataset mtg-moods --path mtg-moods --limit 200 --out essentia-mtg.json
modal run tastr/cloud/modal_app.py --kind essentia --dataset deam --path deam --limit 200 --out essentia-deam.json
```

Each run fans out over 8 containers (4 CPUs each). `--limit` takes a fixed sample (by a hash of the id), so
reruns score the same clips. The code is in `tastr/engine/bench/essentia_heads.py`.

## Results

### 2026-11-04: 200 DEAM clips + 200 MTG-Jamendo test-set clips

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
  does much better (-0.51 against -0.13). Keep the engine's "dark".
- Danceability is a tie (0.84 against 0.82), so the refit engine formula holds up.
- emoMusic's arousal and valence beat energy and "bright" on DEAM, a dataset neither head saw in training.
  MuSe is weak.
- Voice, tonal, electronic and acoustic are near perfect (0.97-1.00). The engine has nothing like them.
- `gender:female` scores 0.09, which is 0.91 read the other way round. The metadata's class order is probably
  reversed. It isn't needed for any card field.
- "melancholic" and "party" in the mood/theme tags had no tagged track among the 86, so they weren't scored.
- The top Discogs styles look right for these mixed-genre datasets (Ambient, Folk, Indie Rock,
  Downtempo). These datasets can't judge style on house music; Pam's own library has to.

Before any of this goes into the app, the full sets should be run: 3,069 MTG and 1,802 DEAM clips, about
2 hours of CPU, roughly $1-2.
