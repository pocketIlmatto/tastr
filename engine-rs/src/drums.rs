//! Drum lane activations (tastr/engine/drums.py), built-in lanes only (the optional ADTOF drum model stays on
//! the server): semi-adaptive NMF of the percussive mel spectrogram into low, mid and high parts, leakage
//! removed, and each hit sorted into a lane by a closer look at its spectrum and envelope.

use crate::features::Matrix;
use crate::np::{clip, pairwise_sum, percentile_f32};

pub const LANES: [&str; 7] = ["kick", "snare", "clap", "hat", "open_hat", "tom", "perc"];

pub const LANE_HELP: [(&str, &str); 7] = [
    ("kick", "Low hits whose energy sits below about 120 Hz."),
    ("snare", "Noisy mid hits (400 Hz to 5 kHz), usually with some body around 200 Hz."),
    ("clap", "Noisy mid hits with little body, made of several quick bursts."),
    ("hat", "Short hits above 6 kHz (closed hi-hat, shaker)."),
    ("open_hat", "Hits above 6 kHz that keep ringing (open hi-hat, ride, crash)."),
    ("tom", "Low hits pitched above about 120 Hz, higher than a kick."),
    ("perc", "Pitched mid hits without body (cowbell, rim, conga, wood block)."),
];

/// (centre Hz, width in octaves, weight) bumps per part: kick, snare, hat.
const TEMPLATES: [&[(f64, f64, f64)]; 3] =
    [&[(60.0, 0.7, 1.0), (3000.0, 1.0, 0.01)], &[(200.0, 0.4, 0.6), (1200.0, 1.0, 1.0)], &[(6000.0, 1.0, 1.0)]];
const PARTS: [&str; 3] = ["kick", "snare", "hat"];
const TOM_PITCH: (f64, f64) = (100.0, 135.0);
const UNMIX_PERCENTILE: f64 = 20.0;
const LEAKS: [(usize, usize); 3] = [(0, 1), (2, 1), (1, 0)];

fn ramp(x: f64, lo: f64, hi: f64) -> f64 {
    clip((x - lo) / (hi - lo), 0.0, 1.0)
}

/// bins x 3, column-normalised, row-major.
fn templates(freqs: &[f64]) -> Vec<[f64; 3]> {
    let mut w = vec![[0.0f64; 3]; freqs.len()];
    for (j, bumps) in TEMPLATES.iter().enumerate() {
        let col: Vec<f64> = freqs
            .iter()
            .map(|&fr| {
                let f = fr.max(10.0);
                let mut s = 0.0f64;
                for &(c, wd, a) in bumps.iter() {
                    let z = libm::log2(f / c) / wd;
                    s += a * libm::exp(-0.5 * (z * z));
                }
                s + 1e-4
            })
            .collect();
        let total = pairwise_sum(&col);
        for (r, v) in col.iter().enumerate() {
            w[r][j] = v / total;
        }
    }
    w
}

fn col_sums(w: &[[f64; 3]]) -> [f64; 3] {
    let mut s = [0.0f64; 3];
    for row in w {
        for j in 0..3 {
            s[j] += row[j];
        }
    }
    s
}

/// The NMF parts: activations (3 x frames, float32 as numpy keeps them) and the adapted templates.
pub struct Parts {
    pub h: [Vec<f32>; 3],
    pub w: Vec<[f64; 3]>,
    /// sqrt of the percussive mel power, bins x frames.
    pub v: Matrix,
}

