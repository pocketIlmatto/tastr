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
- **Real toms are missed by the built-in detector.** MDB Drums and grooves
  built from StemGMD single hits check toms, perc and open hats on real kits
  (bench.md, "Real kits"): real tom F is about 0.02. Real toms pitch at 70 to
  140 Hz (a low tom sits where a kick does) and mostly land in the snare part,
  whose hits are never sorted into toms. The optional drum model (below) fixes
  most of this. Clap is still checked only on the synthetic kit, and perc only
  on a synthetic cowbell: MDB's perc is tambourine and side stick, which the
  pitched-mid test isn't designed to find.
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

### Tom and perc variants (2026-10-30)

Tuned on 5 StemGMD kits (stemgmd-mix `--kits train`), held out on the other 5,
MDB mixes and MDB drum stems; guardrails were IDMT (kick, snare and hat may
drop at most 0.01) and synth. Tom F (P / R):

| Variant | Test kits | MDB stems | MDB mixes | IDMT kick / snare / hat | Synth tom / perc |
|---|---|---|---|---|---|
| Built-in (pitch rule) | 0.00 (0.25 / 0.00) | 0.00 | 0.01 | 0.956 / 0.935 / 0.904 | 0.67 / 0.64 |
| Low-band share + decay rule instead of pitch | 0.16 (0.71 / 0.09) | 0.20 | 0.04 | 0.92 kick | 0.00 / 0.57 |
| The rule as well as pitch, kick untouched | 0.11 (0.66 / 0.06) | 0.20 | 0.03 | 0.942 / 0.938 / 0.904 | 0.55 / 0.57 |
| Logistic per-hit tom model (C=0.1) | 0.27 (0.58 / 0.17) | 0.02 | 0.01 | 0.88 kick | 0.12 / 0.64 |
| The same, C=0.01, gated at p 0.8 to 0.97 | 0.17 (0.70 / 0.10) | 0.04 | 0.02 | 0.943 kick, toms on 12% of IDMT hits | 0.25 / 0.64 |
| The model, with the tom's share taken out of the snare | 0.27 | 0.02 | 0.01 | snare 0.66 | |
| The rule as well as pitch, times the model | 0.11 (0.76 / 0.06) | 0.21 | 0.03 | 0.951 / 0.938 / 0.904 | 0.55 / 0.64 |

Only the last stayed inside the guardrails, and its gain was small. Two
things capped all of them: toms are about 1% of the hits in real songs
(34% in the training grooves), so even a good ranking (AUC 0.88 on MDB
stems) gives poor precision; and StemGMD toms peak about 9 dB under the
kicks, below the bench's 0.4 x family threshold. A per-hit look at the
low-band share (about 0.2 of 30 to 500 Hz energy below 100 Hz, against 0.07
for snares and 0.65 for kicks) is what separates a solo tom best. A 4th NMF
part for toms wasn't retried (see the separate templates above). The
pretrained model replaced all of this.

Perc: a stricter pitched-mid test cut false perc on MDB snares from 195 to
as few as 35, but MDB perc F stayed at 0.02 to 0.03 while the cowbell's
recall and the synth snare fell. Kept as is.

## Pretrained drum model (ADTOF)

