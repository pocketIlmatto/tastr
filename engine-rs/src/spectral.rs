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
    pub bins: usize,
    pub data: Vec<Complex<f32>>,
}

fn hann(n: usize) -> Vec<f64> {
    // scipy.signal.get_window("hann", N, fftbins=True): 0.5 - 0.5 cos(2 pi k / N)
    (0..n).map(|k| 0.5 - 0.5 * libm::cos(2.0 * core::f64::consts::PI * k as f64 / n as f64)).collect()
}

/// librosa.stft(y) with the defaults: n_fft 2048, hop 512.
pub fn stft(y: &[f32]) -> Spectrogram {
    stft_with(y, N_FFT, HOP)
}

/// librosa.stft(y, n_fft=n_fft, hop_length=hop): centred frames, zero padding, periodic Hann window.
pub fn stft_with(y: &[f32], n_fft: usize, hop: usize) -> Spectrogram {
    let bins = n_fft / 2 + 1;
    let mut data = Vec::with_capacity((1 + y.len() / hop) * bins);
    let frames = stft_frames(y, n_fft, hop, |_, col| data.extend_from_slice(col));
    Spectrogram { frames, bins, data }
}

/// The same STFT handed over one frame at a time, `each(frame, bins)`, without keeping it. Returns the frame count.
pub fn stft_frames(y: &[f32], n_fft: usize, hop: usize, mut each: impl FnMut(usize, &[Complex<f32>])) -> usize {
    let win = hann(n_fft);
    let bins = n_fft / 2 + 1;
    let frames = 1 + y.len() / hop;
    let fft = FftPlannerScalar::<f64>::new().plan_fft_forward(n_fft);
    let mut buf = vec![Complex::new(0.0f64, 0.0); n_fft];
    let mut scratch = vec![Complex::new(0.0f64, 0.0); fft.get_inplace_scratch_len()];
    let mut col = vec![Complex::new(0.0f32, 0.0); bins];
    let pad = (n_fft / 2) as isize;
    for f in 0..frames {
        let start = (f * hop) as isize - pad;
        for (k, c) in buf.iter_mut().enumerate() {
            let i = start + k as isize;
            let v = if i >= 0 && (i as usize) < y.len() { y[i as usize] as f64 } else { 0.0 };
            *c = Complex::new(win[k] * v, 0.0);
        }
        fft.process_with_scratch(&mut buf, &mut scratch);
        for (o, c) in col.iter_mut().zip(&buf[..bins]) {
            *o = Complex::new(c.re as f32, c.im as f32);
        }
        each(f, &col);
    }
    frames
}

/// |D| ** power, as librosa's _spectrogram does in float32 (hypot, then square for power 2).
pub fn magnitude(spec: &Spectrogram, power: u32) -> Vec<f32> {
    spec.data
        .iter()
        .map(|c| {
            let m = libm::hypotf(c.re, c.im);
            if power == 2 {
                m * m
            } else {
                m
            }
        })
        .collect()
}

/// librosa.istft(D, length=len) for a centred STFT with the defaults above, fed one frame at a time so the
/// whole spectrogram never has to be held.
pub struct Istft {
    len: usize,
    frames: usize,
    win: Vec<f64>,
    fft: std::sync::Arc<dyn rustfft::Fft<f32>>,
    buf: Vec<Complex<f32>>,
    scratch: Vec<Complex<f32>>,
    acc: Vec<f32>,
}

impl Istft {
    /// For a signal of `len` samples whose STFT has `frames` frames.
    pub fn new(len: usize, frames: usize) -> Self {
        let padded = len + 2 * (N_FFT / 2);
        let frames = frames.min(padded.div_ceil(HOP));
        let fft = FftPlannerScalar::<f32>::new().plan_fft_inverse(N_FFT);
        let scratch = vec![Complex::new(0.0f32, 0.0); fft.get_inplace_scratch_len()];
        // Overlap-add over the padded timeline; the first n_fft/2 samples are dropped at the end.
        let total = N_FFT + HOP * (frames.max(1) - 1);
        Istft {
            len,
            frames,
            win: hann(N_FFT),
            fft,
            buf: vec![Complex::new(0.0f32, 0.0); N_FFT],
            scratch,
            acc: vec![0.0f32; total.max(len + N_FFT / 2)],
        }
    }