/// drums.decompose: KL-divergence NMF whose templates adapt only late and partly.
pub fn decompose(mel: &Matrix, freqs: &[f64]) -> Parts {
    let (bins, t) = (mel.rows, mel.cols);
    let v = Matrix { rows: bins, cols: t, data: mel.data.iter().map(|x| x.max(0.0).sqrt()).collect() };
    let w0 = templates(freqs);
    let mut w = w0.clone();
    let init = crate::np::mean_f32(&v.data) + 1e-9f32;
    let mut h: [Vec<f32>; 3] = [vec![init; t], vec![init; t], vec![init; t]];
    let eps = 1e-12f64;
    let n_iter = 60;
    let mut ratio = vec![0.0f64; bins * t];
    let fill_ratio = |w: &[[f64; 3]], h: &[Vec<f32>; 3], ratio: &mut [f64]| {
        for r in 0..bins {
            let vr = v.row(r);
            let wr = w[r];
            let out = &mut ratio[r * t..(r + 1) * t];
            for c in 0..t {
                let wh = wr[0] * h[0][c] as f64 + wr[1] * h[1][c] as f64 + wr[2] * h[2][c] as f64 + eps;
                out[c] = vr[c] as f64 / wh;
            }
        }
    };
    for it in 0..n_iter {
        fill_ratio(&w, &h, &mut ratio);
        let wsum = col_sums(&w);
        let mut num = [vec![0.0f64; t], vec![0.0f64; t], vec![0.0f64; t]];
        for r in 0..bins {
            let rr = &ratio[r * t..(r + 1) * t];
            for j in 0..3 {
                let wj = w[r][j];
                for (n, x) in num[j].iter_mut().zip(rr) {
                    *n += wj * x;
                }
            }
        }
        for j in 0..3 {
            let den = wsum[j] + eps;
            for (hv, n) in h[j].iter_mut().zip(&num[j]) {
                *hv = (*hv as f64 * (n / den)) as f32;
            }
        }
        fill_ratio(&w, &h, &mut ratio);
        let hsum: [f32; 3] = [pairwise_sum(&h[0]) + 1e-12, pairwise_sum(&h[1]) + 1e-12, pairwise_sum(&h[2]) + 1e-12];
        let mut wn = vec![[0.0f64; 3]; bins];
        for r in 0..bins {
            let rr = &ratio[r * t..(r + 1) * t];
            for j in 0..3 {
                let mut acc = 0.0f64;
                for (x, hv) in rr.iter().zip(&h[j]) {
                    acc += x * *hv as f64;
                }
                wn[r][j] = w[r][j] * acc / hsum[j] as f64;
            }
        }
        let s = col_sums(&wn);
        for row in wn.iter_mut() {
            for j in 0..3 {
                row[j] /= s[j] + eps;
            }
        }
        let alpha = libm::pow(it as f64 / n_iter as f64, 4.0) * 0.5;
        for r in 0..bins {
            for j in 0..3 {
                w[r][j] = (1.0 - alpha) * w0[r][j] + alpha * wn[r][j];
            }
        }
        let s = col_sums(&w);
        for row in w.iter_mut() {
            for j in 0..3 {
                row[j] /= s[j];
            }
        }
    }
    Parts { h, w, v }
}

fn local_peaks(x: &[f32]) -> Vec<usize> {
    (1..x.len().saturating_sub(1)).filter(|&i| x[i] >= x[i - 1] && x[i] > x[i + 1]).collect()
}

fn roll_max(v: &[f32]) -> Vec<f32> {
    let n = v.len();
    (0..n).map(|i| v[i].max(v[(i + n - 1) % n].max(v[(i + 1) % n]))).collect()
}

/// drums._unmix: take off each part what it typically picks up when another drum hits alone.
fn unmix(nov: [Vec<f32>; 3]) -> [Vec<f32>; 3] {
    let mut out = nov.clone();
    let wide: Vec<Vec<f32>> = nov.iter().map(|v| roll_max(v)).collect();
    for src in 0..3 {
        let x = &nov[src];
        let pk = local_peaks(x);
        if pk.len() < 8 {
            continue;
        }
        let vals: Vec<f32> = pk.iter().map(|&p| x[p]).collect();
        let cut = percentile_f32(&vals, 70.0);
        let strong: Vec<usize> = pk.iter().cloned().filter(|&p| x[p] >= cut).collect();
        for dst in 0..3 {
            if !LEAKS.contains(&(src, dst)) || strong.len() < 4 {
                continue;
            }
            let ratio: Vec<f32> = strong.iter().map(|&p| wide[dst][p] / (x[p] + 1e-12f32)).collect();
            let c = percentile_f32(&ratio, UNMIX_PERCENTILE) as f64;
            if 0.0 < c && c <= 1.0 {
                let by = if dst == 1 { &wide[src] } else { x };
                for (o, b) in out[dst].iter_mut().zip(by) {
                    *o -= c as f32 * *b;
                }
            }
        }
    }
    for v in out.iter_mut() {
        for x in v.iter_mut() {
            *x = x.max(0.0);
        }
    }
    out
}

/// Everything lane_activations reads, computed once per track.
pub struct Drums {
    pub nov: [Vec<f32>; 3],
    parts: Parts,
    rise: Matrix,
    freqs: Vec<f64>,
    snare_ref: f64,
}

