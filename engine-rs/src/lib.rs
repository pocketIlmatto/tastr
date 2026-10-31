//! Tastr's analysis engine in Rust (docs/rust-engine.md). So far: decoding, resampling, HPSS and the shared
//! signals the stages read, chroma, tempo, beats and section clustering, built both natively and for WebAssembly, so the two builds can be checked for
//! identical output.

pub mod cqt;
pub mod decode;
mod detmath;
pub mod features;
pub mod np;
pub mod resample;
pub mod rhythm;
pub mod sections;
pub mod spectral;

pub use features::Matrix;

pub const SR: u32 = 22050;

/// Mono audio at 22,050 Hz from file bytes.
pub fn load(bytes: Vec<u8>) -> Result<Vec<f32>, String> {
    let d = decode::decode(bytes)?;
    Ok(resample::resample(&d.samples, d.sr, SR))
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
            Err(_) => -1,
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
