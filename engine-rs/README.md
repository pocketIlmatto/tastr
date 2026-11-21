# tastr-engine

The music analysis engine behind [Tastr](https://github.com/pocketIlmatto/tastr), in pure Rust. It finds the
BPM, beats, downbeats, meter, key and song sections of a track (and, for Tastr's own cards, drum patterns,
vibe tags and production suggestions). It builds natively and for WebAssembly with the same results, and
needs no network: everything runs on the caller's machine.

## Use it from Rust

It's on [crates.io](https://crates.io/crates/tastr-engine). The API may change before 1.0, so each 0.x minor
version can break it:

```toml
[dependencies]
tastr-engine = "0.6"
# If you decode audio yourself (ffmpeg, a DAW), leave out the built-in decoder:
# tastr-engine = { version = "0.6", default-features = false }
```

Each release is also a tag `engine-v<version>` in the repository, so a git dependency pinned to one works too:
`tastr-engine = { git = "https://github.com/pocketIlmatto/tastr", tag = "engine-v0.6.0" }`. Needs Rust 1.88 or
newer.

```rust
use tastr_engine::dj::{analyze_pcm, DjOptions};

// Interleaved f32 PCM in -1..1, any sample rate and channel count.
let r = analyze_pcm(&samples, 44_100, 2, &DjOptions::default())?;
println!("{:?} BPM ({:?} likely right)", r.bpm, r.bpm_likely_correct);
if let Some(k) = &r.key {
    println!("key {} ({})", k.camelot, k.mode);
}
if let Some(g) = &r.grid {
    // A DJ beat grid: tempo and first beat to a fraction of a millisecond, one anchor per tempo.
    println!("grid {} BPM from {:.4} s, first downbeat {:?}, {} anchor(s)", g.bpm, g.first_beat, g.first_downbeat, g.anchors.len());
}
```

With the default `decode` feature, `tastr_engine::dj::analyze_file(bytes, &opts)` takes WAV, FLAC, MP3,
AAC/M4A or Ogg Vorbis file bytes instead. `DjOptions::default().with_beats_per_bar(3)` sets the meter instead of
detecting it.

`beat_times` are the beat tracker's, on the engine's analysis frames (about 23 ms steps). For a DJ grid use
`grid`: each tracked beat moved onto its attack at sample resolution and a line fitted through them, with a
new anchor where the tempo changes. More on what
the engine measures, and how well, is in Tastr's
[engine report](https://github.com/pocketIlmatto/tastr/blob/main/docs/engine-report.md) and
[docs/rust-engine.md](https://github.com/pocketIlmatto/tastr/blob/main/docs/rust-engine.md#using-the-engine-as-a-library).

## From the command line

Download the program for macOS (Apple silicon) or Linux (x86-64, ARM) from the repository's
[releases](https://github.com/pocketIlmatto/tastr/releases) (tags `engine-v*`), check it against `SHA256SUMS`,
unpack it and run:

```sh
./tastr-engine dj song.mp3 > song.json
```

Or build it yourself with `cargo build --release` (the program is `target/release/tastr-engine`). On macOS a
downloaded program may need `xattr -d com.apple.quarantine tastr-engine` before the first run, since it isn't
signed.

## Licences

The code is MIT ([LICENSE](https://github.com/pocketIlmatto/tastr/blob/main/LICENSE)). Read
[NOTICE](NOTICE) before you ship it: the key model's weights are CC BY-SA 4.0, and the optional decoder
(Symphonia) is MPL-2.0.
