//! The constant-Q transform, librosa's tuning estimate and CQT chroma, following librosa 1.0 step by step:
//! `librosa.cqt` (a VQT with gamma 0: Hann-windowed complex wavelets, one octave at a time from the top, halving
//! the sample rate between octaves), `estimate_tuning` (parabolic peak picking on the STFT, then a histogram of
//! the peaks' offsets from the semitone grid) and `chroma_cqt` (seven octaves of 36 bins folded into 12).
//!
//! Precision follows numpy and scipy: the wavelets are built in float64 and stored as complex64, their FFT is
//! single precision, the sparse basis is applied in complex64, and the STFT under it runs in float64 and is
//! stored as complex64. Downsampling uses our resampler (shaped like soxr HQ), so the octaves below the top one
//! differ from librosa's slightly, in the top tenth of each halved band.

use rustfft::num_complex::Complex;
use rustfft::FftPlannerScalar;

use crate::features::Matrix;
use crate::np::pairwise_sum;
use crate::resample::resample;
use crate::spectral;

/// librosa.filters.window_bandwidth("hann").
const HANN_BANDWIDTH: f64 = 1.50018310546875;

/// librosa.note_to_hz("C1").
pub fn c1() -> f64 {
    440.0 * libm::pow(2.0, (24.0 - 69.0) / 12.0)
}

/// librosa.interval_frequencies(n_bins, fmin, "equal", bins_per_octave).
fn frequencies(n_bins: usize, bpo: usize, fmin: f64) -> Vec<f64> {
    let ratios: Vec<f64> = (0..bpo).map(|j| libm::pow(2.0, (0.0 + j as f64) / bpo as f64)).collect();
    let octaves = n_bins.div_ceil(bpo);
    let mut out: Vec<f64> =
        (0..octaves).flat_map(|o| ratios.iter().map(move |r| libm::pow(2.0, o as f64) * r)).take(n_bins).collect();
    out.sort_by(|a, b| a.total_cmp(b));
    out.iter().map(|r| r * fmin).collect()
}

/// librosa.filters._relative_bandwidth: each bin's bandwidth from the spacing of its neighbours.
fn relative_bandwidth(freqs: &[f64]) -> Vec<f64> {
    let n = freqs.len();
    let logf: Vec<f64> = freqs.iter().map(|&f| libm::log2(f)).collect();
    let bpo: Vec<f64> = (0..n)
        .map(|i| {
            if i == 0 {
                1.0 / (logf[1] - logf[0])
            } else if i == n - 1 {
                1.0 / (logf[n - 1] - logf[n - 2])
            } else {
                2.0 / (logf[i + 1] - logf[i - 1])
            }
        })
        .collect();
    bpo.iter()
        .map(|&b| {
            let p = libm::pow(2.0, 2.0 / b);
            (p - 1.0) / (p + 1.0)
        })
        .collect()
}

/// librosa.filters.wavelet_lengths with gamma 0 and filter_scale 1: (lengths, highest frequency reached).
fn wavelet_lengths(freqs: &[f64], alpha: &[f64], sr: f64) -> (Vec<f64>, f64) {
    let q: Vec<f64> = alpha.iter().map(|a| 1.0 / a).collect();
    let cutoff =
        freqs.iter().zip(&q).map(|(f, q)| f * (1.0 + 0.5 * HANN_BANDWIDTH / q) + 0.5 * 0.0).fold(f64::MIN, f64::max);
    let lengths = freqs.iter().zip(&q).map(|(f, q)| q * sr / (f + 0.0 / 1.0)).collect();
    (lengths, cutoff)
}

/// One octave's filters in the frequency domain: for each bin, its non-zero (FFT bin, weight) pairs.
type SparseBasis = Vec<Vec<(usize, Complex<f32>)>>;