impl Drums {
    /// drums.onsets: each part's rise over two frames (the hat part replaced by the air band's flux), unmixed.
    pub fn new(mel: &Matrix, freqs: &[f64], air_flux: &[f32]) -> Drums {
        let parts = decompose(mel, freqs);
        let rise2 = |v: &[f32]| -> Vec<f32> {
            (0..v.len()).map(|i| if i < 2 { 0.0 } else { 0.0f32.max(v[i] - v[i - 2]) }).collect()
        };
        let nov = unmix([rise2(&parts.h[0]), rise2(&parts.h[1]), air_flux.to_vec()]);
        let v = &parts.v;
        let mut rise = Matrix { rows: v.rows, cols: v.cols, data: Vec::with_capacity(v.data.len()) };
        for r in 0..v.rows {
            rise.data.extend(rise2(v.row(r)));
        }
        let snare_ref = percentile_f32(&nov[1], 99.0) as f64 + 1e-12;
        Drums { nov, parts, rise, freqs: freqs.to_vec(), snare_ref }
    }

    /// drums.lane_activations: per lane (LANES order), the strength at each frame in `frames`.
    #[allow(clippy::too_many_arguments)]
    pub fn lane_activations(
        &self,
        frames: &[i64],
        env_mid: &[f32],
        env_air: &[f32],
        sr: f64,
        hop: usize,
        step_seconds: Option<f64>,
        slop: i64,
    ) -> Result<Vec<Vec<f64>>, String> {
        let mut out = vec![vec![0.0f64; frames.len()]; LANES.len()];
        let n = self.nov[0].len() as i64;
        if n < 3 || frames.is_empty() {
            return Ok(out);
        }
        let gap = step_seconds.filter(|&g| g != 0.0).unwrap_or(0.125);
        let freqs = &self.freqs;
        let low: Vec<usize> = (0..freqs.len()).filter(|&r| freqs[r] >= 30.0 && freqs[r] < 400.0).collect();
        let body: Vec<usize> = (0..freqs.len()).filter(|&r| freqs[r] >= 100.0 && freqs[r] < 400.0).collect();
        let mid: Vec<usize> = (0..freqs.len()).filter(|&r| freqs[r] >= 400.0 && freqs[r] < 5000.0).collect();
        let tonal: Vec<usize> = (0..freqs.len()).filter(|&r| freqs[r] >= 400.0 && freqs[r] < 3000.0).collect();
        let [kick, snare, hat] = &self.nov;
        for (k, &f) in frames.iter().enumerate() {
            let a = (f - slop).max(0);
            let b = (f + slop + 1).min(n);
            if b <= a {
                return Err("ValueError: zero-size array to reduction operation maximum which has no identity".into());
            }
            let (a, b) = (a as usize, b as usize);
            let smax = |v: &[f32]| v[a..b].iter().cloned().fold(f32::NEG_INFINITY, f32::max) as f64;
            let argmax = |v: &[f32]| {
                let mut best = a;
                for i in a..b {
                    if v[i] > v[best] {
                        best = i;
                    }
                }
                best
            };
            let (sk, ss, sh) = (smax(kick), smax(snare), smax(hat));
            let i = f * hop as i64;
            if sk > 0.0 {
                let t = argmax(kick);
                let col = (n as usize - 1).min(t + 1);
                let mut best = 0;
                for (j, &r) in low.iter().enumerate() {
                    if self.parts.v.row(r)[col] > self.parts.v.row(low[best])[col] {
                        best = j;
                    }
                }
                let pitch = freqs[low[best]];
                let tom = ramp(pitch, TOM_PITCH.0, TOM_PITCH.1) * (1.0 - ramp(ss / self.snare_ref, 0.15, 0.4));
                out[0][k] = sk * (1.0 - tom);
                out[5][k] += sk * tom;
            }
            if ss > 0.0 {
                let t = argmax(snare);
                let kt = kick[t] as f64;
                let spec: Vec<f64> =
                    (0..freqs.len()).map(|r| (self.rise.row(r)[t] as f64 - self.parts.w[r][0] * kt).max(0.0)).collect();
                let pick = |idx: &[usize]| idx.iter().map(|&r| spec[r]).collect::<Vec<f64>>();
                let sb = pairwise_sum(&pick(&body));
                let body_share = sb / (sb + pairwise_sum(&pick(&mid)) + 1e-12);
                let tm = pick(&tonal);
                let tmax = tm.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
                let peaky = if !tm.is_empty() && tmax > 0.0 {
                    tmax / (pairwise_sum(&tm) / tm.len() as f64 + 1e-12)
                } else {
                    0.0
                };
                let n_bursts = bursts(env_mid, i, sr);
                let perc = ramp(peaky, 4.0, 7.0) * (1.0 - ramp(body_share, 0.25, 0.45));
                let clap = ramp(n_bursts as f64, 2.0, 3.0) * (1.0 - ramp(body_share, 0.35, 0.55)) * (1.0 - perc);
                let sn = (1.0 - perc - clap).max(0.0);
                out[6][k] += ss * perc;
                out[2][k] += ss * clap;
                out[1][k] += ss * sn;
            }
            if sh > 0.0 {
                let ring = ringing(env_air, i, sr, gap);
                out[3][k] = sh * (1.0 - ring);
                out[4][k] = sh * ring;
            }
        }
        Ok(out)
    }
}

