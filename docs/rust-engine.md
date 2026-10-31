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
| P1 | The other shared signals (mel, MFCC, RMS, onsets, filters, drum envelopes) | Next |
| P2 | CQT and chroma, beat tracker, tempogram, Ward clustering | |
| P3 | The stages, and cards from Rust | |
| P4 | Switch-over behind a flag, full benchmarks | |
| P5 | Retire the Python engine from the analysis path | |

## Layout

```
engine-rs/
  src/decode.rs      audio bytes -> mono f32 (symphonia: WAV, FLAC, MP3, AAC/M4A, Ogg Vorbis; not Opus)
  src/resample.rs    our own rational-ratio Kaiser-sinc resampler, shaped like soxr HQ
  src/spectral.rs    STFT, inverse STFT and HPSS, step by step as librosa 1.0 does them
  src/detmath.rs     sin, cos, pow, ... for the whole binary, from the pure-Rust libm crate
  src/lib.rs         signals(bytes); a small C interface for the WebAssembly build
  src/main.rs        `tastr-engine <file> [out dir]`: the native build as a command
  run-wasm.mjs       the same through the WebAssembly build, under Node
tests/test_rust_engine.py   native vs WebAssembly bit for bit, and HPSS vs the Python engine
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
- **Fixed order of operations.** No parallel sums; the summation order is the same in both builds.
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