    /// Adds frame `f` (BINS values); frames past the ones librosa uses are ignored.
    #[allow(clippy::needless_range_loop)]
    pub fn push(&mut self, f: usize, col: &[Complex<f32>]) {
        if f >= self.frames {
            return;
        }
        let buf = &mut self.buf;
        // Hermitian spectrum -> real signal (scipy.fft.irfft ignores the imaginary part of DC and Nyquist).
        buf[0] = Complex::new(col[0].re, 0.0);
        buf[N_FFT / 2] = Complex::new(col[BINS - 1].re, 0.0);
        for k in 1..N_FFT / 2 {
            buf[k] = col[k];
            buf[N_FFT - k] = col[k].conj();
        }
        self.fft.process_with_scratch(buf, &mut self.scratch);
        let scale = 1.0f32 / N_FFT as f32;
        let off = f * HOP;
        for k in 0..N_FFT {
            let v = self.win[k] * (buf[k].re * scale) as f64;
            self.acc[off + k] = (self.acc[off + k] as f64 + v) as f32;
        }
    }

    /// The signal, divided by the summed squared window where that isn't ~0.
    #[allow(clippy::needless_range_loop)]
    pub fn finish(self) -> Vec<f32> {
        let mut wss = vec![0.0f32; self.acc.len()];
        for f in 0..self.frames {
            for k in 0..N_FFT {
                let i = f * HOP + k;
                wss[i] = (wss[i] as f64 + self.win[k] * self.win[k]) as f32;
            }
        }
        let tiny = f32::MIN_POSITIVE;
        (0..self.len)
            .map(|i| {
                let j = i + N_FFT / 2;
                if wss[j] > tiny {
                    self.acc[j] / wss[j]
                } else {
                    self.acc[j]
                }
            })
            .collect()
    }
}

pub fn istft(spec: &Spectrogram, len: usize) -> Vec<f32> {
    let mut out = Istft::new(len, spec.frames);
    for f in 0..spec.frames {
        out.push(f, &spec.data[f * spec.bins..(f + 1) * spec.bins]);
    }
    out.finish()
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
    let mag = magnitude(&d, 1);
    hpss_from(&d, &mag, y.len())
}

/// HPSS from the default STFT of a `len`-sample signal and its magnitude (librosa.magphase's).
pub fn hpss_from(d: &Spectrogram, mag: &[f32], len: usize) -> (Vec<f32>, Vec<f32>) {
    let frames = d.frames;
    // Harmonic medians run along time, per frequency bin (each bin copied out first: storage is by frame).
    let mut harm = vec![0.0f32; mag.len()];
    let (mut col, mut out) = (vec![0.0f32; frames], vec![0.0f32; frames]);
    for b in 0..BINS {
        for f in 0..frames {
            col[f] = mag[f * BINS + b];
        }
        median_line(frames, |f| col[f], |f, v| out[f] = v);
        for f in 0..frames {
            harm[f * BINS + b] = out[f];
        }
    }
    // Then frame by frame: the percussive medians along frequency, both soft masks, and both inverse STFTs.
    let (mut h_out, mut p_out) = (Istft::new(len, frames), Istft::new(len, frames));
    let mut perc = vec![0.0f32; BINS];
    let (mut h_col, mut p_col) = (vec![Complex::new(0.0f32, 0.0); BINS], vec![Complex::new(0.0f32, 0.0); BINS]);
    for f in 0..frames {
        let row = &mag[f * BINS..(f + 1) * BINS];
        median_line(BINS, |b| row[b], |b, v| perc[b] = v);
        for b in 0..BINS {
            let (i, c, m) = (f * BINS + b, d.data[f * BINS + b], row[b]);
            // librosa.magphase's phase: D / |D|, and 1 where |D| is 0.
            let z = if m == 0.0 { 1.0f32 } else { 0.0 };
            let nz = m + z;
            let phase = Complex::new(c.re / nz + z, c.im / nz);
            let sh = m * softmask(harm[i], perc[b]);
            let sp = m * softmask(perc[b], harm[i]);
            h_col[b] = Complex::new(sh * phase.re, sh * phase.im);
            p_col[b] = Complex::new(sp * phase.re, sp * phase.im);
        }
        h_out.push(f, &h_col);
        p_out.push(f, &p_col);
    }
    (h_out.finish(), p_out.finish())
}
