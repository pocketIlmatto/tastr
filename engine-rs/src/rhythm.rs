//! Tempo and beats, as librosa 1.0 does them for the core stage: the onset autocorrelation tempogram, the tempo
//! estimate, the dynamic-programming beat tracker (Ellis 2007) and onset peak picking. Each follows librosa's
//! code step by step, including the precision of each step (numba keeps some sums in float32).

use rustfft::num_complex::Complex;
use rustfft::FftPlannerScalar;

use crate::np::{median, pairwise_sum, round_half_even};

/// Frames per second of the onset envelope.
fn frame_rate(sr: f64, hop: usize) -> f64 {
    sr / hop as f64
}

/// scipy.fft.next_fast_len(n, real=True): the next 2^a 3^b 5^c at or above n.
fn next_fast_len(n: usize) -> usize {
    let mut m = n.max(1);
    loop {
        let mut k = m;
        for p in [2, 3, 5] {
            while k.is_multiple_of(p) {
                k /= p;
            }
        }
        if k == 1 {
            return m;
        }
        m += 1;
    }
}

/// librosa.feature.tempogram(onset_envelope, win_length): win_length x frames, row-major by lag. Each frame is a
/// Hann-windowed stretch of the envelope (linear ramps to 0 padding both ends), autocorrelated, then scaled so
/// its largest value is 1.
pub fn tempogram(onset: &[f32], win_length: usize) -> Vec<f64> {
    let n = onset.len();
    let half = win_length / 2;
    // np.pad(..., mode="linear_ramp", end_values=0): a ramp from 0 up to (not including) the edge value.
    let mut padded = Vec::with_capacity(n + 2 * half);
    let (first, last) = (*onset.first().unwrap_or(&0.0) as f64, *onset.last().unwrap_or(&0.0) as f64);
    for i in 0..half {
        padded.push((first * i as f64 / half as f64) as f32);
    }
    padded.extend_from_slice(onset);
    for i in (0..half).rev() {
        padded.push((last * i as f64 / half as f64) as f32);
    }
    let win: Vec<f64> = (0..win_length)
        .map(|k| 0.5 - 0.5 * libm::cos(2.0 * core::f64::consts::PI * k as f64 / win_length as f64))
        .collect();
    let n_pad = next_fast_len(2 * win_length - 1);
    let mut planner = FftPlannerScalar::<f64>::new();
    let fwd = planner.plan_fft_forward(n_pad);
    let inv = planner.plan_fft_inverse(n_pad);
    let mut buf = vec![Complex::new(0.0f64, 0.0); n_pad];
    let mut scratch = vec![Complex::new(0.0f64, 0.0); fwd.get_inplace_scratch_len().max(inv.get_inplace_scratch_len())];
    let mut out = vec![0.0f64; win_length * n];
    let mut col = vec![0.0f64; win_length];
    for t in 0..n {
        for (k, c) in buf.iter_mut().enumerate() {
            *c = if k < win_length { Complex::new(padded[t + k] as f64 * win[k], 0.0) } else { Complex::new(0.0, 0.0) };
        }
        fwd.process_with_scratch(&mut buf, &mut scratch);
        for c in buf.iter_mut() {
            *c = Complex::new(c.re * c.re + c.im * c.im, 0.0);
        }
        inv.process_with_scratch(&mut buf, &mut scratch);
        let mut peak = 0.0f64;
        for (k, v) in col.iter_mut().enumerate() {
            *v = buf[k].re / n_pad as f64;
            peak = peak.max(v.abs());
        }
        let norm = if peak < f64::MIN_POSITIVE { 1.0 } else { peak };
        for (k, v) in col.iter().enumerate() {
            out[k * n + t] = v / norm;
        }
    }
    out
}

/// librosa.tempo_frequencies(n, hop_length, sr): the BPM of each autocorrelation lag (lag 0 is infinite).
pub fn tempo_frequencies(n: usize, sr: f64, hop: usize) -> Vec<f64> {
    (0..n).map(|i| if i == 0 { f64::INFINITY } else { 60.0 * sr / (hop as f64 * i as f64) }).collect()
}

/// Mean of each tempogram row over time.
pub fn tempogram_mean(tg: &[f64], win_length: usize, frames: usize) -> Vec<f64> {
    (0..win_length)
        .map(|k| {
            let row = &tg[k * frames..(k + 1) * frames];
            pairwise_sum(row) / frames as f64
        })
        .collect()
}

