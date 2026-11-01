//! Tastr's analysis engine in Rust (docs/rust-engine.md): decoding, resampling, the shared signals and every
//! card stage (core, sections, tags, suggestions, views), built both natively and for WebAssembly, so the two
//! builds can be checked for identical output.

pub mod card;
pub mod consts;
pub mod cqt;
pub mod decode;
mod detmath;
pub mod drums;
pub mod features;
pub mod json;
pub mod music;
pub mod np;
pub mod resample;
pub mod rhythm;
pub mod segment;
pub mod spectral;
pub mod stages;

pub use features::Matrix;

pub const SR: u32 = 22050;

/// The crate version, so callers can tell which engine made a card.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Mono audio at 22,050 Hz from file bytes.
pub fn load(bytes: Vec<u8>) -> Result<Vec<f32>, String> {
    let d = decode::decode(bytes)?;
    Ok(resample::resample(&d.samples, d.sr, SR))
}

/// The whole card for the audio, as the Python engine's run_pipeline makes it. `beats_per_bar` (3 or 4) sets
/// the meter instead of detecting it. `drum_hits` are the optional drum model's hits (the server runs it).
pub fn analyze(y: Vec<f32>, beats_per_bar: Option<i64>, drum_hits: Option<drums::ModelHits>) -> card::Card {
    let mut ctx = stages::Ctx::new(y, beats_per_bar);
    ctx.drum_hits = drum_hits;
    stages::run(&mut ctx)
}

/// The card as TrackCard.to_dict() JSON; "source" is null (the caller knows the file).
pub fn card_json(y: Vec<f32>, beats_per_bar: Option<i64>, drum_hits: Option<drums::ModelHits>) -> String {
    analyze(y, beats_per_bar, drum_hits).to_json(json::Json::Null).to_string()
}

/// Drum model hits from a flat buffer: for each MODEL_LANES lane, its count n, then n onset seconds, then n
/// confidences. None for an empty buffer; an error if it is cut short.
pub fn parse_drum_hits(buf: &[f64]) -> Result<Option<drums::ModelHits>, String> {
    if buf.is_empty() {
        return Ok(None);
    }
    let mut hits: drums::ModelHits = Default::default();
    let mut i = 0;
    for lane in hits.iter_mut() {
        let n = *buf.get(i).ok_or("drum hits cut short")? as usize;
        let end = i + 1 + 2 * n;
        if end > buf.len() {
            return Err("drum hits cut short".into());
        }
        lane.0 = buf[i + 1..i + 1 + n].to_vec();
        lane.1 = buf[i + 1 + n..end].iter().map(|&c| c as f32).collect();
        i = end;
    }
    Ok(Some(hits))
}

/// The audio, its HPSS parts and every shared signal, by name (signals.py's names where it has one).
pub fn signals(y: Vec<f32>) -> Vec<(&'static str, Matrix)> {
    let spec = spectral::stft(&y);
    let mag = spectral::magnitude(&spec, 1);
    let (harmonic, percussive) = spectral::hpss_from(&spec, &mag, y.len());
    let frames = spec.frames;
    drop(spec);
    let mut out = features::all(&y, &mag, frames, &percussive, SR as f64);
    let onset = out.iter().find(|(n, _)| *n == "onset_envelope").map(|(_, m)| m.data.clone()).unwrap_or_default();
    out.extend(rhythm_outputs(&onset, None));
    out.extend(chroma_outputs(&harmonic));
    out.splice(0..0, [("y", row(y)), ("harmonic", row(harmonic)), ("percussive", row(percussive))]);
    out
}

fn row(data: Vec<f32>) -> Matrix {
    Matrix { rows: 1, cols: data.len(), data }
}

fn frames_row(frames: &[usize]) -> Matrix {
    row(frames.iter().map(|&f| f as f32).collect())
}

/// What the core stage takes from the onset envelope: the 384-lag tempogram's mean (its periodicity check),
/// librosa's tempo and beats, the beats at `bpm` if given, and the onset frames. Frame numbers are stored as
/// floats (exact below 2^24).
pub fn rhythm_outputs(onset: &[f32], bpm: Option<f64>) -> Vec<(&'static str, Matrix)> {
    let (sr, hop) = (SR as f64, spectral::HOP);
    let n = onset.len();
    let tg = rhythm::tempogram(onset, 384);
    let mean = rhythm::tempogram_mean(&tg, 384, n);
    drop(tg);
    let (tempo, beats) = rhythm::beat_track(onset, sr, hop, None);
    let mut out = vec![
        ("tempogram_mean", row(mean.iter().map(|&v| v as f32).collect())),
        ("tempo", row(vec![tempo as f32])),
        ("beats", frames_row(&beats)),
        ("onsets", frames_row(&rhythm::onset_detect(onset, sr, hop))),
    ];
    if let Some(bpm) = bpm {
        out.push(("beats_at_bpm", frames_row(&rhythm::beat_track(onset, sr, hop, Some(bpm)).1)));
    }
    out
}

