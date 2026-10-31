//! STFT, inverse STFT and HPSS, following librosa 1.0's defaults step by step (n_fft 2048, hop 512, periodic
//! Hann window, centred frames with zero padding, kernel 31, soft masks with power 2).
//!
//! Precision follows what numpy does in librosa: the forward FFT runs in f64 (librosa multiplies the float32
//! frames by a float64 window) and is stored as f32; masks are f32; the inverse FFT is f32 and overlap-adds into
//! an f32 signal.

use rustfft::num_complex::Complex;
use rustfft::FftPlannerScalar;

pub const N_FFT: usize = 2048;
pub const HOP: usize = 512;
pub const BINS: usize = N_FFT / 2 + 1;
const KERNEL: usize = 31;

/// Frames x bins, row-major by frame.
pub struct Spectrogram {
    pub frames: usize,
    pub data: Vec<Complex<f32>>,
}

fn hann() -> Vec<f64> {
    // scipy.signal.get_window("hann", N, fftbins=True): 0.5 - 0.5 cos(2 pi k / N)
    (0..N_FFT).map(|k| 0.5 - 0.5 * libm::cos(2.0 * core::f64::consts::PI * k as f64 / N_FFT as f64)).collect()
}

pub fn n_frames(len: usize) -> usize {
    1 + len / HOP
}

pub fn stft(y: &[f32]) -> Spectrogram {
    let win = hann();
    let frames = n_frames(y.len());
    let fft = FftPlannerScalar::<f64>::new().plan_fft_forward(N_FFT);
    let mut buf = vec![Complex::new(0.0f64, 0.0); N_FFT];
    let mut scratch = vec![Complex::new(0.0f64, 0.0); fft.get_inplace_scratch_len()];
    let mut data = Vec::with_capacity(frames * BINS);
    let pad = (N_FFT / 2) as isize;
    for f in 0..frames {
        let start = (f * HOP) as isize - pad;
        for (k, c) in buf.iter_mut().enumerate() {
            let i = start + k as isize;
            let v = if i >= 0 && (i as usize) < y.len() { y[i as usize] as f64 } else { 0.0 };
            *c = Complex::new(win[k] * v, 0.0);
        }
        fft.process_with_scratch(&mut buf, &mut scratch);
        data.extend(buf[..BINS].iter().map(|c| Complex::new(c.re as f32, c.im as f32)));
    }
    Spectrogram { frames, data }
}

/// librosa.istft(D, length=len) for a centred STFT with the defaults above.
pub fn istft(spec: &Spectrogram, len: usize) -> Vec<f32> {
    let win = hann();
    let padded = len + 2 * (N_FFT / 2);
    let frames = spec.frames.min(padded.div_ceil(HOP));
    let fft = FftPlannerScalar::<f32>::new().plan_fft_inverse(N_FFT);
    let mut buf = vec![Complex::new(0.0f32, 0.0); N_FFT];
    let mut scratch = vec![Complex::new(0.0f32, 0.0); fft.get_inplace_scratch_len()];
    // Overlap-add over the padded timeline, then drop the first n_fft/2 samples.
    let total = N_FFT + HOP * (frames.max(1) - 1);
    let mut acc = vec![0.0f32; total.max(len + N_FFT / 2)];
    let mut wss = vec![0.0f32; acc.len()];
    let scale = 1.0f32 / N_FFT as f32;
    for f in 0..frames {
        let col = &spec.data[f * BINS..(f + 1) * BINS];
        // Hermitian spectrum -> real signal (scipy.fft.irfft ignores the imaginary part of DC and Nyquist).
        buf[0] = Complex::new(col[0].re, 0.0);
        buf[N_FFT / 2] = Complex::new(col[BINS - 1].re, 0.0);
        for k in 1..N_FFT / 2 {
            buf[k] = col[k];
            buf[N_FFT - k] = col[k].conj();
        }
        fft.process_with_scratch(&mut buf, &mut scratch);
        let off = f * HOP;
        for k in 0..N_FFT {
            let v = win[k] * (buf[k].re * scale) as f64;
            acc[off + k] = (acc[off + k] as f64 + v) as f32;
            wss[off + k] = (wss[off + k] as f64 + win[k] * win[k]) as f32;
        }
    }
    let tiny = f32::MIN_POSITIVE;
    (0..len)
        .map(|i| {
            let j = i + N_FFT / 2;
            if wss[j] > tiny {
                acc[j] / wss[j]
            } else {
                acc[j]
            }
        })
        .collect()
}