/// librosa's __vqt_filter_fft for one octave at `sr`, its filters scaled by `rescale` (sqrt of the downsampling
/// so far). Returns the sparse basis and the FFT length.
fn octave_basis(sr: f64, freqs: &[f64], alpha: &[f64], rescale: f64) -> (SparseBasis, usize) {
    let (lengths, _) = wavelet_lengths(freqs, alpha, sr);
    let longest = lengths.iter().cloned().fold(f64::MIN, f64::max);
    let n_fft = libm::pow(2.0, libm::ceil(libm::log2(longest))) as usize;
    let fft = FftPlannerScalar::<f32>::new().plan_fft_forward(n_fft);
    let mut basis = Vec::with_capacity(freqs.len());
    let mut buf = vec![Complex::new(0.0f32, 0.0); n_fft];
    for (&ilen, &freq) in lengths.iter().zip(freqs) {
        // filters.wavelet: a phasor over arange(-ilen // 2, ilen // 2), a periodic Hann window, L1-normalised.
        let start = libm::floor(-ilen / 2.0);
        let n = (libm::floor(ilen / 2.0) - start) as usize;
        let win = spectral::hann(n);
        let sig: Vec<Complex<f64>> = (0..n)
            .map(|k| {
                let angle = (start + k as f64) * 2.0 * core::f64::consts::PI * freq / sr;
                let (s, c) = (libm::sin(angle), libm::cos(angle));
                Complex::new(c * win[k], s * win[k])
            })
            .collect();
        let mags: Vec<f64> = sig.iter().map(|c| libm::hypot(c.re, c.im)).collect();
        let mut norm = pairwise_sum(&mags);
        if norm < f64::MIN_POSITIVE {
            norm = 1.0;
        }
        // Stored as complex64, centred in n_fft, then scaled by length / n_fft (in float64, rounded again).
        let scale = ilen / n_fft as f64;
        let lpad = (n_fft - n) / 2;
        buf.iter_mut().for_each(|c| *c = Complex::new(0.0, 0.0));
        for (k, c) in sig.iter().enumerate() {
            let (re, im) = ((c.re / norm) as f32, (c.im / norm) as f32);
            buf[lpad + k] = Complex::new((re as f64 * scale) as f32, (im as f64 * scale) as f32);
        }
        fft.process(&mut buf);
        let row = &buf[..n_fft / 2 + 1];
        // util.sparsify_rows(quantile=0.01): drop the smallest weights that together make up 1% of the row's sum.
        let mags: Vec<f32> = row.iter().map(|c| libm::hypotf(c.re, c.im)).collect();
        let total = pairwise_sum(&mags);
        let mut sorted = mags.clone();
        sorted.sort_by(|a, b| a.total_cmp(b));
        let mut cum = 0.0f32;
        let mut idx = 0;
        for (i, &m) in sorted.iter().enumerate() {
            cum += m / total;
            if cum >= 0.01 {
                idx = i;
                break;
            }
        }
        let thresh = sorted[idx];
        basis.push(
            row.iter()
                .zip(&mags)
                .enumerate()
                .filter(|(_, (_, &m))| m >= thresh)
                .map(|(j, (c, _))| (j, Complex::new((c.re as f64 * rescale) as f32, (c.im as f64 * rescale) as f32)))
                .collect(),
        );
    }
    (basis, n_fft)
}

/// librosa.resample(y, orig_sr=from, target_sr=to, scale=True) with our resampler.
fn resample_scaled(y: &[f32], from: u32, to: u32) -> Vec<f32> {
    let gain = libm::sqrt(to as f64 / from as f64);
    resample(y, from, to).into_iter().map(|v| (v as f64 / gain) as f32).collect()
}