With `tastr[drums]` (PyTorch) installed, `adt.py` runs ADTOF's Frame_RNN
(Zehren et al., https://github.com/MZehren/ADTOF): two convolution blocks and
three bidirectional GRUs over a 12-bands-per-octave log spectrogram at 100
frames a second, with one output each for kick, snare, toms, hi-hat and
cymbals (crash and ride). The released weights (`Frame_RNN_adtofAll_0`) are
trained on ADTOF-RGW and ADTOF-YT only: about 360 hours of commercial music
annotated from rhythm-game charts. None of our benchmark sets (MDB Drums,
IDMT-SMT-Drums, StemGMD, ENST) is in its training data, so its scores below
are held out. A MedleyDB song could in principle appear in a game chart;
that can't be checked track by track, and is unlikely.

**License.** ADTOF's code and weights are CC BY-NC-SA 4.0: non-commercial,
share-alike. Tastr doesn't include them. The first analysis downloads the
two checkpoint files (about 5 MB) from a pinned commit of the ADTOF
repository into `$TASTR_HOME/models/`, checks their SHA-256, and reads the
TensorFlow checkpoint without TensorFlow (`tf_checkpoint.py`). The network,
the madmom-style spectrogram and the peak picking in `adt.py` are written
from ADTOF's published architecture and settings, not copied.

**What it replaces.** The model supplies the kick, hat and tom lanes and
adds a cymbal lane. Each hit is placed on the analysis frames with its
confidence (ADTOF's per-drum threshold maps to 0.5) times the level of the
built-in part for that family, so the groove's "20% of the loudest lane"
rule still compares like with like. Snare, clap, perc and open hat stay
with the built-in detector (the model's hi-hats replace only the hits our
ringing test calls closed). Because of the license it is opt-in: it runs
once `tastr drum-model fetch` has cached the weights, or with
`TASTR_DRUM_MODEL=adtof`. Without PyTorch, with `TASTR_DRUM_MODEL=off`, or
offline before the first download, the built-in lanes are used and the
output is the same as before.

**Scores of the model's own onsets** (eval adapter: its peak picking; onset F within 50 ms
with the same labels and cymbal rule as the built-in lanes; built-in -> ADTOF;
"-" where ADTOF has no such drum). The shipped lanes go through the frame
mapping and family scaling above, so their scores differ a little; see the
next table for those:

| Suite | Kick | Snare | Hats | Open hat | Tom | Perc | Cymbal |
|---|---|---|---|---|---|---|---|
| MDB mixes | 0.81 -> 0.83 | 0.51 -> 0.74 | 0.65 -> 0.85 | 0.24 -> - | 0.01 -> 0.33 | 0.03 -> - | - -> 0.83 |
| MDB drum stems | 0.95 -> 0.96 | 0.60 -> 0.80 | 0.70 -> 0.87 | 0.30 -> - | 0.00 -> 0.32 | 0.00 -> - | - -> 0.87 |
| StemGMD test kits | 0.90 -> 0.96 | 0.46 -> 0.91 | 0.71 -> 0.74 | 0.52 -> - | 0.00 -> 0.89 | (cowbell) | - -> 0.86 |
| IDMT | 0.956 -> 0.970 | 0.935 -> 0.880 | 0.904 -> 0.946 | | | | |
| Synth, as onsets | 0.98 -> 0.97 | 0.86 -> 0.89 | 0.77 -> 0.86 | 0.51 -> - | 0.22 -> 0.00 | 0.54 -> - | |

MDB mixes are from `adt.py`; the other rows from the same weights through
the ADTOF-pytorch port, within about 0.02 of `adt.py` on MDB. On StemGMD
and synth the audio stops at 11 kHz (it is generated at 22.05 kHz), which
probably costs ADTOF some hats there. MDB toms are few (90) and ADTOF's
false toms there are mostly on snares (P 0.24). The synthetic tom isn't
recognised; it is a sine sweep.

**Through the shipped lanes** (`tastr drum-bench SUITE PATH --remote
[--drum-model]` on Modal, main d3934cc, 2026-10-30; onset F within 50 ms;
built-in -> with the model; "=" where the lane stays built-in):

| Suite | Kick | Snare | Hat | Hats (either) | Open hat | Tom | Perc | Cymbal |
|---|---|---|---|---|---|---|---|---|
| MDB mixes | 0.806 -> 0.834 | 0.506 = | 0.621 -> 0.776 | 0.649 -> 0.753 | 0.236 -> 0.209 | 0.013 -> 0.369 | 0.027 = | - -> 0.832 |
| MDB drum stems | 0.955 -> 0.965 | 0.601 = | 0.699 -> 0.831 | 0.705 -> 0.808 | 0.303 -> 0.289 | 0.000 -> 0.215 | 0.000 = | - -> 0.851 |
| StemGMD test kits | 0.901 -> 0.959 | 0.463 = | 0.665 -> 0.682 | 0.712 -> 0.751 | 0.520 -> 0.481 | 0.002 -> 0.880 | 0.664 = | - -> 0.848 |
| IDMT | 0.956 -> 0.977 | 0.935 = | 0.904 -> 0.940 | | | | | |
| Synth | 1.000 -> 0.977 | 0.750 = | 0.914 -> 0.943 | | 0.686 = | 0.667 -> 0.000 | 0.636 = | |

Kick, hats and toms improve on every real-audio suite. Toms are found but
not yet precise on MDB (P 0.25 on mixes, 0.13 on stems, R about 0.7; most
false toms land on snares, and MDB has only 90 toms). On StemGMD a tom
alone scores 0.98, a tom with a kick or snare 0.59. On tom-less kits (IDMT)
the tom lane fires 0.017 times per labeled hit (0.010 built-in). Open hat
drops a little (0.01 to 0.04), since our open-hat lane now keeps only the
hits the model doesn't also call a hat. Synth loses its synthetic tom, a
sine sweep the model doesn't recognise. Each run took 38 to 87 s on Modal
(4 CPUs). The earlier eval-adapter numbers for StemGMD and synth agreed
within about 0.01.

**Open hats.** Splitting the model's hi-hats into closed and open with the
built-in ringing test scored better on the StemGMD test kits (open hat 0.66
against 0.48) but worse on synth (0.42 against 0.49), and MDB couldn't be
checked. The built-in open-hat lane stays (`drums.MODEL_OPEN_HATS = False`);
revisit with a bench option that runs MDB and IDMT both ways on Modal.

**The snare trade-off.** ADTOF's snare is far better on MDB and StemGMD
(0.74 and 0.91 against 0.51 and 0.46) but worse on IDMT's sampled and
techno kits (0.88 against 0.94; 338 false snares on hi-hats). The built-in
snare is kept for now. Keeping ADTOF's snares only where the built-in snare
also has an onset (eval adapter) is precise but loses ADTOF's recall:
StemGMD test kits F 0.63 (P 0.98, R 0.46) against 0.46 built-in and 0.91
ADTOF; synth 0.82 against 0.86 and 0.89. MDB and IDMT still to measure.

**Speed.** About 0.6 s per minute of audio on one CPU thread once loaded,
against about 2.5 s for the built-in lanes in the bench.

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