/// The harmonic part's tuning (in fractions of a 36-per-octave bin), its 12 x frames chroma and its
/// 36 x frames bass CQT.
pub fn chroma_outputs(harmonic: &[f32]) -> Vec<(&'static str, Matrix)> {
    let (sr, hop) = (SR as f64, spectral::HOP);
    vec![
        ("tuning", row(vec![cqt::estimate_tuning(harmonic, sr, 36) as f32])),
        ("chroma", cqt::chroma_cqt(harmonic, sr, hop)),
        ("bass_cqt", cqt::bass_cqt(harmonic, sr, hop)),
    ]
}

/// FNV-1a over the bits of each array: equal hashes mean bit-identical output.
pub fn fingerprint<'a>(arrays: impl IntoIterator<Item = &'a [f32]>) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for a in arrays {
        for v in a.iter() {
            for byte in v.to_bits().to_le_bytes() {
                h ^= byte as u64;
                h = h.wrapping_mul(0x100000001b3);
            }
        }
    }
    h
}

/// A small C interface for the WebAssembly build: the page copies the file into memory from `tastr_alloc`,
/// calls `tastr_signals`, then reads each output through `tastr_out_name` / `_rows` / `_ptr` / `_len`.
pub mod ffi {
    use super::*;
    use std::cell::RefCell;

