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

Not run yet.
