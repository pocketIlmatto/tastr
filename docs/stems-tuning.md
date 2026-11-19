# Tuning stem separation

The app separates stems with Demucs `htdemucs_ft` (`htdemucs` until 2026-11-17) at the library defaults: shifts 1,
overlap 0.25, 16-bit FLAC at 44.1 kHz, on a Modal T4 (`tastr/stems.py`, `tastr/cloud/modal_app.py`). This page
tracks whether other settings sound better and what they cost, and how the app knows which settings made a
track's stems.

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

Open `OUT/listen.html` to listen: it plays the FLACs next to it, switches letters in sync, and records your best
and worst pick per track (`python scripts/stem_ab_page.py OUT` rewrites it). The script sends the audio from your machine and returns the stems there; nothing goes to R2. Keep the output
out of the repo, since it holds your music.

## Results

First run 2026-11-04: 5 tracks from one of Pam's playlists, 20 runs on a T4,
1,463 GPU seconds in all (about $0.25 at T4 list price).

| Config | Average GPU seconds per track | vs today |
| --- | ---: | ---: |
| today | 28 | 1x |
| shifts2-overlap0.5 | 53 | 1.9x |
| ft | 64 | 2.3x |
| ft-shifts2-overlap0.5 | 148 | 5.3x |

Listening verdict: pending.

## Six stems (guitar and piano)

Suno-style splits go past four stems. A survey on 2026-11-19 found that only Demucs' `htdemucs_6s` (adds guitar
and piano) has the same MIT terms as today's model; the stronger RoFormer models (a 6-stem BS-RoFormer "SW", and
MVSep's 53-stem model with strings, brass and synth) publish weights with no clear licence or known trainer, and
need a bigger GPU. Demucs' own notes say the 6-stem guitar is fair and the piano bleeds.

`--set six` runs three 6-stem configs instead of the four above, blind as before (letters A to C):

```
modal run scripts/stem_ab.py --audio-dir PLAYLIST/audio --out-dir stem-ab-six --limit 5 --set six
```

| Config | What it does | Shippable |
| --- | --- | --- |
| 6s | `htdemucs_6s` alone | yes (MIT) |
| hybrid | `htdemucs_ft` for vocals, drums and bass, `htdemucs_6s` for guitar and piano, other = mix minus those five | yes (MIT) |
| roformer-sw | BS-RoFormer SW 6-stem via `bs-roformer-infer` | no: yardstick only (weights unlicensed) |

`listen.html` adds guitar and piano buttons (keys `g` and `p`) when every version has them. Tracks with real
guitar or piano parts tell the most. Timings count only the GPU work (not decoding the track).

Results: pending.

## Which model made each stem set

Each stem folder keeps a `stems.json` next to its FLACs (on disk and in the audio bucket), written when the
separation finishes:

```json
{"model": "htdemucs_ft", "shifts": 1, "overlap": 0.25, "demucs": "4.1.0", "device": "cuda", "made_at": "2026-11-17T01:20:00Z"}
```

On Modal, `run_stems` returns these settings with the stems and the server writes the file (straight to the bucket
when the stems never touch its disk). A folder without `stems.json` predates the record, so it counts as
`htdemucs` with the defaults rather than being guessed from dates. `GET /api/tracks/{id}/stems` reports it for
ready stems as `made_with`, with `recorded` (false: no `stems.json`) and `outdated` (made with another model than
today's default) and `latest` (that default), and `tastr stems ID` says which model made existing stems.
Separating again (`--force`, `POST /api/tracks/{id}/stems?again=1`, or after deleting the stems) replaces the
record. The app offers it for outdated sets (Files, and the Strudel stems picker; [features.md](features.md#files)).

While a set is separated again the old one stays: the status stays `ready` and `again` holds the new job's state
(queued, running, or error, in which case the old set is untouched). On this machine the new stems go to a
`.partial` folder and are swapped in with two renames. On Modal, `run_stems` uploads to signed links under a
staging folder (`stems/<hash>.staging/`); once all four are there the server copies them over the old keys inside
the bucket (S3 CopyObject), writes `stems.json`, and deletes the staging copy. The old set is copied aside
(`stems/<hash>.old/`) first, so a copy that fails partway puts it back rather than leaving a mix of old and new. A separation again counts toward
the daily stems limit like a first one. Stems made between the switch to `htdemucs_ft` and this record also
have no `stems.json`, so they show as `htdemucs` too; separating them again is harmless.
