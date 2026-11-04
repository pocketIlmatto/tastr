# Tuning stem separation

The app separates stems with Demucs `htdemucs` at the library defaults: shifts 1, overlap 0.25, 16-bit FLAC
at 44.1 kHz, on a Modal T4 (`tastr/stems.py`, `tastr/cloud/modal_app.py`). This page tracks whether other
settings sound better and what they cost.

## Listening test

`scripts/stem_ab.py` separates a few tracks with each config below on a Modal GPU and shuffles them into
folders A to D per track, so you can listen blind. `answers.json` says which is which and `timings.csv` has
the GPU seconds per run.

```
modal run scripts/stem_ab.py --audio-dir PLAYLIST/audio --out-dir stem-ab --limit 5
```

| Config | Model | Shifts | Overlap |
| --- | --- | --- | --- |
| today | htdemucs | 1 | 0.25 |
| shifts2-overlap0.5 | htdemucs | 2 | 0.5 |
| ft | htdemucs_ft (one fine-tuned model per stem) | 1 | 0.25 |
| ft-shifts2-overlap0.5 | htdemucs_ft | 2 | 0.5 |

The script sends the audio from your machine and returns the stems there; nothing goes to R2. Keep the output
out of the repo, since it holds your music.

## Results

Not run yet.