    thread_local! {
        static OUT: RefCell<Vec<(&'static str, Matrix)>> = const { RefCell::new(Vec::new()) };
        static CARD: RefCell<String> = const { RefCell::new(String::new()) };
    }

    /// Decodes the `len` bytes at `ptr` and analyses them into a card. `beats_per_bar` is 3 or 4 to set the
    /// meter, 0 to detect it. Returns the card JSON's length in bytes (read it from `tastr_card_ptr`). If the
    /// audio could not be decoded it returns -(n + 1) instead, with the decoder's n-byte message at
    /// `tastr_card_ptr`.
    ///
    /// # Safety
    /// `ptr` must come from `tastr_alloc(len)` and hold `len` written bytes; it is freed here.
    #[no_mangle]
    pub unsafe extern "C" fn tastr_card(ptr: *mut u8, len: usize, beats_per_bar: i32) -> i32 {
        unsafe { tastr_card_hits(ptr, len, beats_per_bar, std::ptr::null(), 0) }
    }

    /// tastr_card with the optional drum model's hits: `hits_n` float64 values at `hits` (the caller keeps
    /// them), laid out as `parse_drum_hits` reads them; `hits_n` 0 for none. Malformed hits are an error, like
    /// audio that can't be decoded.
    ///
    /// # Safety
    /// As tastr_card; `hits` must point at `hits_n` readable float64 values (or `hits_n` be 0).
    #[no_mangle]
    pub unsafe extern "C" fn tastr_card_hits(
        ptr: *mut u8,
        len: usize,
        beats_per_bar: i32,
        hits: *const f64,
        hits_n: usize,
    ) -> i32 {
        let bytes = unsafe { Vec::from_raw_parts(ptr, len, len) };
        CARD.with(|c| c.borrow_mut().clear());
        let parsed = unsafe { read_hits(hits, hits_n) };
        match parsed.and_then(|h| load(bytes).map(|y| (y, h))) {
            Ok((y, h)) => finish(card_json(y, meter(beats_per_bar), h)),
            Err(e) => {
                let n = e.len() as i32;
                CARD.with(|c| *c.borrow_mut() = e);
                -(n + 1)
            }
        }
    }

    /// The same from audio already decoded: `n` mono float32 samples at 22,050 Hz at `ptr`, which must come
    /// from `tastr_alloc(4 * n)` and is freed here. The server and the benchmarks use it to analyze audio they
    /// decoded themselves.
    ///
    /// # Safety
    /// `ptr` must come from `tastr_alloc(4 * n)` and hold `n` written floats.
    #[no_mangle]
    pub unsafe extern "C" fn tastr_card_f32(ptr: *mut u8, n: usize, beats_per_bar: i32) -> i32 {
        unsafe { tastr_card_f32_hits(ptr, n, beats_per_bar, std::ptr::null(), 0) }
    }

    /// tastr_card_f32 with the drum model's hits, as in tastr_card_hits.
    ///
    /// # Safety
    /// As tastr_card_f32 and tastr_card_hits.
    #[no_mangle]
    pub unsafe extern "C" fn tastr_card_f32_hits(
        ptr: *mut u8,
        n: usize,
        beats_per_bar: i32,
        hits: *const f64,
        hits_n: usize,
    ) -> i32 {
        let bytes = unsafe { Vec::from_raw_parts(ptr, 4 * n, 4 * n) };
        let y: Vec<f32> = bytes.as_chunks::<4>().0.iter().map(|b| f32::from_le_bytes(*b)).collect();
        drop(bytes);
        match unsafe { read_hits(hits, hits_n) } {
            Ok(h) => finish(card_json(y, meter(beats_per_bar), h)),
            Err(e) => {
                let len = e.len() as i32;
                CARD.with(|c| *c.borrow_mut() = e);
                -(len + 1)
            }
        }
    }

    fn meter(beats_per_bar: i32) -> Option<i64> {
        if beats_per_bar > 0 {
            Some(beats_per_bar as i64)
        } else {
            None
        }
    }

    fn finish(s: String) -> i32 {
        let n = s.len() as i32;
        CARD.with(|c| *c.borrow_mut() = s);
        n
    }

    /// # Safety
    /// `hits` must point at `n` readable float64 values, or `n` be 0.
    unsafe fn read_hits(hits: *const f64, n: usize) -> Result<Option<drums::ModelHits>, String> {
        if n == 0 || hits.is_null() {
            return Ok(None);
        }
        parse_drum_hits(unsafe { std::slice::from_raw_parts(hits, n) })
    }

    /// The crate version (engine-rs/Cargo.toml), as UTF-8 at the returned pointer; its length from
    /// `tastr_version_len`.
    #[no_mangle]
    pub extern "C" fn tastr_version() -> *const u8 {
        VERSION.as_ptr()
    }

    #[no_mangle]
    pub extern "C" fn tastr_version_len() -> usize {
        VERSION.len()
    }

    #[no_mangle]
    pub extern "C" fn tastr_card_ptr() -> *const u8 {
        CARD.with(|c| c.borrow().as_ptr())
    }

    #[no_mangle]
    pub extern "C" fn tastr_alloc(len: usize) -> *mut u8 {
        let mut v = Vec::<u8>::with_capacity(len);
        let p = v.as_mut_ptr();
        std::mem::forget(v);
        p
    }

    /// Decodes the `len` bytes at `ptr` and computes every signal. Returns how many outputs there are, or -1 if
    /// the audio could not be decoded.
    ///
    /// # Safety
    /// `ptr` must come from `tastr_alloc(len)` and hold `len` written bytes; it is freed here.
    #[no_mangle]
    pub unsafe extern "C" fn tastr_signals(ptr: *mut u8, len: usize) -> i32 {
        let bytes = unsafe { Vec::from_raw_parts(ptr, len, len) };
        OUT.with(|o| o.borrow_mut().clear());
        match load(bytes) {
            Ok(y) => {
                let out = signals(y);
                let n = out.len() as i32;
                OUT.with(|o| *o.borrow_mut() = out);
                n
            }
            Err(e) => {
                let n = e.len() as i32;
                CARD.with(|c| *c.borrow_mut() = e);
                -(n + 1)
            }
        }
    }

    fn with<T>(i: usize, f: impl Fn(&(&'static str, Matrix)) -> T, empty: T) -> T {
        OUT.with(|o| o.borrow().get(i).map_or(empty, f))
    }

    #[no_mangle]
    pub extern "C" fn tastr_out_name(i: usize) -> *const u8 {
        with(i, |(n, _)| n.as_ptr(), std::ptr::null())
    }

    #[no_mangle]
    pub extern "C" fn tastr_out_name_len(i: usize) -> usize {
        with(i, |(n, _)| n.len(), 0)
    }

    #[no_mangle]
    pub extern "C" fn tastr_out_rows(i: usize) -> usize {
        with(i, |(_, m)| m.rows, 0)
    }

    #[no_mangle]
    pub extern "C" fn tastr_out_ptr(i: usize) -> *const f32 {
        with(i, |(_, m)| m.data.as_ptr(), std::ptr::null())
    }

    #[no_mangle]
    pub extern "C" fn tastr_out_len(i: usize) -> usize {
        with(i, |(_, m)| m.data.len(), 0)
    }
}
