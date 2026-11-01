# The Rust engine

A rewrite of the analysis engine in Rust (`engine-rs/`), so the server and the browser run the same code and
get the same numbers. The plan, its phases and the decisions behind it are in the plan doc (2026-10-31):
one crate built two ways (a native library for the server, WebAssembly for the page), phases P0 to P5 with a
gate at the end of each, and the Python engine kept as the reference until P5.

Decided so far: the Python engine stays as the reference until P5; existing cards are left alone until
someone re-analyzes them; Opus files, which symphonia can't decode, fall back to the server.

## Status

| Phase | What | State |
|---|---|---|
| P0 | Decode, resample, STFT and HPSS; native and WebAssembly bit-identical | Done (below) |
| P1 | The other shared signals (mel, MFCC, RMS, onsets, filters, drum envelopes); speed and memory | Done (below) |
| P2 | CQT and chroma, beat tracker, tempogram, Ward clustering | Done (below) |
| P3 | The stages, and cards from Rust | Done (below) |
| P4 | Switch-over behind a flag, full benchmarks | Done (below) |
| P5 | Retire the Python engine from the analysis path | Done (below) |

## Layout

```
engine-rs/
  src/decode.rs      audio bytes -> mono f32 (symphonia: WAV, FLAC, MP3, AAC/M4A, Ogg Vorbis; not Opus), with
                     the M4A edit list honoured (symphonia 0.5 ignores it), and MP3s whose ID3v2 tag has a
                     compressed frame read past the tag (symphonia 0.5 refuses them; 8 of FMA Keys' 695)
  src/resample.rs    our own rational-ratio Kaiser-sinc resampler, shaped like soxr HQ
  src/spectral.rs    STFT, inverse STFT and HPSS, step by step as librosa 1.0 does them
  src/features.rs    mel filter banks, power_to_db, MFCC, RMS, onset strength, spectral centroid, the kick- and
                     air-band flux, log mel, and drums.py's band envelopes (Butterworth + sosfiltfilt)
  src/cqt.rs         librosa's CQT (wavelet filters, one octave at a time), tuning estimate and CQT chroma; the
                     bass CQT
  src/rhythm.rs      the onset tempogram, librosa's tempo estimate, its dynamic-programming beat tracker and
                     onset peak picking
  src/segment.rs     librosa.segment.agglomerative: Ward clustering where only neighbouring bars may merge
  src/stages/        the card stages, one file each as in tastr/engine/stages/: core, sections, tags,
                     suggestions, views; mod.rs holds the shared signals (Ctx) and runs them as run_pipeline does
  src/drums.rs       drums.py's lanes: semi-adaptive NMF, unmixing, kick/tom, snare/clap/perc and closed/open
                     hat; with the optional ADTOF drum model, the hits the server hands in (set_model)
  src/music.rs       music.py and calibration.py: key model, modes, chord matching, roman numerals
  src/card.rs        the card, written as the same JSON as TrackCard.to_dict(); src/json.rs is its writer
  src/consts.rs      the key model's weights and the calibration tables, copied from Python (a test checks)
  src/np.rs          numpy's pairwise float sums, round-half-even, percentiles and friends, where results
                     depend on them
  src/detmath.rs     sin, cos, pow, ... for the whole binary, from the pure-Rust libm crate
  src/lib.rs         load(bytes), signals(y), analyze(y) and card_json(y); a small C interface for the
                     WebAssembly build (tastr_signals, tastr_card)
  src/main.rs        `tastr-engine <file> [out dir]`: the native build as a command; writes every output as
                     <name>.f32 plus manifest.json ({name: [rows, cols]}). `rhythm`, `chroma` and `ward`
                     subcommands run one step on a .f32 input, to compare it with librosa on the same input;
                     `card <file> <out.json> [beats per bar]` writes the whole card
  run-wasm.mjs       the same through the WebAssembly build, under Node
  .cargo/config.toml WebAssembly SIMD on (same bits, faster)
tests/test_rust_engine.py   native vs WebAssembly bit for bit; every signal, beats, chroma and Ward vs librosa;
                            cards vs the Python engine; the copied constants
tastr/engine/rust.py        the native library through ctypes (tastr_card, tastr_card_f32), STAGES, TASTR_ENGINE, and
                            where the WebAssembly build is and its id
tastr/engine/browser_card.py  checks a card the browser made (TASTR_BROWSER_ENGINE)
tastr/web/static/engine.js  the worker that runs the WebAssembly card in the page
tastr/cloud/rust_image.py   builds the native library into the Modal images
tests/test_rust_switch.py   the switch, the fallbacks, browser card checks, the upload path
```

