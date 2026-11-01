//! The core stage (tastr/engine/stages/core.py): BPM, beats and downbeats, meter, key, energy, loudness and the
//! shared scalar features.

use super::{Ctx, HOP};
use crate::card::{Card, Key};
use crate::features::Matrix;
use crate::json::Json;
use crate::music;
use crate::np::{
    argmax, clip, mean, mean_f32, median, median_f32, pairwise_sum, percentile, polyfit1, round_dec, round_half_even,
    std, std_f32, strided_mean,
};
use crate::rhythm;

const MIN_BPM: f64 = 70.0;
const MAX_BPM: f64 = 180.0;
const MIN_BEATS: usize = 8;
const THREE_TWO_RATIO: f64 = 0.8;
const THREE_TWO_MAX_METER: f64 = 0.0;
/// Pulse clarity: (onset on the tracker's beats / mean onset - 1) / PULSE_SCALE, read before the kick
/// alignment (core.py PULSE_SCALE).
const PULSE_SCALE: f64 = 4.0;
const METER_CUE_SCALES: [(&str, f64); 6] =
    [("timbre_lag", 0.1), ("bass_lag", 0.27), ("kick", 0.3), ("snare", 0.25), ("change", 0.17), ("bass_change", 0.22)];
const METER_THRESHOLD: f64 = 1.0;

pub fn run(ctx: &mut Ctx, card: &mut Card) -> Result<(), String> {
    let sr = ctx.sr;
    let n = ctx.y.len();
    let duration = n as f64 / sr;
    let sq: Vec<f32> = ctx.y.iter().map(|v| v * v).collect();
    let loud = 20.0f32 * libm::log10f((if n > 0 { mean_f32(&sq) } else { f32::NAN }).sqrt() + 1e-9);
    card.core.loudness_db = Some(round_dec(loud as f64, 2));
    if n == 0 || !ctx.y.iter().any(|v| v.abs() > 1e-4) {
        return Ok(());
    }
    let rms_db: Vec<f32> = ctx.rms.iter().map(|&v| 20.0f32 * libm::log10f(v.max(1e-5))).collect();
    let n_frames = ctx.onset.len();

    let (bpm, beat_frames, bpm_conf, stability, pulse_clarity) = tempo_and_beats(ctx, n_frames);
    let beat_idx: Vec<usize> =
        beat_frames.iter().map(|&f| clip(round_half_even(f), 0.0, n_frames.max(1) as f64 - 1.0) as usize).collect();
    if let Some(bpm) = bpm {
        let beat_times: Vec<f64> = beat_frames.iter().map(|f| f * HOP as f64 / sr).collect();
        set_tempo(ctx, card, bpm, bpm_conf, &beat_times, &beat_idx, duration);
    }
    set_features(ctx, card, bpm, &beat_idx, &rms_db, duration, (stability, pulse_clarity));
    set_key(ctx, card);
    if !card.core.downbeat_times.is_empty() {
        let (t, v) = bar_energy_series(&card.core.downbeat_times, &rms_db, sr, duration);
        card.series.push(("energy".into(), t, v));
    }
    Ok(())
}

/// BPM, beats, meter and downbeats on the card.
fn set_tempo(ctx: &Ctx, card: &mut Card, bpm: f64, conf: f64, beat_times: &[f64], beat_idx: &[usize], duration: f64) {
    let core = &mut card.core;
    core.bpm = Some(round_dec(bpm, 2));
    core.bpm_confidence = Some(round_dec(conf, 3));
    core.bpm_likely_correct = Some(music::likely_correct("bpm", conf));
    core.beat_times = beat_times.iter().filter(|&&t| t <= duration).map(|&t| round_dec(t, 3)).collect();
    let cues = if beat_idx.len() >= MIN_BEATS && ctx.log_mel.cols >= 2 {
        Some(beat_cues(beat_idx, &ctx.log_mel, &ctx.log_mel_freqs))
    } else {
        None
    };
    let meter = meter_score(beat_idx, &ctx.log_mel, &ctx.log_mel_freqs, cues.as_ref());
    let guess = if meter > METER_THRESHOLD { 3 } else { 4 };
    card.extras.set("core.meter_guess", Json::obj().with("beats_per_bar", guess).with("score", round_dec(meter, 2)));
    let (bpb, set) = match ctx.beats_per_bar {
        Some(m @ (3 | 4)) => (m, true),
        _ => (guess, false),
    };
    let core = &mut card.core;
    core.beats_per_bar = bpb;
    core.meter_set = set;
    let phase = downbeat_phase(beat_idx, &ctx.log_mel, &ctx.log_mel_freqs, bpb as usize, cues.as_ref());
    core.downbeat_times = beat_times
        .iter()
        .skip(phase)
        .step_by(bpb as usize)
        .filter(|&&t| t <= duration)
        .map(|&t| round_dec(t, 3))
        .collect();
    card.extras.set("core.bpm_alternatives", vec![round_dec(bpm / 2.0, 2), round_dec(bpm * 2.0, 2)]);
    card.extras.set("core.downbeat_phase", phase);
}