/// |librosa.cqt(y, sr, hop_length=hop, fmin, n_bins, bins_per_octave)|: n_bins x frames magnitudes, row-major.
pub fn cqt(y: &[f32], sr: f64, hop: usize, fmin: f64, n_bins: usize, bpo: usize) -> Matrix {
    let freqs = frequencies(n_bins, bpo, fmin);
    let alpha = relative_bandwidth(&freqs);
    let (_, cutoff) = wavelet_lengths(&freqs, &alpha, sr);
    let n_octaves = n_bins.div_ceil(bpo);
    let n_filters = bpo.min(n_bins);

    // __early_downsample: skip octaves of sample rate the filters never reach.
    let nyquist = sr / 2.0;
    let count1 = (libm::ceil(libm::log2(nyquist / cutoff)) as i64 - 1 - 1).max(0);
    let count2 = (hop.trailing_zeros() as i64 - n_octaves as i64 + 1).max(0);
    let count = count1.min(count2) as u32;
    let (mut my_y, mut my_sr, mut my_hop) = (y.to_vec(), sr, hop);
    if count > 0 {
        let factor = 1u32 << count;
        my_hop /= factor as usize;
        my_sr = sr / factor as f64;
        my_y = resample_scaled(&my_y, factor, 1);
    }
    let base_sr = my_sr;

    // Octave by octave from the top, each as complex responses scaled and turned into magnitudes.
    let (lengths, _) = wavelet_lengths(&freqs, &alpha, base_sr);
    let mut octaves: Vec<(usize, usize, Vec<f32>)> = Vec::with_capacity(n_octaves); // (first bin, frames, mags)
    let mut end = n_bins;
    for i in 0..n_octaves {
        let lo = end.saturating_sub(n_filters);
        let (basis, n_fft) = octave_basis(my_sr, &freqs[lo..end], &alpha[lo..end], libm::sqrt(base_sr / my_sr));
        let rows = end - lo;
        let ones = vec![1.0f64; n_fft];
        let mut data = Vec::new();
        let frames = spectral::stft_frames_window(&my_y, &ones, my_hop, |_, col| {
            for (r, row) in basis.iter().enumerate() {
                let mut acc = Complex::new(0.0f32, 0.0);
                for &(j, a) in row {
                    let x = col[j];
                    acc = Complex::new(acc.re + (a.re * x.re - a.im * x.im), acc.im + (a.re * x.im + a.im * x.re));
                }
                let s = libm::sqrt(lengths[lo + r]);
                let (re, im) = ((acc.re as f64 / s) as f32, (acc.im as f64 / s) as f32);
                data.push(libm::hypotf(re, im));
            }
        });
        octaves.push((lo, frames, transpose(&data, frames, rows)));
        end = lo;
        if i < n_octaves - 1 {
            let f_max_next = freqs[lo - 1];
            if my_hop.is_multiple_of(2) && f_max_next <= my_sr / 5.0 {
                my_hop /= 2;
                my_sr /= 2.0;
                my_y = resample_scaled(&my_y, 2, 1);
            }
        }
    }
    // __trim_stack: every octave cut to the shortest one's frame count.
    let cols = octaves.iter().map(|o| o.1).min().unwrap_or(0);
    let mut out = vec![0.0f32; n_bins * cols];
    for (lo, frames, mags) in &octaves {
        let rows = mags.len() / frames;
        for r in 0..rows {
            out[(lo + r) * cols..(lo + r + 1) * cols].copy_from_slice(&mags[r * frames..r * frames + cols]);
        }
    }
    Matrix { rows: n_bins, cols, data: out }
}

/// frames x rows (by frame) -> rows x frames.
fn transpose(data: &[f32], frames: usize, rows: usize) -> Vec<f32> {
    let mut out = vec![0.0f32; data.len()];
    for f in 0..frames {
        for r in 0..rows {
            out[r * frames + f] = data[f * rows + r];
        }
    }
    out
}

/// librosa.estimate_tuning(y, sr, bins_per_octave=bpo): the most common offset of the STFT's spectral peaks
/// (150 Hz to 4 kHz, above a tenth of each frame's peak) from the semitone grid, in fractions of a bin.
pub fn estimate_tuning(y: &[f32], sr: f64, bpo: usize) -> f64 {
    let n_fft = spectral::N_FFT;
    let bins = n_fft / 2 + 1;
    let val = 1.0 / (n_fft as f64 * (1.0 / sr)); // np.fft.rfftfreq
    let in_range: Vec<bool> = (0..bins).map(|b| (150.0..4000.0f64.min(sr / 2.0)).contains(&(b as f64 * val))).collect();
    let (mut pitches, mut mags) = (Vec::new(), Vec::new());
    let mut s = vec![0.0f32; bins];
    let mut masked = vec![0.0f32; bins];
    spectral::stft_frames(y, n_fft, spectral::HOP, |_, col| {
        for (v, c) in s.iter_mut().zip(col) {
            *v = libm::hypotf(c.re, c.im);
        }
        let reference = 0.1f32 * s.iter().cloned().fold(f32::NEG_INFINITY, f32::max);
        for (m, &v) in masked.iter_mut().zip(&s) {
            *m = if v > reference { v } else { 0.0 };
        }
        for b in 1..bins - 1 {
            // util.localmax of the thresholded spectrum (interior bins; the edges are outside 150 Hz..4 kHz).
            if !in_range[b] || !(masked[b] > masked[b - 1] && masked[b] >= masked[b + 1]) {
                continue;
            }
            // _parabolic_interpolation (numba promotes to float64) and np.gradient (float32).
            let a = (s[b + 1] + s[b - 1]) as f64 - 2.0 * s[b] as f64;
            let half = (s[b + 1] - s[b - 1]) as f64 / 2.0;
            let shift = if half.abs() >= a.abs() { 0.0f32 } else { (-half / a) as f32 };
            let avg = (s[b + 1] - s[b - 1]) / 2.0;
            pitches.push(((b as f64 + shift as f64) * (sr / n_fft as f64)) as f32);
            mags.push(s[b] + 0.5 * avg * shift);
        }
    });
    let threshold = if mags.is_empty() {
        0.0
    } else {
        let mut sorted = mags.clone();
        sorted.sort_by(|a, b| a.total_cmp(b));
        let m = sorted.len();
        if m % 2 == 1 {
            sorted[m / 2]
        } else {
            (sorted[m / 2 - 1] + sorted[m / 2]) / 2.0
        }
    };
    let chosen: Vec<f32> =
        pitches.iter().zip(&mags).filter(|(&p, &m)| m >= threshold && p > 0.0).map(|(&p, _)| p).collect();
    pitch_tuning(&chosen, bpo)
}

