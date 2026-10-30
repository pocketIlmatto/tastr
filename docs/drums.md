# Drum lane detection: design notes and learnings

How `tastr/engine/drums.py` finds the lanes in the detected groove, why it
works this way, and what was tried and dropped. Scores and how to run the
benchmark are in [bench.md](bench.md#drum-lanes). Written 2026-10-29
(PR #23).

## Approach

1. **Semi-adaptive NMF, three parts.** The percussive mel spectrogram (square
   root of power) is factored with KL-divergence NMF into a low part (kick), a
   mid part (snare, clap, perc) and a high part (hats). Each part starts from a
   hand-made spectral template, built as Gaussian bumps in log frequency. The
   templates adapt only late and partly (alpha rises to 0.5 over 60
   iterations), so a part can't drift into another drum.
2. **Hat onsets from the air band.** The hat lane uses the mean per-bin dB
   rise above 6 kHz, floored 50 dB under the band's loudest moments, rather
   than the NMF hat part. It found soft hats the NMF part missed.
3. **Per-track leak removal.** NMF still hands part of a hit to a neighbouring
   part. For the pairs kick to snare, hat to snare and snare to kick, the leak
   ratio is the 20th percentile of dst/src over the source's strongest onsets
   (mostly solo hits). That share of the source onset is then subtracted from
   the destination. This is what stops hi-hat bleed into the snare lane.
4. **Per-hit sorting into seven lanes:**
   - a low hit is a **tom** when its pitch is above about 100 to 135 Hz,
     unless a mid hit lands at the same time (then that pitch is the snare's
     body); otherwise it is a **kick**;
   - a mid hit is a **clap** when the 800 Hz to 3 kHz envelope (4 ms
     smoothing) shows 3 or more bursts with at least 6 dB prominence and the
     hit has little 100 to 400 Hz body. It is **perc** when its 400 Hz to 3 kHz
     spectrum is peaky (pitched) without body. Otherwise it is a **snare**;
   - a high hit is an **open hat** when the air band falls less than about
     10 dB in 90 ms (or before the next 16th at fast tempos). Otherwise it is a
     closed **hat**.
5. **What the grid reports.** A lane is detected when its averaged step
   profile is at least 3x its level halfway between 16ths. The new lanes
   (clap, open hat, tom, perc) must also reach 20% of the loudest lane. A mid
   lane that is a faint copy of the kick (correlation above 0.9, under 35% of
   its level) is dropped.

Why not a pretrained drum-transcription model: those packages need
TensorFlow or PyTorch, hundreds of MB. We chose to ship this classifier
first and add a model later as an optional extra if accuracy falls short.

## Results

IDMT-SMT-Drums, full set (94 loops), onset F-measure within 50 ms:

| | Kick | Snare | Hat |
|---|---|---|---|
| Old three-band split | 0.74 | 0.48 | 0.89 |
| Now | 0.96 | 0.94 | 0.90 |

Snare precision went from 0.33 to 0.95. On IDMT, the old split's snare false
positives were almost all at hi-hat hits.

Synthetic loops, step-level F: kick 1.00, clap 1.00, hat 0.91, snare 0.75,
open hat 0.69, tom 0.67, perc 0.64.

## Known weaknesses

- **Toms and perc** are found when they play alone, but missed when they land
  together with a kick or snare: the per-hit spectrum is then dominated by
  the other drum. On IDMT kits, which have neither, they fire on about 3 in
  100 labeled hits.
- **Open hats** score lowest on fast synthetic loops, where there is little
  time between 16ths to see a hat keep ringing.
- **Clap vs snare** relies on the burst pattern. Layered or heavily processed
  claps may read as snare.
- **Real toms are missed.** MDB Drums and grooves built from StemGMD single
  hits now check toms, perc and open hats on real kits (bench.md, "Real
  kits"): real tom F is about 0.02, because a tom barely moves the low part.
  Clap is still checked only on the synthetic kit. These lanes were tuned on
  the synthetic kit, so treat their thresholds as provisional.
- **Grid phase comes from core.** In a full-pipeline check on a synthetic
  loop, the bar grid started half a bar late. The lanes were right but
  shifted by 8 steps. That is the core downbeat estimate, not drum detection.

## Tried and dropped

Measured on a 20-loop IDMT slice (`--limit 20`) and the synthetic suite.

- **Band-share rules without NMF**: classifying each step by the share of its
  energy rise per band. Kicks spilled into the snare shares, and co-hits
  (kick plus clap) broke the shares. It was worse than the old split on
  synthetic loops.
- **Separate NMF templates for clap, tom and perc**: the parts competed. A
  snare split into clap plus tom, and kick body went to tom. Three families
  plus per-hit sorting worked much better.
- **Free NMF parts** (1 to 3 unconstrained components to absorb leftovers):
  on sparse material they took over whole drums (a solo hat or tom vanished
  from its part). Removed.
- **NMF hat part instead of the air-band flux**: hat F 0.83 vs 0.92 on the
  slice. Moving the hat template (6 to 9.5 kHz, 0.5 to 1.2 octaves wide)
  didn't close the gap.
- **Normalising every part before leak removal**: it hurt kick and snare
  (kick 0.98 to 0.95).
- **Leak removal for every pair**: kick to hat erased hats that always land
  with the kick, and snare to hat cost hat recall. Only kick to snare, hat to
  snare and snare to kick are kept.
- **A 50th-percentile leak ratio**: it erased claps that always land on a
  kick (house). 20th it is.
- **2.5 ms envelope smoothing for clap bursts**: snare noise alone produced 5
  "bursts". 4 ms with 6 dB prominence separates them.

## Staging the dataset

IDMT-SMT-Drums is on Zenodo (record 7544164, `IDMT-SMT-DRUMS-V2.zip`, about
290 MB, CC BY-NC-ND 4.0). Cloud sessions can reach zenodo.org; GitHub
archive downloads are refused there. Unzip it and pass the folder that holds
`audio/` and `annotation_xml/`. The license allows no derivatives, so its
audio and labels stay out of the repo, and the templates are hand-made
rather than learned from it.

A synthetic drum loop click artifact is worth knowing about if you extend
`bench/synth.py`: a sample that ends abruptly clicks, and a click reads as a
hat. Every kit sample gets an 8 ms fade, and a closed hat chokes a ringing
open hat, as on a real kit.