/// The share of spectral power below 150 Hz.
fn low_end(ctx: &Ctx) -> f64 {
    let bins = crate::spectral::BINS;
    let frames = ctx.frames;
    let freqs = crate::features::fft_frequencies(ctx.sr, crate::spectral::N_FFT);
    let mut low = Vec::new();
    let mut all = Vec::with_capacity(bins * frames);
    for (b, &fb) in freqs.iter().enumerate().take(bins) {
        for t in 0..frames {
            let p = ctx.mag[t * bins + b] * ctx.mag[t * bins + b];
            all.push(p);
            if fb < 150.0 {
                low.push(p);
            }
        }
    }
    (pairwise_sum(&low) / (pairwise_sum(&all) + 1e-9)) as f64
}

/// Energy and the scalar features (brightness, percussiveness, danceability, swing...).
fn set_features(
    ctx: &Ctx,
    card: &mut Card,
    bpm: Option<f64>,
    beat_idx: &[usize],
    rms_db: &[f32],
    duration: f64,
    (stability, pulse_clarity): (f64, f64),
) {
    let low_end = low_end(ctx);
    let brightness = clip((mean_f32(&ctx.centroid) / 3500.0f32) as f64, 0.0, 1.0);
    let sum_sq = |x: &[f32]| pairwise_sum(&x.iter().map(|v| v * v).collect::<Vec<f32>>());
    let (hp, pp) = (sum_sq(&ctx.harmonic), sum_sq(&ctx.percussive));
    let perc_ratio = (pp / (hp + pp + 1e-9)) as f64;
    let onsets = rhythm::onset_detect(&ctx.onset, ctx.sr, HOP);
    let onset_rate = if duration > 0.0 { onsets.len() as f64 / duration } else { 0.0 };
    let perc_score = clip(perc_ratio / 0.5, 0.0, 1.0);
    let loud_score = clip((card.core.loudness_db.unwrap() + 30.0) / 22.0, 0.0, 1.0);
    let energy = 0.35 * loud_score + 0.25 * clip(onset_rate / 6.0, 0.0, 1.0) + 0.20 * brightness + 0.20 * perc_score;
    card.core.energy = Some(round_dec(clip(energy, 0.0, 1.0), 3));

    let tempo_fit = bpm.map_or(0.0, |b| libm::exp(-((b - 135.0) / 45.0).powi(2)));
    let danceability = 0.25 * tempo_fit + 0.75 * perc_score;
    let (swing_pct, swing_conf) = if bpm.is_some() { swing(&ctx.onset, beat_idx) } else { (50.0, 0.0) };
    let rdb: Vec<f64> = rms_db.iter().map(|&v| v as f64).collect();
    for (k, v) in [
        ("brightness", round_dec(brightness, 3)),
        ("percussiveness", round_dec(perc_ratio, 3)),
        ("low_end", round_dec(low_end, 3)),
        ("pulse_clarity", round_dec(pulse_clarity, 3)),
        ("danceability", round_dec(clip(danceability, 0.0, 1.0), 3)),
        ("onset_rate", round_dec(onset_rate, 3)),
        ("dynamic_range_db", round_dec(percentile(&rdb, 95.0) - percentile(&rdb, 10.0), 2)),
        ("swing_percent", round_dec(swing_pct, 1)),
        ("swing_confidence", round_dec(swing_conf, 3)),
        ("tempo_stability", round_dec(stability, 3)),
    ] {
        card.set_feature(k, v);
    }
}