Build both and run the tests:

```
cd engine-rs
cargo build --release
rustup target add wasm32-unknown-unknown
cargo build --release --lib --target wasm32-unknown-unknown
cd .. && python -m pytest tests/test_rust_engine.py
```

CI builds both only when `engine-rs/` or its test changes (about 50 s from clean, less with the cache);
otherwise the test skips.

## How the two builds stay identical

- **One implementation of every maths function.** Rust's `f64::sin()` calls the C library: glibc on a Linux
  server, a Rust port of musl's maths in WebAssembly. They differ in the last bit now and then. symphonia's
  MP3, AAC and Vorbis decoders build tables with them, and the first P0 run had WAV and FLAC identical but MP3
  off by one rounding step in half the samples. `detmath.rs` now defines `sin`, `cos`, `pow`, `sincos` and the
  rest for the whole binary from the `libm` crate, so every caller, dependencies included, uses one version.
  The test checks the native build imports none of them from the system.
- **No run-time SIMD choice.** symphonia is built without its SIMD features, rustfft through its scalar
  planner, and the resampler is ours, because resampling crates pick AVX code on a server and none in a
  browser, and those round differently.
- **Fixed order of operations.** No parallel sums; the summation order is the same in both builds. Where a
  sum is worth vectorising (the resampler's dot product), it is written as eight explicit running sums added
  in a set order, so SIMD (on with `+simd128` for WebAssembly, SSE2 natively) can't change the result.
- **Checked, not assumed.** The test decodes WAV, MP3, AAC, Vorbis and FLAC through both builds and compares
  the bytes of the audio, harmonic and percussive parts.

## What P0 measured (2026-10-31)

On a 4-CPU container, with synthetic tracks (a 124 BPM loop plus a noise floor, 44.1 and 48 kHz stereo),
Node 22 for WebAssembly:

- **Bit-identical:** WAV (16-bit, float), FLAC, MP3, AAC (M4A) and Ogg Vorbis, at 22.05, 44.1 and 48 kHz:
  every sample of the audio and both HPSS parts. Opus is refused ("unsupported codec"), as planned.
- **HPSS against the Python engine** on the same input audio: within 2e-7 of the peak (about 140 dB SNR),
  the same agreement as the browser's JavaScript HPSS.
- **Decoding and resampling against librosa.load:** same length and alignment for WAV, FLAC, MP3 and Vorbis;
  76 dB SNR overall, -110 dB or better below 10 kHz. The differences sit in the top kilohertz, where our
  filter and soxr's roll off differently. A first filter cut off at 95% had 54 dB; shaping it like soxr HQ
  (flat to 91.3% of Nyquist, 120 dB down at Nyquist) brought it to 76 dB.
- **AAC:** symphonia trims less of the encoder's padding than ffmpeg does, so our audio is 512 samples
  (23 ms) later and 512 longer than librosa's. To fix in P1 (honour the M4A's edit list).
- **The card:** analyzing a 63 s MP3 with the Rust audio and harmonic part (through the browser-signals path,
  so int16 on the way) gave the same BPM, key, energy, loudness, downbeats, sections and tags as the Python
  engine; one feature moved (swing confidence 0.764 to 0.766).
