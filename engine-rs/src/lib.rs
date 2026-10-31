//! Tastr's analysis engine in Rust (docs/rust-engine.md). Phase 0: decode, resample and HPSS, built both
//! natively and for WebAssembly, so the two builds can be checked for identical output.

pub mod decode;
mod detmath;
pub mod resample;
pub mod spectral;

pub const SR: u32 = 22050;

/// The engine's audio and its HPSS parts.
pub struct Signals {
    pub y: Vec<f32>,
    pub harmonic: Vec<f32>,
    pub percussive: Vec<f32>,
}

/// File bytes in: mono audio at 22,050 Hz, then HPSS.
pub fn signals(bytes: Vec<u8>) -> Result<Signals, String> {
    let d = decode::decode(bytes)?;
    let y = resample::resample(&d.samples, d.sr, SR);
    let (harmonic, percussive) = spectral::hpss(&y);
    Ok(Signals { y, harmonic, percussive })
}

/// FNV-1a over the bits of each array: equal hashes mean bit-identical output.
pub fn fingerprint(arrays: &[&[f32]]) -> u64 {
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
/// calls `tastr_signals`, then reads each output array through `tastr_out_ptr` / `tastr_out_len`.
pub mod ffi {
    use super::*;
    use std::cell::RefCell;

    thread_local! {
        static OUT: RefCell<Vec<Vec<f32>>> = const { RefCell::new(Vec::new()) };
    }

    #[no_mangle]
    pub extern "C" fn tastr_alloc(len: usize) -> *mut u8 {
        let mut v = Vec::<u8>::with_capacity(len);
        let p = v.as_mut_ptr();
        std::mem::forget(v);
        p
    }

    /// Takes ownership of the `len` bytes at `ptr` (from `tastr_alloc`). Returns 0 on success, -1 if the audio
    /// could not be decoded.
    ///
    /// # Safety
    /// `ptr` must come from `tastr_alloc(len)` and hold `len` written bytes; it is freed here.
    #[no_mangle]
    pub unsafe extern "C" fn tastr_signals(ptr: *mut u8, len: usize) -> i32 {
        let bytes = unsafe { Vec::from_raw_parts(ptr, len, len) };
        match signals(bytes) {
            Ok(s) => {
                OUT.with(|o| *o.borrow_mut() = vec![s.y, s.harmonic, s.percussive]);
                0
            }
            Err(_) => -1,
        }
    }

    #[no_mangle]
    pub extern "C" fn tastr_out_ptr(i: usize) -> *const f32 {
        OUT.with(|o| o.borrow().get(i).map_or(std::ptr::null(), |v| v.as_ptr()))
    }

    #[no_mangle]
    pub extern "C" fn tastr_out_len(i: usize) -> usize {
        OUT.with(|o| o.borrow().get(i).map_or(0, |v| v.len()))
    }
}