/// The key and its alternatives, when the track has any pitch content.
fn set_key(ctx: &Ctx, card: &mut Card) {
    let profile = music::key_profile(&ctx.chroma);
    if !profile.iter().any(|&v| v > 0.0) {
        return;
    }
    let est = music::estimate_key(&profile);
    card.core.key = Some(Key {
        tonic: est.tonic,
        mode: est.mode,
        scale: music::refine_mode(&profile, est.tonic, est.mode),
        camelot: music::camelot(est.tonic, est.mode),
        confidence: est.confidence,
        likely_correct: music::likely_correct("key", est.confidence),
    });
    let alts: Vec<Json> =
        est.alternatives.iter().map(|(k, p)| Json::obj().with("key", k.clone()).with("probability", *p)).collect();
    card.extras.set("core.key_alternatives", Json::Arr(alts));
}

fn diffs(x: &[f64]) -> Vec<f64> {
    x.windows(2).map(|w| w[1] - w[0]).collect()
}

fn beat_index(frames: &[f64], n_frames: usize) -> Vec<usize> {
    frames.iter().map(|&f| clip(round_half_even(f), 0.0, n_frames as f64 - 1.0) as usize).collect()
}

/// _tempo_and_beats: (bpm, beat frames, confidence, stability, pulse clarity).
fn tempo_and_beats(ctx: &mut Ctx, n_frames: usize) -> (Option<f64>, Vec<f64>, f64, f64, f64) {
    let (sr, hop) = (ctx.sr, HOP as f64);
    let onset = ctx.onset.clone();
    let max = onset.iter().cloned().fold(f32::NEG_INFINITY, f32::max);
    if n_frames < 2 || max <= 1e-6 || std_f32(&onset) < 1e-6 {
        return (None, Vec::new(), 0.0, 0.0, 0.0);
    }
    let frames = tracked_beats(&onset, sr, n_frames);
    let bpm = 60.0 / (median(&diffs(&frames)) * hop / sr);
    let (bpm, frames) = three_over_two(ctx, &onset, bpm, frames, n_frames);
    let (mut bpm, frames) = fold_into_range(bpm, frames);
    let ibis: Vec<f64> = diffs(&frames).iter().map(|d| d * hop / sr).collect();
    let stability = if ibis.is_empty() { 0.0 } else { clip(1.0 - std(&ibis) / (mean(&ibis) + 1e-9) * 4.0, 0.0, 1.0) };
    let frames = regularize(&frames, n_frames);
    let picked: Vec<f32> = beat_index(&frames, n_frames).into_iter().map(|i| onset[i]).collect();
    let ratio = mean_f32(&picked) / (mean_f32(&onset) + 1e-9);
    let pulse = clip(((ratio - 1.0) as f64) / PULSE_SCALE, 0.0, 1.0);
    let frames = align_to_kick(&frames, &ctx.low_flux);
    if frames.len() > 1 {
        bpm = 60.0 * sr / (hop * median(&diffs(&frames)));
    }
    (Some(bpm), frames, 0.5 * stability + 0.5 * pulse, stability, pulse)
}

/// The beat tracker's beats, or an even grid at its tempo (120 BPM when it has none) when it finds too few.
fn tracked_beats(onset: &[f32], sr: f64, n_frames: usize) -> Vec<f64> {
    let (mut tempo, beats) = rhythm::beat_track(onset, sr, HOP, None);
    if beats.len() >= MIN_BEATS && tempo > 0.0 {
        return beats.iter().map(|&b| b as f64).collect();
    }
    if tempo <= 0.0 {
        tempo = 120.0;
    }
    let step = 60.0 / tempo * sr / HOP as f64;
    let count = libm::ceil(n_frames as f64 / step) as usize;
    (0..count).map(|i| i as f64 * step).collect()
}

