//! Rational-ratio windowed-sinc resampler.
//!
//! Our own rather than a crate's, because resampling crates pick SIMD code paths at run time (AVX on a server,
//! none in a browser), and those round differently. This one is plain scalar code with a fixed summation order
//! and maths from the `libm` crate, so a native build and a WebAssembly build give the same bits.
//!
//! The filter is a linear-phase Kaiser-windowed sinc shaped like soxr's "HQ" setting, which librosa.load uses:
//! flat to 91.3% of the lower Nyquist rate, about 120 dB down by Nyquist.

const PASS: f64 = 0.913; // passband edge, share of the lower Nyquist rate
const STOP: f64 = 1.0; // stopband edge
const ATTEN_DB: f64 = 120.0;

fn gcd(a: u64, b: u64) -> u64 {
    if b == 0 {
        a
    } else {
        gcd(b, a % b)
    }
}

fn bessel_i0(x: f64) -> f64 {
    let (mut sum, mut term, q) = (1.0f64, 1.0f64, x * x / 4.0);
    for k in 1..200 {
        term *= q / ((k * k) as f64);
        sum += term;
        if term < sum * 1e-17 {
            break;
        }
    }
    sum
}

pub fn resample(x: &[f32], from: u32, to: u32) -> Vec<f32> {
    if from == to {
        return x.to_vec();
    }
    let g = gcd(from as u64, to as u64);
    let (up, down) = ((to as u64 / g) as usize, (from as u64 / g) as usize);
    // Cut-off and transition width as shares of the input rate's Nyquist; scaled down when downsampling.
    let scale = (up as f64 / down as f64).min(1.0);
    let fc = 0.5 * (PASS + STOP) * scale;
    let width = (STOP - PASS) * scale;
    // Kaiser's design formulas for the window's shape and length.
    let beta = 0.1102 * (ATTEN_DB - 8.7);
    let taps = (ATTEN_DB - 8.0) / (2.285 * core::f64::consts::PI * width);
    let half = (taps / 2.0).ceil() as usize; // input samples each side of the centre
    let i0_beta = bessel_i0(beta);

    // One set of 2*half taps per output phase (up of them), for input offsets -half+1 ..= half.
    let taps_per = 2 * half;
    let mut bank = vec![0.0f64; up * taps_per];
    for p in 0..up {
        let frac = p as f64 / up as f64;
        for j in 0..taps_per {
            let t = (j as f64) - (half as f64 - 1.0) - frac; // input index offset minus the fractional position
            let r = t / half as f64;
            let w = if r.abs() >= 1.0 { 0.0 } else { bessel_i0(beta * libm::sqrt(1.0 - r * r)) / i0_beta };
            let a = core::f64::consts::PI * fc * t;
            let s = if t == 0.0 { 1.0 } else { libm::sin(a) / a };
            bank[p * taps_per + j] = fc * s * w;
        }
    }

    let n_out = (x.len() as u64 * up as u64).div_ceil(down as u64) as usize;
    let mut out = Vec::with_capacity(n_out);
    for n in 0..n_out {
        let pos = n * down; // in units of 1/up input samples
        let (i, p) = (pos / up, pos % up);
        let taps = &bank[p * taps_per..(p + 1) * taps_per];
        let start = i as isize - (half as isize - 1);
        let mut acc = 0.0f64;
        for (j, &h) in taps.iter().enumerate() {
            let k = start + j as isize;
            if k >= 0 && (k as usize) < x.len() {
                acc += h * x[k as usize] as f64;
            }
        }
        out.push(acc as f32);
    }
    out
}
