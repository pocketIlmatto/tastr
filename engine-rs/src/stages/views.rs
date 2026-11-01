//! Picture data for the UI's Waveform and Spectrogram views (tastr/engine/stages/views.py).
//!
//! The PNG is deflated with miniz_oxide rather than zlib, so its bytes differ from the Python engine's; the
//! decoded pixels are the same.

use super::Ctx;
use crate::card::Card;
use crate::features::fft_frequencies;
use crate::json::Json;
use crate::np::round_half_even;
use crate::spectral;

const WAVE_BLOCK: usize = 256;
const SPEC_HOP: f64 = 0.1;
const SPEC_ROWS: usize = 120;
const SPEC_FMIN: f64 = 40.0;
const SPEC_FMAX: f64 = 10000.0;
const SPEC_RANGE_DB: f32 = 70.0;
const SPEC_NFFT: usize = 4096;

/// Per block of 256 samples, min then max as int8, scaled so the loudest peak is +-127.
pub fn waveform(y: &[f32]) -> Vec<i8> {
    if y.is_empty() {
        return Vec::new();
    }
    let n = y.len().div_ceil(WAVE_BLOCK);
    let last = y[y.len() - 1];
    let mut mm = Vec::with_capacity(2 * n);
    for b in 0..n {
        let (mut lo, mut hi) = (f32::INFINITY, f32::NEG_INFINITY);
        for i in b * WAVE_BLOCK..(b + 1) * WAVE_BLOCK {
            let v = if i < y.len() { y[i] } else { last };
            lo = lo.min(v);
            hi = hi.max(v);
        }
        mm.push(lo);
        mm.push(hi);
    }
    let peak = (mm.iter().fold(0.0f32, |m, v| m.max(v.abs())) as f64).max(1e-6);
    // mm / peak: float32 array over a Python float stays float32.
    let peak = peak as f32;
    mm.iter().map(|v| round_half_even((v / peak * 127.0) as f64).clamp(-127.0, 127.0) as i8).collect()
}

/// Per block of 256 samples (the waveform's blocks), how loud the lows, mids and highs are, for the UI's
/// frequency-colored waveform (GitHub #3). Two one-pole low-passes at 250 Hz and 2.5 kHz split the signal:
/// low = lp250, mid = lp2500 - lp250, high = x - lp2500. Each band's RMS per block is scaled to that band's
/// loudest block and stored as round(255 * sqrt(rms / peak)), so quiet blocks keep some resolution; the page
/// squares it back. Interleaved low, mid, high per block.
pub fn bands(y: &[f32], sr: f64) -> Vec<u8> {
    let n = y.len().div_ceil(WAVE_BLOCK);
    let k1 = 1.0 - libm::exp(-2.0 * std::f64::consts::PI * 250.0 / sr);
    let k2 = 1.0 - libm::exp(-2.0 * std::f64::consts::PI * 2500.0 / sr);
    let (mut f1, mut f2) = (0.0f64, 0.0f64);
    let mut rms = vec![0.0f64; 3 * n];
    for b in 0..n {
        let (a, e) = (b * WAVE_BLOCK, ((b + 1) * WAVE_BLOCK).min(y.len()));
        let mut s = [0.0f64; 3];
        for &v in &y[a..e] {
            let v = v as f64;
            f1 += k1 * (v - f1);
            f2 += k2 * (v - f2);
            s[0] += f1 * f1;
            s[1] += (f2 - f1) * (f2 - f1);
            s[2] += (v - f2) * (v - f2);
        }
        for k in 0..3 {
            rms[3 * b + k] = (s[k] / (e - a).max(1) as f64).sqrt();
        }
    }
    let mut peak = [1e-12f64; 3];
    for (i, v) in rms.iter().enumerate() {
        peak[i % 3] = peak[i % 3].max(*v);
    }
    rms.iter().enumerate().map(|(i, v)| (255.0 * (v / peak[i % 3]).sqrt()).round().clamp(0.0, 255.0) as u8).collect()
}