/// Try 1.5x the tempo (a 3:2 error): taken when its periodicity is nearly as strong and its beats don't read as
/// triple meter.
fn three_over_two(ctx: &mut Ctx, onset: &[f32], bpm: f64, frames: Vec<f64>, n_frames: usize) -> (f64, Vec<f64>) {
    if !(bpm * 1.5 < MAX_BPM && periodicity(ctx, bpm * 1.5) >= THREE_TWO_RATIO * periodicity(ctx, bpm)) {
        return (bpm, frames);
    }
    let faster: Vec<f64> =
        rhythm::beat_track(onset, ctx.sr, HOP, Some(bpm * 1.5)).1.iter().map(|&b| b as f64).collect();
    if faster.len() >= MIN_BEATS
        && meter_score(&beat_index(&faster, n_frames), &ctx.log_mel, &ctx.log_mel_freqs, None) <= THREE_TWO_MAX_METER
    {
        return (60.0 / (median(&diffs(&faster)) * HOP as f64 / ctx.sr), faster);
    }
    (bpm, frames)
}

/// Double or halve the beats until the tempo is in MIN_BPM..MAX_BPM (halving only while enough beats remain).
fn fold_into_range(mut bpm: f64, mut frames: Vec<f64>) -> (f64, Vec<f64>) {
    while bpm < MIN_BPM {
        let mut all = frames.clone();
        all.extend(frames.windows(2).map(|w| (w[0] + w[1]) / 2.0));
        all.sort_by(|a, b| a.total_cmp(b));
        frames = all;
        bpm *= 2.0;
    }
    while bpm >= MAX_BPM && frames.len() >= 2 * MIN_BEATS {
        frames = frames.iter().step_by(2).cloned().collect();
        bpm /= 2.0;
    }
    (bpm, frames)
}

/// _periodicity: the 384-lag tempogram's mean strength at `bpm`, best of the nearest three lags.
fn periodicity(ctx: &mut Ctx, bpm: f64) -> f64 {
    let (sr, hop) = (ctx.sr, HOP);
    let ac = ctx.tempogram().to_vec();
    let bpms = rhythm::tempo_frequencies(ac.len(), sr, hop);
    let d: Vec<f64> = bpms[1..].iter().map(|b| (b - bpm).abs()).collect();
    let mut i = 0;
    for (j, &v) in d.iter().enumerate() {
        if v < d[i] {
            i = j;
        }
    }
    let i = i + 1;
    ac[(i - 1).max(1)..(i + 2).min(ac.len())].iter().cloned().fold(f64::NEG_INFINITY, f64::max)
}

/// _regularize: a uniform grid across the track when the tracked beats are steady.
fn regularize(frames: &[f64], n_frames: usize) -> Vec<f64> {
    if frames.len() < MIN_BEATS {
        return frames.to_vec();
    }
    let d = diffs(frames);
    let med = median(&d);
    let small: Vec<f64> = d.iter().cloned().filter(|&v| v < 1.5 * med).collect();
    let typical = mean(&small);
    let mut k = vec![0.0f64];
    let mut acc = 0.0;
    for v in &d {
        acc += round_half_even(v / typical).max(1.0);
        k.push(acc);
    }
    let (period, offset) = polyfit1(&k, frames);
    let worst =
        frames.iter().zip(&k).map(|(f, kk)| (f - (offset + period * kk)).abs()).fold(f64::NEG_INFINITY, f64::max);
    if period <= 0.0 || worst > 0.2 * period {
        return frames.to_vec();
    }
    let first = libm::ceil(-offset / period - 1e-9) as i64;
    let last = libm::floor((n_frames as f64 - 1.0 - offset) / period) as i64;
    (first..=last).map(|i| offset + period * i as f64).collect()
}