- **Speed, 3-minute track:** native 3.0 s single-threaded (decode 0.1 s, resample 1.1 s, HPSS 1.9 s), the
  Python engine about 3.4 s (load 0.4 s, HPSS 3.0 s). WebAssembly in Node 7.8 s. Our resampler is the slow
  part (about 360 taps an output sample against soxr's FFT method); then the median filters and the two
  inverse STFTs. All of it can go faster in P1 without changing results: the medians and STFT frames are
  independent of each other, so they can run on several threads without changing a bit.
- **Size:** the `.wasm` is 1.5 MB, 0.58 MB gzipped, before any size tuning.
- **Memory:** about 385 MB at peak for 3 minutes, native or WebAssembly, the same as the Python engine
  (about 130 MB a minute). The spike keeps whole spectrograms and several copies; streaming the STFT through
  the medians should cut this a lot, and that is what could lift the 12-minute upload cap.

The P0 gate (identical builds, measured speed, size and memory) is met. Speed and memory are no better than
Python yet, which P1 needs to change before the browser path is worth turning on.

## What P1 measured (2026-10-31)

Same machine and tracks as P0.

- **Every shared signal against the Python engine**, on the same audio: onset envelope, RMS, MFCC, spectral
  centroid, the 96-band percussive mel, kick-band and air-band flux, and the 128-band log mel all within
  5e-7 of their peak (float32 rounding: only summation order and the maths library differ). The drum band
  envelopes (dB) within 0.001 dB wherever the band is louder than -80 dB; near digital silence (-135 dB)
  HPSS's 1e-7 differences become up to 0.5 dB, which the burst and ring measures never look at.
- **Native vs WebAssembly:** every output bit-identical on WAV (16-bit and float), FLAC, MP3, AAC and Vorbis,
  at 22.05, 44.1 and 48 kHz, with WebAssembly SIMD on.
- **AAC:** decoding now reads the M4A's edit list (skip 1,024 priming samples, then play the stated length),
  so the audio lines up with the WAV it was encoded from to the sample and ends within 10 samples of it.
  librosa (through ffmpeg) keeps the padding at the end, so AAC tracks are now slightly shorter than the
  Python engine's.
- **The card:** analyzing the 63 s MP3 and AAC test tracks from the Rust audio and harmonic part gave the same
  BPM, key, downbeats, sections and tags; brightness moved by 0.002 on the AAC and swing confidence by 0.002.
- **Speed, 3-minute MP3, everything above:** native 2.9 s (decode and resample 0.5 s, signals 2.4 s), single
  threaded; WebAssembly in Node 4.9 s. The Python engine takes 5.3 s for the same signals (load 0.6 s, the
  signals 4.7 s), and its process peaked at 770 MB. The resampler went from 1.1 s to 0.4 s with the fixed-order lanes;
  WebAssembly SIMD took the browser build from 6.5 s to 4.9 s.
- **Memory, 3 minutes, everything above:** 200 MB at peak natively, 207 MB in WebAssembly (P0 was 385 MB for
  HPSS alone). HPSS now runs frame by frame after its time medians, inverse STFTs included, and the
  percussive part's mel spectrograms are made without keeping its STFT. That is about 67 MB a minute against
  the Python engine's 130, so the 12-minute upload cap could roughly double once the Rust engine is in use.
- **Size:** 1.7 MB `.wasm`, 0.64 MB gzipped.

Still to do for speed (none of it changes results): the median filters and STFT frames are independent of
each other and could run on several threads natively; the time medians could stream too, which would bring
memory down to a few spectrogram columns plus the audio.

## What P2 measured (2026-10-31)

Same machine as P0 and P1, plus 30 real tracks: a random sample of the Beatport EDM Key previews (Zenodo
1101082, read straight out of the zip with ranged requests, about 44 MB; not kept).

- **Tempo, beats and onsets:** given the same onset envelope, librosa's tempo, beat frames (with and without a
  given BPM, as core's 1.5x re-track uses) and onset frames come out identical on the synthetic tracks and on 12
  real ones; the 384-lag tempogram mean is within 3e-8. Getting there meant copying numpy's and numba's
  precision choices exactly: the onset normalisation uses numpy's float32 pairwise sums, the beat tracker's
  local score accumulates in float32 and its dynamic program in float64, and peak picking takes numba's float32
  mean and compares in float64. librosa's local score also skips one term at the window's end (its loop bound
  stops one short); ours does too.
- **Ward clustering:** the same section boundaries as `librosa.segment.agglomerative` on 300 random cases,
  including exact repeats, which test the tie-breaking (scikit-learn's heap order of (cost, i, j)).
- **Tuning, chroma and the bass CQT:** the same tuning estimate, chroma within 1e-4 (7e-6 on a real track), the
  bass CQT within 1e-4 of its peak in the top octave and 7e-4 in the lowest. What's left is the resampler:
  every octave below the top one is computed from audio halved (or, for the bass CQT, cut 16 times) by our
  soxr-shaped filter instead of soxr itself. The two differ only near each halved band's top, where no filter
  of the next octave reaches, so it shows up as small level differences, not different notes.
- **The cards:** all 30 tracks analyzed by the Python engine with librosa's chroma, bass CQT, tempo, beats,
  onsets and Ward swapped for the Rust steps gave exactly the same cards as the Python engine alone: every BPM,
  key, confidence, downbeat, section, tag and feature. So on this sample, key (MIREX 0.537, 24 scored tracks),
  tempo (15 with a Beatport BPM, 80% within 4%) and sections are unchanged, not merely within a point.
- **Speed, 3-minute track:** tuning, chroma and the bass CQT 1.24 s against librosa's 2.0 s; tempo, beats and
  onsets 0.2 s against 0.35 s. Everything so far (decode through chroma and beats) takes 3.9 s natively,
  single-threaded, against about 7.6 s for the same work in the Python engine; 8.1 s in WebAssembly under Node.
  Most of the CQT's time is resampling (our filter computes every tap; soxr uses FFTs), the first place to
  speed up, and it can do so without changing a bit.
- **Memory:** unchanged at about 200 MB at peak for 3 minutes; the CQT keeps only magnitudes.
- **Native vs WebAssembly:** every output, the new ones included, bit-identical on WAV, FLAC, MP3, AAC and
  Vorbis. Ward isn't in the WebAssembly outputs yet (it needs the bar features, which come in P3); it uses only
  float64 add, subtract, multiply and divide, which IEEE 754 makes identical everywhere.

How the card check ran: a scratch script replaced `librosa.feature.chroma_cqt`, `librosa.cqt` (the bass call),
`librosa.beat.beat_track`, `librosa.feature.tempogram`, `librosa.onset.onset_detect` and
`librosa.segment.agglomerative` with calls to the binary's step commands, then ran `tastr bench beatport-key`,
`tastr bench beatport-tempo` and `analyze_file` on the 30 tracks, with and without the swap. P3 moves the stages
themselves into Rust; the full benchmarks run at P4, on Modal as usual.

## What P3 measured (2026-10-31)

Same machine and the same 30 Beatport previews as P2, plus the synthetic tracks.

- **The cards, same audio in:** each track decoded once by the Python engine, then the identical samples
  analyzed by `run_pipeline` and by `tastr-engine card`. On all 30 tracks and on synthetic tracks at 128 and
  174 BPM the two cards are equal in every field: BPM, key, confidences, beats, downbeats, meter, sections and
  their letters and labels, tags and every vibe score, features, every suggestion's title, body and data
  (drum patterns and velocities, bass loops and per-section beats, chord loops), the energy series and the
  waveform. The one field that isn't byte-equal by design is the spectrogram PNG (deflated with miniz_oxide,
  not zlib); decoded, its pixels match except for 1 or 2 of 144,240 on 8 tracks, one 4-level step where the
  4096-point FFT's last bit tips a rounding.
- **The one difference found:** on a synthetic 90 BPM track, one beat lands a frame apart, which moves
  `swing_confidence` from 0.209 to 0.218. The track's tempo is exact in frames, so the regularised grid falls
  on x.5 frames and rounds either way depending on the last bit of the line fit (numpy's `polyfit` solves it
  with LAPACK's SVD, ours in closed form). Real tracks don't sit on exact halves; none of the 30 did.
- **The measured shift, Rust decoding too:** analyzing the MP3s end to end in Rust (our decoder and
  soxr-shaped resampler instead of librosa's) against the Python cards: BPM, key, meter and bass loops agree on
  30 of 30, scale and first downbeats on 29, sections on 28 (one boundary moved by 2 bars, one section's label
  changed between verse and breakdown), tags on 28, chord loops on 27, every drum lane's pattern on 20, and
  energy to the third decimal on 16 (the others off by 0.001 or 0.002). The drum patterns are the most
  sensitive: a step whose strength sits on the 0.5 or 0.72 cut-off flips between `.`, `o` and `x`.
- **Native vs WebAssembly:** the card JSON is byte-identical (WAV, MP3, FLAC, and the 30 tracks spot-checked).
- **Speed and memory, 3-minute MP3, whole card:** native 6.3 s single-threaded (decode 1.2 s, analysis
  5.1 s) against 7.3 s for the Python engine; WebAssembly under Node 10.0 s, with 207 MB of memory at the end
  (77 MB for a 1-minute track). The NMF behind the drum lanes is about 1 s of the native time.
- **The ADTOF drum model** (optional, server only, needs PyTorch) isn't ported: the server runs it on the file
  (`engine/__init__.py` `_drum_model_hits`) and passes its hits to `tastr_card_hits` / `tastr_card_f32_hits`.
  Rust then puts them into the kick, tom and closed-hat lanes and adds the cymbal lane, as `drums.model_lanes`
  does (`tests/test_rust_engine.py` checks the two cards match). The browser never runs it.
  `TASTR_METER` isn't read: the caller passes the meter (`beats_per_bar`), as the browser will.

How it was matched: the same precision choices as in P2, carried through the stages (numpy's float32 means
and pairwise sums where the Python arrays are float32, its float32 percentile, which keeps the index in
float64 and interpolates in float32, axis-0 means added row by row, Python's `round` to a decimal via the
shortest correctly rounded string). The NMF's matrix products are summed in a plain order rather than BLAS's,
which changes nothing visible after its float32 activations. P4 puts the Rust card behind a flag and runs the
full benchmarks on Modal.

## P4: the switch (2026-10-31)

Two flags, both off by default:

- **`TASTR_ENGINE=rust`** (server and Modal; or `options["engine"]`): `analyze_file` hands the whole analysis to
  the native library, which decodes the file and makes the card. The card says `extras["engine"] = "rust
  0.4.0"`. It falls back to Python, saying why in `extras["engine_fallback"]`, when the library isn't there,
  for a subset of stages (re-analysis of one part), with browser signals, with the drum model, and when the
  library can't decode the file (Opus). The Modal images build the library with Rust 1.97.0 in one step and
  drop the toolchain (`tastr/cloud/rust_image.py`); the server passes its `TASTR_ENGINE` to Modal with each job.
- **`TASTR_BROWSER_ENGINE=1`** (Fly): the page runs the WebAssembly build on the upload and sends the card; the
  server stores it after the checks in `browser_card.py` and skips Modal. The Docker image builds the wasm in a
  Rust stage and serves it at `/engine/tastr_engine.wasm` (cached forever; the URL carries its hash).

Benchmarks: `tastr bench <name> --engine rust` (locally or `--remote` on Modal) runs every stage in Rust,
decoding the file itself; synthetic audio goes in as samples. The downbeat and Harmonix section benches work
from spectrograms rather than audio, so they keep the Python path; P3 showed those stages are identical given
the same input.

What the full benchmarks measured (2026-10-31, `modal run tastr/cloud/modal_app.py --kind bench ... --engine rust`
from the P4 branch, every stage in Rust including decoding; no failed tracks):

| Bench | Tracks | Python | Rust |
|---|---|---|---|
| `giantsteps-key`, exact key | 599 | 0.599 | 0.606 (MIREX weighted 0.682) |
| `beatport-key`, exact key | 1,275 | 0.491 | 0.491 (MIREX weighted 0.587) |
| `beatport-tempo`, Acc1 / Acc2 | 774 | 0.897 / 0.930 | 0.897 / 0.930 |
| `giantsteps-tempo`, Acc1 / Acc2 | 43 | 0.744 / 0.907 | 0.744 / 0.907 |

Tempo is unchanged and key moves within the decoder's noise (GiantSteps up 4 tracks), so no retuning was
needed. Key errors on GiantSteps: 66 fifths, 27 parallel, 23 relative, 120 other; the fifths stay the biggest
miss. Wall time on 8 CPUs: 6 min for GiantSteps key, 13 min for Beatport key, 6 min for Beatport tempo. The
downbeat and Harmonix section benches start from spectrograms, not audio, so they don't exercise the switch;
P3 showed those stages give identical results on the same input.

## P5: Rust makes every card (2026-10-31)

`analyze_file` now hands every analysis to the Rust engine; `TASTR_ENGINE` defaults to `rust`, on your own
machine, the Fly server and Modal alike.

- **Formats Rust can't decode** (Opus): Python decodes the file (librosa, else ffmpeg) and Rust makes the card
  from those samples (`tastr_card_f32`). The card says `extras["decoded_by"] = "python"`.
- **The Python engine** (`tastr/engine/stages/`, `run_pipeline`) stays as the reference: the benchmarks score
  it with `--engine python`, and the spectrogram benches (downbeats, Harmonix sections), the fitting scripts and
  the stage tests call its stage code directly. Since TAS-47 (2026-11-01) it never makes an app card:
  `analyze_file` has no fallback and no stage subset (`tastr analyze --stages` is gone), `TASTR_ENGINE` only
  picks the benchmarks' engine, and without the library analysis fails with "the Rust engine library isn't
  built (cd engine-rs && cargo build --release)". `rust.STAGES` lists the stages and versions every Rust card
  runs, for servers without the native library (browser cards are checked against it) and `tastr stages`.
  Tests that analyze through the app are marked `rust` and skip without the library (`tests/conftest.py`).
  A change to a stage still goes in both engines while the benchmarks use Python, and `tests/test_rust_engine.py`
  checks that their cards agree.
- **Browser signals are gone** (`TASTR_BROWSER_SIGNALS`, `signals.js`, `browser_signals.py`): they fed HPSS into
  the Python engine; the browser card (`TASTR_BROWSER_ENGINE`) replaces them. The upload's form field for the
  card is now `card`.
- **Memory**, peak RSS of the whole process for a synthetic track, 2026-10-31:

  | Length | Rust | Python |
  |---|---|---|
  | 3 min | 235 MB, 4.2 s | ~740 MB |
  | 10 min | 699 MB, 18 s | 1,551 MB, 37 s |
  | 20 min | 1,362 MB, 40 s | ~2.9 GB |

  About 66 MB per minute against Python's 130, so Modal's 2 GB `analyze` function could take tracks of about 25
  minutes. On the deployed function, 12 to 30-minute MP3s all analyzed (24 to 54 s), so `TASTR_MAX_MINUTES` is now
  25.
- **Local installs** need the library built once (`cd engine-rs && cargo build --release --lib`); without it
  analysis fails and says how to build it.