fn window(len: usize, i: i64, back: f64, ahead: f64, sr: f64) -> (usize, usize) {
    let a = (i - (back * sr) as i64).max(0) as usize;
    let b = ((i + (ahead * sr) as i64).max(0) as usize).min(len);
    (a, b)
}

/// drums._bursts: quick bursts in the mid band within 40 ms of sample i (scipy find_peaks).
fn bursts(env: &[f32], i: i64, sr: f64) -> usize {
    let (a, b) = window(env.len(), i, 0.015, 0.04, sr);
    if b <= a || b - a < 8 {
        return 0;
    }
    let seg: Vec<f64> = env[a..b].iter().map(|&v| v as f64).collect();
    let height = seg.iter().cloned().fold(f64::NEG_INFINITY, f64::max) - 10.0;
    find_peaks(&seg, 6.0, ((0.004 * sr) as usize).max(1), height).len()
}

/// scipy.signal.find_peaks(x, prominence, distance, height): local maxima (plateau midpoints), then the
/// height, distance and prominence filters in scipy's order.
pub fn find_peaks(x: &[f64], prominence: f64, distance: usize, height: f64) -> Vec<usize> {
    let n = x.len();
    let mut peaks = Vec::new();
    let mut i = 1;
    while i + 1 < n {
        if x[i - 1] < x[i] {
            let mut ahead = i + 1;
            while ahead < n - 1 && x[ahead] == x[i] {
                ahead += 1;
            }
            if x[ahead] < x[i] {
                peaks.push((i + ahead - 1) / 2);
                i = ahead;
            }
        }
        i += 1;
    }
    peaks.retain(|&p| x[p] >= height);
    // distance: keep the highest peaks first (np.argsort's order for ties: stable enough for audio)
    let m = peaks.len();
    let mut keep = vec![true; m];
    let mut order: Vec<usize> = (0..m).collect();
    order.sort_by(|&p, &q| x[peaks[p]].total_cmp(&x[peaks[q]]));
    for &j in order.iter().rev() {
        if !keep[j] {
            continue;
        }
        let mut k = j as isize - 1;
        while k >= 0 && peaks[j] - peaks[k as usize] < distance {
            keep[k as usize] = false;
            k -= 1;
        }
        let mut k = j + 1;
        while k < m && peaks[k] - peaks[j] < distance {
            keep[k] = false;
            k += 1;
        }
    }
    let peaks: Vec<usize> = peaks.into_iter().zip(keep).filter(|(_, k)| *k).map(|(p, _)| p).collect();
    peaks
        .into_iter()
        .filter(|&p| {
            let v = x[p];
            let mut left = v;
            let mut i = p as isize;
            while i >= 0 && x[i as usize] <= v {
                left = left.min(x[i as usize]);
                i -= 1;
            }
            let mut right = v;
            let mut i = p;
            while i < n && x[i] <= v {
                right = right.min(x[i]);
                i += 1;
            }
            v - left.max(right) >= prominence
        })
        .collect()
}

/// drums._ringing: how much the air band still rings after a hit at sample i (1 = open hat).
fn ringing(env: &[f32], i: i64, sr: f64, gap: f64) -> f64 {
    let (a, b) = window(env.len(), i, 0.015, 0.03, sr);
    if b <= a {
        return 0.0;
    }
    let mut p = a;
    for k in a..b {
        if env[k] > env[p] {
            p = k;
        }
    }
    let look = 0.09f64.min(0.03f64.max(gap - 0.02));
    let j = (env.len() - 1).min(p + (look * sr) as usize);
    let drop = (env[p] as f64 - env[j] as f64) * 0.09 / look;
    1.0 - ramp(drop, 9.0, 16.0)
}

pub fn lane_help(lane: &str) -> Option<&'static str> {
    LANE_HELP.iter().find(|(k, _)| *k == lane).map(|(_, h)| *h)
}

pub const PART_NAMES: [&str; 3] = PARTS;