/// _align_to_kick: slide the grid onto the kick when the kick evidence is clearly stronger elsewhere.
fn align_to_kick(frames: &[f64], low_flux: &[f32]) -> Vec<f64> {
    let n = low_flux.len();
    let max = low_flux.iter().cloned().fold(f32::NEG_INFINITY, f32::max);
    if frames.len() < MIN_BEATS || n == 0 || max <= 0.0 {
        return frames.to_vec();
    }
    let period = median(&diffs(frames));
    let half = libm::floor(period / 2.0) as i64;
    let scores: Vec<f32> = (-half..=half)
        .map(|d| {
            let v: Vec<f32> = frames
                .iter()
                .map(|&f| low_flux[clip(round_half_even(f + d as f64), 0.0, n as f64 - 1.0) as usize])
                .collect();
            mean_f32(&v)
        })
        .collect();
    let base = scores[half as usize];
    let mut best = 0;
    for (i, &s) in scores.iter().enumerate() {
        if s > scores[best] {
            best = i;
        }
    }
    if scores[best] <= base * 1.25 + 1e-9 {
        return frames.to_vec();
    }
    let shift = best as f64 - half as f64;
    let mut shifted: Vec<f64> =
        frames.iter().map(|f| f + shift).filter(|&f| f >= -2.0 && f <= n as f64 - 1.0).collect();
    if shifted.is_empty() {
        return frames.to_vec();
    }
    while shifted[0] - period >= -2.0 {
        shifted.insert(0, shifted[0] - period);
    }
    shifted.iter().map(|&f| f.max(0.0)).collect()
}

/// Peak of an envelope within `radius` frames of each frame.
fn sample(env: &[f32], frames: &[usize], radius: usize) -> Vec<f64> {
    frames
        .iter()
        .map(|&f| {
            let (a, b) = (f.saturating_sub(radius), (f + radius + 1).min(env.len()));
            if b > a {
                env[a..b].iter().cloned().fold(f32::NEG_INFINITY, f32::max) as f64
            } else {
                0.0
            }
        })
        .collect()
}

pub struct Cues {
    kick: Vec<f64>,
    snare: Vec<f64>,
    /// beats x bands, float32 as numpy has them
    timbre: Vec<Vec<f32>>,
    bass: Vec<Vec<f32>>,
    change: Vec<f64>,
    bass_change: Vec<f64>,
    near_quiet: Vec<bool>,
}

fn rows_in(freqs: &[f64], lo: f64, hi: f64) -> Vec<usize> {
    (0..freqs.len()).filter(|&r| freqs[r] >= lo && freqs[r] < hi).collect()
}

/// Mean positive rise per frame over the given rows (float32, rows added in order).
fn band_flux(m: &Matrix, rows: &[usize]) -> Vec<f32> {
    let t = m.cols;
    let mut acc = vec![0.0f32; t];
    for &r in rows {
        let row = m.row(r);
        for (j, a) in acc.iter_mut().enumerate() {
            let prev = row[j.saturating_sub(1)];
            *a += 0.0f32.max(row[j] - prev);
        }
    }
    acc.iter().map(|a| a / rows.len() as f32).collect()
}

fn norm_f32(v: &[f32]) -> f32 {
    pairwise_sum(&v.iter().map(|x| x * x).collect::<Vec<f32>>()).sqrt()
}

fn beat_cues(beat_frames: &[usize], log_mel: &Matrix, freqs: &[f64]) -> Cues {
    let n_frames = log_mel.cols;
    let frames: Vec<usize> = beat_frames.iter().map(|&f| f.min(n_frames - 1)).collect();
    let spectra = |lo: f64, hi: f64| -> Vec<Vec<f32>> {
        let rows = rows_in(freqs, lo, hi);
        let last = frames[frames.len() - 1];
        let step = (last as i64 - frames[frames.len() - 2] as i64).max(1) as usize;
        let mut edges = frames.clone();
        edges.push(n_frames.min(last + step));
        edges
            .windows(2)
            .map(|w| {
                let (a, b) = (w[0], w[1].max(w[0] + 1));
                rows.iter().map(|&r| mean_f32(&log_mel.row(r)[a..b.min(n_frames)])).collect()
            })
            .collect()
    };
    let timbre = spectra(60.0, 5000.0);
    let bass = spectra(30.0, 250.0);
    let changes = |x: &Vec<Vec<f32>>| -> Vec<f64> {
        let mut out = vec![0.0];
        for w in x.windows(2) {
            let d: Vec<f32> = w[1].iter().zip(&w[0]).map(|(a, b)| a - b).collect();
            out.push(norm_f32(&d) as f64);
        }
        out
    };
    let mut change = changes(&timbre);
    let mut bass_change = changes(&bass);
    let level: Vec<f32> = timbre.iter().map(|v| mean_f32(v)).collect();
    let thr = 0.5f32 * median_f32(&level);
    let quiet: Vec<bool> = level.iter().map(|&l| l < thr).collect();
    let n = quiet.len();
    let mut near: Vec<bool> =
        (0..n).map(|i| quiet[i] || (i > 0 && quiet[i - 1]) || (i + 1 < n && quiet[i + 1])).collect();
    for (i, &q) in quiet.iter().enumerate() {
        if q {
            for v in near.iter_mut().take((i + 5).min(n)).skip(i.saturating_sub(4)) {
                *v = true;
            }
        }
    }
    for i in 0..n {
        if near[i] {
            change[i] = 0.0;
            bass_change[i] = 0.0;
        }
    }
    Cues {
        kick: sample(&band_flux(log_mel, &rows_in(freqs, 30.0, 150.0)), &frames, 2),
        snare: sample(&band_flux(log_mel, &rows_in(freqs, 180.0, 2500.0)), &frames, 2),
        timbre,
        bass,
        change,
        bass_change,
        near_quiet: near,
    }
}