/// librosa.feature.tempo(onset_envelope, sr, hop): the 8 s tempogram's average, weighted by a log-normal prior
/// around 120 BPM, capped at 320 BPM.
pub fn tempo(onset: &[f32], sr: f64, hop: usize) -> f64 {
    let win_length = ((8.0 * sr) as usize) / hop; // librosa.time_to_frames(8.0)
    let tg = tempogram(onset, win_length);
    let mean = tempogram_mean(&tg, win_length, onset.len());
    let bpms = tempo_frequencies(win_length, sr, hop);
    let max_idx = bpms.iter().position(|&b| b < 320.0).unwrap_or(0);
    let mut best = (f64::NEG_INFINITY, 0usize);
    for (i, (&ac, &bpm)) in mean.iter().zip(&bpms).enumerate() {
        let prior = if i < max_idx { f64::NEG_INFINITY } else { -0.5 * (libm::log2(bpm) - libm::log2(120.0)).powi(2) };
        let score = libm::log1p(1e6 * ac) + prior;
        if score > best.0 {
            best = (score, i);
        }
    }
    bpms[best.1]
}

/// librosa.beat.beat_track(onset_envelope, sr, hop, bpm): (tempo, beat frames).
pub fn beat_track(onset: &[f32], sr: f64, hop: usize, bpm: Option<f64>) -> (f64, Vec<usize>) {
    if !onset.iter().any(|&v| v != 0.0) {
        return (0.0, Vec::new());
    }
    let bpm = bpm.unwrap_or_else(|| tempo(onset, sr, hop));
    let fpb = round_half_even(frame_rate(sr, hop) * 60.0 / bpm);
    let n = onset.len();
    let local = beat_local_score(&normalize_onsets(onset), fpb);
    let (cum, backlink) = beat_track_dp(&local, fpb);
    let mut beats = vec![false; n];
    let mut k = last_beat(&cum);
    while k >= 0 {
        beats[k as usize] = true;
        k = backlink[k as usize];
    }
    trim_beats(&local, &mut beats);
    (bpm, (0..n).filter(|&i| beats[i]).collect())
}

/// __normalize_onsets: divide by the sample standard deviation, which numpy computes in float32 with its
/// pairwise sums.
fn normalize_onsets(onset: &[f32]) -> Vec<f32> {
    let n = onset.len();
    let mean = pairwise_sum(onset) / n as f32;
    let sq: Vec<f32> = onset.iter().map(|&v| (v - mean) * (v - mean)).collect();
    let norm = (pairwise_sum(&sq) / (n - 1) as f32).sqrt() + f32::MIN_POSITIVE;
    onset.iter().map(|&v| v / norm).collect()
}

/// __beat_local_score: a Gaussian-windowed "same" convolution, summed into a float32 slot step by step. Like
/// librosa, the k range stops one short, so the envelope's first frame never contributes.
fn beat_local_score(normed: &[f32], fpb: f64) -> Vec<f32> {
    let n = normed.len();
    let f = fpb as i64;
    let window: Vec<f64> = (-f..=f).map(|j| libm::exp(-0.5 * (j as f64 * 32.0 / fpb).powi(2))).collect();
    let kk = window.len() as i64;
    let mut local = vec![0.0f32; n];
    for (i, slot) in local.iter_mut().enumerate() {
        let i = i as i64;
        let mut acc = 0.0f32;
        for k in (i + kk / 2 - n as i64 + 1).max(0)..(i + kk / 2).min(kk) {
            acc = (acc as f64 + window[k as usize] * normed[(i + kk / 2 - k) as usize] as f64) as f32;
        }
        *slot = acc;
    }
    local
}

/// __beat_track_dp, in float64 (numba picks its float64 signature for a float64 tempo): (cumulative score,
/// backlinks).
fn beat_track_dp(local: &[f32], fpb: f64) -> (Vec<f64>, Vec<i64>) {
    let n = local.len();
    let tightness = 100.0f64;
    let max_local = local.iter().cloned().fold(f32::NEG_INFINITY, f32::max) as f64;
    let thresh = 0.01 * max_local;
    let mut backlink = vec![-1i64; n];
    let mut cum = vec![0.0f64; n];
    let mut first_beat = true;
    let log_fpb = libm::log(fpb);
    for i in 0..n {
        let score_i = local[i] as f64;
        let from = i as i64 - round_half_even(fpb / 2.0) as i64;
        let to = (i as f64 - 2.0 * fpb - 1.0) as i64; // int() truncates toward zero
        let (best, at) = best_predecessor(&cum, i as i64, from, to, |d| tightness * (libm::log(d) - log_fpb).powi(2));
        cum[i] = if at >= 0 { score_i + best } else { score_i };
        if first_beat && score_i < thresh {
            backlink[i] = -1;
        } else {
            backlink[i] = at;
            first_beat = false;
        }
    }
    (cum, backlink)
}

/// The best earlier beat for frame i among frames `from` down to (not including) `to`: (score, frame), or
/// (-inf, -1) when there is none. `penalty(distance)` is the tempo deviation cost.
fn best_predecessor(cum: &[f64], i: i64, from: i64, to: i64, penalty: impl Fn(f64) -> f64) -> (f64, i64) {
    let (mut best, mut at) = (f64::NEG_INFINITY, -1i64);
    let mut loc = from;
    while loc > to && loc >= 0 {
        let s = cum[loc as usize] - penalty((i - loc) as f64);
        if s > best {
            best = s;
            at = loc;
        }
        loc -= 1;
    }
    (best, at)
}