/// scipy.ndimage "reflect" index: (d c b a | a b c d | d c b a), periodic with period 2n.
fn reflect(i: isize, n: usize) -> usize {
    let n = n as isize;
    let p = 2 * n;
    let mut m = i.rem_euclid(p);
    if m >= n {
        m = p - 1 - m;
    }
    m as usize
}

/// Running median of `KERNEL` values over a line of `n` (read through `get`), written by `put`. A sorted window
/// slides along: each step the outgoing value's slot is refilled by the incoming one, which is then shifted into
/// place. Medians only select values, so the result is exactly scipy's.
fn median_line(n: usize, get: impl Fn(usize) -> f32, mut put: impl FnMut(usize, f32)) {
    let h = (KERNEL / 2) as isize;
    let mut win = [0.0f32; KERNEL];
    for (slot, d) in win.iter_mut().zip(-h..=h) {
        *slot = get(reflect(d, n));
    }
    win.sort_by(|a, b| a.total_cmp(b));
    put(0, win[KERNEL / 2]);
    for i in 1..n {
        let out = get(reflect(i as isize - 1 - h, n));
        let inn = get(reflect(i as isize + h, n));
        let mut k = win.iter().position(|v| v.to_bits() == out.to_bits()).expect("outgoing value is in the window");
        win[k] = inn;
        while k > 0 && win[k - 1].total_cmp(&win[k]).is_gt() {
            win.swap(k - 1, k);
            k -= 1;
        }
        while k + 1 < KERNEL && win[k + 1].total_cmp(&win[k]).is_lt() {
            win.swap(k + 1, k);
            k += 1;
        }
        put(i, win[KERNEL / 2]);
    }
}

fn softmask(x: f32, x_ref: f32) -> f32 {
    let z = x.max(x_ref);
    if z < f32::MIN_POSITIVE {
        return 0.5; // split_zeros
    }
    let (a, b) = (x / z, x_ref / z);
    let (m, r) = (a * a, b * b);
    m / (m + r)
}

/// (harmonic, percussive) audio, as tastr.engine.signals.hpss.
pub fn hpss(y: &[f32]) -> (Vec<f32>, Vec<f32>) {
    let d = stft(y);
    let frames = d.frames;
    // librosa.magphase: |D| (hypot) and D / |D|, with phase 1 where |D| is 0.
    let mag: Vec<f32> = d.data.iter().map(|c| libm::hypotf(c.re, c.im)).collect();
    let phase: Vec<Complex<f32>> = d
        .data
        .iter()
        .zip(&mag)
        .map(|(c, &m)| {
            let z = if m == 0.0 { 1.0f32 } else { 0.0 };
            let nz = m + z;
            Complex::new(c.re / nz + z, c.im / nz)
        })
        .collect();

    let mut harm = vec![0.0f32; mag.len()];
    let mut perc = vec![0.0f32; mag.len()];
    let (mut col, mut out) = (vec![0.0f32; frames], vec![0.0f32; frames]);
    for b in 0..BINS {
        // harmonic: along time, per frequency bin (copied out first: the spectrogram is stored frame by frame)
        for f in 0..frames {
            col[f] = mag[f * BINS + b];
        }
        median_line(frames, |f| col[f], |f, v| out[f] = v);
        for f in 0..frames {
            harm[f * BINS + b] = out[f];
        }
    }
    for f in 0..frames {
        // percussive: along frequency, per frame
        let row = &mag[f * BINS..(f + 1) * BINS];
        let out = &mut perc[f * BINS..(f + 1) * BINS];
        median_line(BINS, |b| row[b], |b, v| out[b] = v);
    }

    let part = |mask_of: &dyn Fn(usize) -> f32| -> Vec<f32> {
        let data = (0..mag.len())
            .map(|i| {
                let s = mag[i] * mask_of(i);
                Complex::new(s * phase[i].re, s * phase[i].im)
            })
            .collect();
        istft(&Spectrogram { frames, data }, y.len())
    };
    let h = part(&|i| softmask(harm[i], perc[i]));
    let p = part(&|i| softmask(perc[i], harm[i]));
    (h, p)
}