fn per_phase(v: &[f64], m: usize) -> Vec<f64> {
    (0..m).map(|p| strided_mean(v, p, m)).collect()
}

fn downbeat_phase(beat_frames: &[usize], log_mel: &Matrix, freqs: &[f64], m: usize, cues: Option<&Cues>) -> usize {
    if beat_frames.len() < MIN_BEATS || log_mel.cols < 2 {
        return 0;
    }
    let owned;
    let c = match cues {
        Some(c) => c,
        None => {
            owned = beat_cues(beat_frames, log_mel, freqs);
            &owned
        }
    };
    let n = c.timbre.len();
    let mut bar_change = vec![0.0f64; n];
    let bands = c.timbre[0].len();
    let block_mean = |a: usize, b: usize| -> Vec<f32> {
        (0..bands)
            .map(|j| {
                let mut s = 0.0f32;
                for row in &c.timbre[a..b] {
                    s += row[j];
                }
                s / (b - a) as f32
            })
            .collect()
    };
    if n >= m {
        for (i, bc) in bar_change.iter_mut().enumerate().take(n - m + 1).skip(m) {
            let (x, y) = (block_mean(i, i + m), block_mean(i - m, i));
            let d: Vec<f32> = x.iter().zip(&y).map(|(a, b)| a - b).collect();
            let mut dot = 0.0f32;
            for v in &d {
                dot += v * v;
            }
            *bc = dot.sqrt() as f64;
        }
    }
    for (bc, &q) in bar_change.iter_mut().zip(&c.near_quiet) {
        if q {
            *bc = 0.0;
        }
    }
    let sn = per_phase(&c.snare, m);
    let backbeat: Vec<f64> = if m == 4 {
        (0..4).map(|p| sn[(p + 1) % 4] + sn[(p + 3) % 4] - sn[p] - sn[(p + 2) % 4]).collect()
    } else {
        let total: f64 = sn.iter().sum();
        sn.iter().map(|s| (total - s) / (m as f64 - 1.0) - s).collect()
    };
    let mut total = vec![0.0f64; m];
    for sc in [
        per_phase(&c.kick, m),
        backbeat,
        per_phase(&c.change, m),
        per_phase(&c.bass_change, m),
        per_phase(&bar_change, m),
    ] {
        let (mu, sd) = (mean(&sc), std(&sc));
        for (t, s) in total.iter_mut().zip(&sc) {
            *t += (s - mu) / (sd + 1e-9);
        }
    }
    let best = argmax(&total);
    if total[best] - total[0] > 1.0 {
        best
    } else {
        0
    }
}