/// (rows, frames) row-major, row 0 = SPEC_FMIN. Each row takes the strongest FFT bin in its band.
pub fn spectrogram(y: &[f32], sr: f64) -> (Vec<u8>, usize) {
    let hop = (round_half_even(SPEC_HOP * sr) as usize).max(1);
    let freqs = fft_frequencies(sr, SPEC_NFFT);
    let edges: Vec<f64> =
        (0..=SPEC_ROWS).map(|i| SPEC_FMIN * libm::pow(SPEC_FMAX / SPEC_FMIN, i as f64 / SPEC_ROWS as f64)).collect();
    let search = |x: f64| freqs.partition_point(|&f| f < x);
    let bands: Vec<(usize, usize)> = (0..SPEC_ROWS)
        .map(|r| {
            let lo = search(edges[r]);
            (lo, (lo + 1).max(search(edges[r + 1])))
        })
        .collect();
    let frames_est = 1 + y.len() / hop;
    let mut rows = vec![0.0f32; SPEC_ROWS * frames_est];
    let frames = spectral::stft_frames(y, SPEC_NFFT, hop, |f, col| {
        for (r, &(a, b)) in bands.iter().enumerate() {
            let m = col[a..b].iter().map(|c| libm::hypotf(c.re, c.im)).fold(f32::NEG_INFINITY, f32::max);
            rows[r * frames_est + f] = m;
        }
    });
    let db: Vec<f32> = rows.iter().map(|v| 20.0f32 * libm::log10f(v + 1e-6f32)).collect();
    let top = db.iter().cloned().fold(f32::NEG_INFINITY, f32::max) - SPEC_RANGE_DB;
    let img = db
        .iter()
        .map(|v| {
            let scaled = (v - top) / SPEC_RANGE_DB;
            (round_half_even((scaled * 63.0) as f64).clamp(0.0, 63.0) * 4.0) as u8
        })
        .collect();
    (img, frames)
}

fn crc32(data: &[u8]) -> u32 {
    let mut c = 0xFFFF_FFFFu32;
    for &b in data {
        c ^= b as u32;
        for _ in 0..8 {
            c = if c & 1 != 0 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 };
        }
    }
    !c
}

/// An 8-bit grayscale PNG of `img` (height x width, row 0 at the top).
pub fn gray_png(img: &[u8], h: usize, w: usize) -> Vec<u8> {
    let mut raw = Vec::with_capacity(h * (w + 1));
    for r in 0..h {
        raw.push(0);
        raw.extend_from_slice(&img[r * w..(r + 1) * w]);
    }
    let mut out = b"\x89PNG\r\n\x1a\n".to_vec();
    let mut chunk = |kind: &[u8], data: &[u8]| {
        out.extend_from_slice(&(data.len() as u32).to_be_bytes());
        let mut body = kind.to_vec();
        body.extend_from_slice(data);
        out.extend_from_slice(&body);
        out.extend_from_slice(&crc32(&body).to_be_bytes());
    };
    let mut ihdr = Vec::new();
    ihdr.extend_from_slice(&(w as u32).to_be_bytes());
    ihdr.extend_from_slice(&(h as u32).to_be_bytes());
    ihdr.extend_from_slice(&[8, 0, 0, 0, 0]);
    chunk(b"IHDR", &ihdr);
    chunk(b"IDAT", &miniz_oxide::deflate::compress_to_vec_zlib(&raw, 9));
    chunk(b"IEND", b"");
    out
}

pub fn base64(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut s = String::with_capacity(data.len().div_ceil(3) * 4);
    for c in data.chunks(3) {
        let n = (c[0] as u32) << 16 | (*c.get(1).unwrap_or(&0) as u32) << 8 | *c.get(2).unwrap_or(&0) as u32;
        s.push(T[(n >> 18) as usize & 63] as char);
        s.push(T[(n >> 12) as usize & 63] as char);
        s.push(if c.len() > 1 { T[(n >> 6) as usize & 63] as char } else { '=' });
        s.push(if c.len() > 2 { T[n as usize & 63] as char } else { '=' });
    }
    s
}

pub fn run(ctx: &mut Ctx, card: &mut Card) -> Result<(), String> {
    let wave = waveform(&ctx.y);
    let wave_bytes: Vec<u8> = wave.iter().map(|&v| v as u8).collect();
    let band_bytes = bands(&ctx.y, ctx.sr);
    let (spec, frames) = spectrogram(&ctx.y, ctx.sr);
    // The PNG has fmax at the top: rows flipped.
    let flipped: Vec<u8> =
        (0..SPEC_ROWS).rev().flat_map(|r| spec[r * frames..(r + 1) * frames].iter().cloned()).collect();
    let png = gray_png(&flipped, SPEC_ROWS, frames);
    let sr = ctx.sr;
    let views = Json::obj()
        .with(
            "waveform",
            Json::obj()
                .with("sr", sr as i64)
                .with("block", WAVE_BLOCK)
                .with("data", base64(&wave_bytes))
                .with("bands", base64(&band_bytes)),
        )
        .with(
            "spectrogram",
            Json::obj()
                .with("hop", round_half_even(SPEC_HOP * sr) / sr)
                .with("fmin", SPEC_FMIN)
                .with("fmax", SPEC_FMAX)
                .with("rows", SPEC_ROWS)
                .with("frames", frames)
                .with("png", format!("data:image/png;base64,{}", base64(&png))),
        );
    card.extras.set("views", views);
    Ok(())
}