/// librosa.pitch_tuning(frequencies, resolution=0.01, bins_per_octave): the histogram peak of the offsets.
fn pitch_tuning(freqs: &[f32], bpo: usize) -> f64 {
    if freqs.is_empty() {
        return 0.0;
    }
    let n_bins = 100;
    let edges: Vec<f64> =
        (0..=n_bins).map(|i| if i == n_bins { 0.5 } else { i as f64 * (1.0 / 100.0) + -0.5 }).collect();
    let mut counts = vec![0usize; n_bins];
    for &f in freqs {
        let octs = libm::log2f(f / 27.5f32); // hz_to_octs: log2(f / (440 / 16)) in float32
        let mut r = libm::fmodf(bpo as f32 * octs, 1.0);
        if r >= 0.5 {
            r -= 1.0;
        }
        let r = r as f64;
        if !(edges[0]..=edges[n_bins]).contains(&r) {
            continue;
        }
        let mut i = ((r - edges[0]) * (n_bins as f64 / (edges[n_bins] - edges[0]))) as usize;
        i = i.min(n_bins - 1);
        if r < edges[i] {
            i -= 1;
        } else if i + 1 < n_bins && r >= edges[i + 1] {
            i += 1;
        }
        counts[i] += 1;
    }
    let best = counts.iter().enumerate().fold((0, 0), |acc, (i, &c)| if c > acc.1 { (i, c) } else { acc });
    edges[best.0]
}

/// librosa.feature.chroma_cqt(y, sr, hop_length): the tuning estimate, a 7-octave CQT with 36 bins an octave
/// from C1, folded into 12 pitch classes (each takes its bin and the two either side), each frame scaled so its
/// largest value is 1.
pub fn chroma_cqt(y: &[f32], sr: f64, hop: usize) -> Matrix {
    let bpo = 36;
    let tuning = estimate_tuning(y, sr, bpo);
    let fmin = c1() * libm::pow(2.0, tuning / bpo as f64);
    let c = cqt(y, sr, hop, fmin, 7 * bpo, bpo);
    let t = c.cols;
    let mut chroma = vec![0.0f32; 12 * t];
    for (p, out) in chroma.chunks_exact_mut(t).enumerate() {
        for o in 0..7 {
            for d in [-1i64, 0, 1] {
                let row = c.row(o * bpo + (3 * p as i64 + d).rem_euclid(bpo as i64) as usize);
                for (v, &x) in out.iter_mut().zip(row) {
                    *v += x;
                }
            }
        }
    }
    for f in 0..t {
        let peak = (0..12).map(|p| chroma[p * t + f]).fold(f32::NEG_INFINITY, f32::max);
        if peak >= f32::MIN_POSITIVE {
            for p in 0..12 {
                chroma[p * t + f] /= peak;
            }
        }
    }
    Matrix { rows: 12, cols: t, data: chroma }
}

/// signals.bass_cqt: |CQT| of the harmonic part, 36 bins from C1 (12 an octave), no tuning correction.
pub fn bass_cqt(y: &[f32], sr: f64, hop: usize) -> Matrix {
    cqt(y, sr, hop, c1(), 36, 12)
}