fn meter_score(beat_frames: &[usize], log_mel: &Matrix, freqs: &[f64], cues: Option<&Cues>) -> f64 {
    if beat_frames.len() < 2 * MIN_BEATS || log_mel.cols < 2 {
        return 0.0;
    }
    let owned;
    let c = match cues {
        Some(c) => c,
        None => {
            owned = beat_cues(beat_frames, log_mel, freqs);
            &owned
        }
    };
    let lag_ratio = |x: &Vec<Vec<f32>>| -> f64 {
        let d = |k: usize| -> f32 {
            let norms: Vec<f32> = (k..x.len())
                .map(|i| norm_f32(&x[i].iter().zip(&x[i - k]).map(|(a, b)| a - b).collect::<Vec<f32>>()))
                .collect();
            mean_f32(&norms)
        };
        let (d3, d4, d6, d8) = (d(3), d(4), d(6), d(8));
        libm::logf((d4 + d8 + 1e-9) / (d3 + d6 + 1e-9)) as f64
    };
    let spread = |v: &[f64], m: usize| -> f64 { std(&per_phase(v, m)) / (std(v) + 1e-9) };
    let mut total = 0.0;
    for (k, s) in METER_CUE_SCALES {
        let raw = match k {
            "timbre_lag" => lag_ratio(&c.timbre),
            "bass_lag" => lag_ratio(&c.bass),
            _ => {
                let v = match k {
                    "kick" => &c.kick,
                    "snare" => &c.snare,
                    "change" => &c.change,
                    _ => &c.bass_change,
                };
                spread(v, 3) - spread(v, 4)
            }
        };
        total += raw / s;
    }
    total
}

/// _swing: where the off-beat 16th lands inside each beat, as (percent, confidence).
fn swing(onset: &[f32], beats: &[usize]) -> (f64, f64) {
    let (mut pos, mut w) = (Vec::new(), Vec::new());
    for p in beats.windows(2) {
        let (a, b) = (p[0], p[1]);
        let length = b as i64 - a as i64;
        if length < 8 {
            continue;
        }
        let lo = a + (length as f64 * 0.18) as usize;
        let hi = (a + (length as f64 * 0.42) as usize).min(onset.len());
        if hi <= lo {
            continue;
        }
        let win = &onset[lo..hi];
        let mut k = 0;
        for (i, &v) in win.iter().enumerate() {
            if v > win[k] {
                k = i;
            }
        }
        let mut offset = k as f64;
        if k > 0 && k < win.len() - 1 {
            let (l, c, r) = (win[k - 1], win[k], win[k + 1]);
            let denom = l - 2.0 * c + r;
            if denom < 0.0 {
                offset += (0.5f32 * (l - r) / denom).clamp(-0.5, 0.5) as f64;
            }
        }
        pos.push((lo as f64 + offset - a as f64) / length as f64);
        w.push(win[k] as f64);
    }
    let wsum = pairwise_sum(&w);
    if pos.is_empty() || w.iter().sum::<f64>() == 0.0 {
        return (50.0, 0.0);
    }
    let pw: Vec<f64> = pos.iter().zip(&w).map(|(p, w)| p * w).collect();
    let avg = pairwise_sum(&pw) / wsum;
    let dev: Vec<f64> = pos.iter().zip(&w).map(|(p, w)| (p - avg) * (p - avg) * w).collect();
    let spread = libm::sqrt(pairwise_sum(&dev) / wsum);
    (clip(avg / 0.5 * 100.0, 50.0, 75.0), clip(1.0 - spread / 0.08, 0.0, 1.0))
}

fn bar_energy_series(downbeats: &[f64], rms_db: &[f32], sr: f64, duration: f64) -> (Vec<f64>, Vec<f64>) {
    let mut edges = downbeats.to_vec();
    edges.push(duration);
    let raw: Vec<f64> = edges
        .windows(2)
        .map(|w| {
            let fa = (w[0] * sr / HOP as f64) as usize;
            let fb = ((w[1] * sr / HOP as f64) as usize).max(fa + 1);
            let seg = if fa < rms_db.len() { &rms_db[fa..fb.min(rms_db.len())] } else { &rms_db[rms_db.len() - 1..] };
            mean_f32(seg) as f64
        })
        .collect();
    let (lo, hi) = (percentile(&raw, 5.0), percentile(&raw, 95.0));
    let norm: Vec<f64> = if hi - lo < 1e-9 {
        vec![0.5; raw.len()]
    } else {
        raw.iter().map(|v| clip((v - lo) / (hi - lo), 0.0, 1.0)).collect()
    };
    (downbeats.iter().map(|&t| round_dec(t, 3)).collect(), norm.iter().map(|&v| round_dec(v, 3)).collect())
}