/// __last_beat: the last local maximum of the cumulative score above half their median (the last frame when
/// there is none).
fn last_beat(cum: &[f64]) -> i64 {
    let n = cum.len();
    let mut is_max = vec![false; n];
    for i in 1..n.saturating_sub(1) {
        is_max[i] = cum[i] > cum[i - 1] && cum[i] >= cum[i + 1];
    }
    if n >= 2 {
        is_max[n - 1] = cum[n - 1] > cum[n - 2];
    }
    let maxima: Vec<f64> = (0..n).filter(|&i| is_max[i]).map(|i| cum[i]).collect();
    let threshold = if maxima.is_empty() { 0.0 } else { 0.5 * median(&maxima) };
    (0..n).rev().find(|&j| is_max[j] && cum[j] >= threshold).map_or(n as i64 - 1, |j| j as i64)
}

/// __trim_beats: drop leading and trailing frames whose local score is under half the RMS of the smoothed
/// beat strengths.
fn trim_beats(local: &[f32], beats: &mut [bool]) {
    let n = local.len();
    let on: Vec<f32> = (0..n).filter(|&i| beats[i]).map(|i| local[i]).collect();
    let w = [0.0f64, 0.5, 1.0, 0.5, 0.0]; // np.hanning(5)
    let full_len = on.len() + 4;
    let smooth: Vec<f64> = (0..full_len)
        .map(|t| {
            let mut s = 0.0f64;
            for (q, &wq) in w.iter().enumerate() {
                if t >= q && t - q < on.len() {
                    s += on[t - q] as f64 * wq;
                }
            }
            s
        })
        .collect();
    let kept = &smooth[2..full_len.min(n + 2)];
    let rms = (kept.iter().map(|v| v * v).sum::<f64>() / kept.len() as f64).sqrt();
    let threshold = 0.5 * rms;
    for i in 0..n {
        if (local[i] as f64) > threshold {
            break;
        }
        beats[i] = false;
    }
    for i in (0..n).rev() {
        if (local[i] as f64) > threshold {
            break;
        }
        beats[i] = false;
    }
}

/// librosa.onset.onset_detect(onset_envelope, sr, hop): the envelope scaled to 0..1, then greedy peak picking
/// (30 ms local max, 100 ms local mean + 0.07, 30 ms wait).
pub fn onset_detect(onset: &[f32], sr: f64, hop: usize) -> Vec<usize> {
    let n = onset.len();
    if n == 0 {
        return Vec::new();
    }
    let min = onset.iter().cloned().fold(f32::INFINITY, f32::min);
    let shifted: Vec<f32> = onset.iter().map(|&v| v - min).collect();
    let max = shifted.iter().cloned().fold(f32::NEG_INFINITY, f32::max);
    let x: Vec<f32> = shifted.iter().map(|&v| v / (max + f32::MIN_POSITIVE)).collect();
    if !x.iter().any(|&v| v != 0.0) || !x.iter().all(|v| v.is_finite()) {
        return Vec::new();
    }
    let frames = |s: f64| ((s * sr) / hop as f64).floor(); // 0.03 * sr // hop
    let pre_max = frames(0.03).ceil() as usize;
    let post_max = (frames(0.0) + 1.0).ceil() as usize;
    let pre_avg = frames(0.10).ceil() as usize;
    let post_avg = (frames(0.10) + 1.0).ceil() as usize;
    let wait = frames(0.03).ceil() as usize;
    let delta = 0.07f64;

    let max_of = |a: usize, b: usize| x[a..b].iter().cloned().fold(f32::NEG_INFINITY, f32::max);
    // numba's np.mean: a float32 running sum, divided in float64, rounded back to float32; the threshold then
    // adds delta (a Python float) in float64.
    let mean_of = |a: usize, b: usize| {
        let mut s = 0.0f32;
        for v in &x[a..b] {
            s += v;
        }
        (s as f64 / (b - a) as f64) as f32 as f64
    };
    let mut peaks = Vec::new();
    let first = x[0] >= max_of(0, post_max.min(n)) && x[0] as f64 >= mean_of(0, post_avg.min(n)) + delta;
    let mut i = if first {
        peaks.push(0);
        wait + 1
    } else {
        1
    };
    while i < n {
        if x[i] != max_of(i.saturating_sub(pre_max), (i + post_max).min(n)) {
            i += 1;
            continue;
        }
        if (x[i] as f64) < mean_of(i.saturating_sub(pre_avg), (i + post_avg).min(n)) + delta {
            i += 1;
            continue;
        }
        peaks.push(i);
        i += wait + 1;
    }
    peaks
}
