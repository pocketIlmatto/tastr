//! Rough section detection (tastr/engine/stages/sections.py): temporally constrained clustering of per-bar
//! timbre, harmony and loudness, snapped to 2-bar phrases, labelled from each section's relative energy.

use super::{Ctx, HOP};
use crate::card::{Card, Section};
use crate::features::{power_to_db, Matrix};
use crate::np::{self, clip, interp, pairwise_sum, round_dec, round_half_even};
use crate::segment;

const FALLBACK_BAR_SECONDS: f64 = 8.0;
const PHRASE_BARS: usize = 2;
const MIN_BARS_FOR_SPLIT: usize = 8;
const SECONDS_PER_SECTION: f64 = 18.0;
const MIN_SECTIONS: f64 = 3.0;
const MAX_SECTIONS: f64 = 12.0;
const LETTER_DISTANCE: f64 = 0.55;

pub fn run(ctx: &mut Ctx, card: &mut Card) -> Result<(), String> {
    let duration = if card.duration != 0.0 { card.duration } else { ctx.y.len() as f64 / ctx.sr };
    let bar_times = bar_times(&card.core.downbeat_times, duration);
    let n_bars = bar_times.len();
    let bar_energy = bar_energy(ctx, card, &bar_times, duration);
    if n_bars < MIN_BARS_FOR_SPLIT {
        card.sections = vec![Section {
            start: 0.0,
            end: round_dec(duration, 3),
            label: "full".into(),
            letter: Some("A".into()),
            start_bar: Some(1),
            end_bar: Some(n_bars.max(1) as i64),
            energy: Some(if bar_energy.is_empty() { 0.5 } else { round_dec(np::mean(&bar_energy), 3) }),
            confidence: Some(0.0),
        }];
        set_features(card);
        return Ok(());
    }

    let z = bar_vectors(ctx, &bar_times, duration);
    let starts = segment_bars(&z, duration, n_bars);
    let mut edges = starts.clone();
    edges.push(n_bars);
    let spans: Vec<(usize, usize)> = (0..starts.len()).map(|i| (edges[i], edges[i + 1])).collect();
    let mut sections: Vec<Section> = spans
        .iter()
        .enumerate()
        .map(|(i, &(a, b))| Section {
            start: if i == 0 { 0.0 } else { round_dec(bar_times[a], 3) },
            end: if i == spans.len() - 1 { round_dec(duration, 3) } else { round_dec(bar_times[b], 3) },
            label: String::new(),
            letter: None,
            start_bar: Some(a as i64 + 1),
            end_bar: Some(b as i64),
            energy: Some(round_dec(np::mean(&bar_energy[a..b]), 3)),
            confidence: None,
        })
        .collect();
    let vecs: Vec<Vec<f32>> = spans.iter().map(|&(a, b)| col_mean(&z, a, b)).collect();
    for (s, letter) in sections.iter_mut().zip(letters(&vecs)) {
        s.letter = Some(letter);
    }
    let electronic = is_electronic(ctx, card);
    for i in 0..sections.len() {
        sections[i].label = label(i, &sections, electronic).into();
    }
    for (s, c) in sections.iter_mut().zip(confidences(&vecs)) {
        s.confidence = Some(c);
    }
    card.sections = sections;
    set_features(card);
    Ok(())
}

fn bar_times(downbeats: &[f64], duration: f64) -> Vec<f64> {
    let t: Vec<f64> = downbeats.iter().cloned().filter(|&d| (0.0..duration).contains(&d)).collect();
    if t.len() >= 2 {
        return t;
    }
    // np.arange(0, duration, 8.0)
    let n = libm::ceil(duration / FALLBACK_BAR_SECONDS).max(0.0) as usize;
    (0..n).map(|i| i as f64 * FALLBACK_BAR_SECONDS).collect()
}

/// Frame range [a, b) per bar; the first bar is stretched back to frame 0 and the last to the end.
fn bar_spans(sr: f64, bar_times: &[f64], duration: f64, n_frames: usize) -> Vec<(usize, usize)> {
    let mut frames: Vec<usize> = bar_times
        .iter()
        .chain(std::iter::once(&duration))
        .map(|&t| clip(round_half_even(t * sr / HOP as f64), 0.0, n_frames as f64) as usize)
        .collect();
    frames[0] = 0;
    let last = frames.len() - 1;
    frames[last] = n_frames;
    (0..bar_times.len()).map(|i| (frames[i], frames[i + 1].max(frames[i] + 1))).collect()
}

fn scale01(x: &[f64]) -> Vec<f64> {
    if x.is_empty() {
        return Vec::new();
    }
    let (lo, hi) = (np::percentile(x, 5.0), np::percentile(x, 95.0));
    if hi - lo < 1e-9 {
        return vec![0.5; x.len()];
    }
    x.iter().map(|v| clip((v - lo) / (hi - lo), 0.0, 1.0)).collect()
}

fn amplitude_db(rms: &[f32]) -> Vec<f32> {
    let power: Vec<f32> = rms.iter().map(|v| v * v).collect();
    power_to_db(&power, 1.0, Some(80.0))
}

fn bar_energy(ctx: &Ctx, card: &Card, bar_times: &[f64], duration: f64) -> Vec<f64> {
    if let Some((times, values)) = card.energy_series() {
        if times.len() >= 2 {
            let mids: Vec<f64> = (0..bar_times.len())
                .map(|i| {
                    let end = if i + 1 < bar_times.len() { bar_times[i + 1] } else { duration };
                    (bar_times[i] + end) / 2.0
                })
                .collect();
            let raw: Vec<f64> = mids.iter().map(|&m| interp(m, times, values)).collect();
            return scale01(&raw);
        }
    }
    let db = amplitude_db(&ctx.rms);
    let onset = &ctx.onset;
    let raw: Vec<f64> = bar_spans(ctx.sr, bar_times, duration, db.len())
        .into_iter()
        .map(|(a, b)| {
            let d = np::mean_f32(&db[a..b.min(db.len())]);
            let o = if a < onset.len() && b > a { np::mean_f32(&onset[a..b.min(onset.len())]) } else { 0.0 };
            (d + 3.0 * o) as f64
        })
        .collect();
    scale01(&raw)
}

/// Z-scored per-bar features (chroma 12 + MFCC 2-13 + loudness), features x bars, in float32.
fn bar_vectors(ctx: &Ctx, bar_times: &[f64], duration: f64) -> Matrix {
    let db = amplitude_db(&ctx.rms);
    let n = ctx.chroma.cols.min(ctx.mfcc.cols).min(db.len());
    let mut rows: Vec<&[f32]> = (0..12).map(|r| &ctx.chroma.row(r)[..n]).collect();
    rows.extend((1..ctx.mfcc.rows).map(|r| &ctx.mfcc.row(r)[..n]));
    rows.push(&db[..n]);
    let spans = bar_spans(ctx.sr, bar_times, duration, n);
    let nb = spans.len();
    let mut data = Vec::with_capacity(rows.len() * nb);
    for row in &rows {
        let bars: Vec<f32> = spans
            .iter()
            .map(|&(a, b)| {
                let lo = a.min(n - 1);
                let hi = b.min(n).max(lo + 1);
                np::mean_f32(&row[lo..hi])
            })
            .collect();
        let m = np::mean_f32(&bars);
        let sd = np::std_f32(&bars);
        data.extend(bars.iter().map(|v| (v - m) / (sd + 1e-9f32)));
    }
    Matrix { rows: rows.len(), cols: nb, data }
}

fn target(duration: f64, n_bars: usize) -> usize {
    let k = clip(round_half_even(duration / SECONDS_PER_SECTION), MIN_SECTIONS, MAX_SECTIONS) as usize;
    k.min(2.max(n_bars / PHRASE_BARS))
}

fn boundaries(z: &Matrix, k: usize) -> Vec<usize> {
    let n_bars = z.cols;
    let mut out: Vec<usize> = segment::agglomerative(z, k).into_iter().filter(|&b| 0 < b && b < n_bars).collect();
    out.push(0);
    out.sort_unstable();
    out.dedup();
    out
}

fn segment_bars(z: &Matrix, duration: f64, n_bars: usize) -> Vec<usize> {
    let target = target(duration, n_bars);
    let mut best = snap(&boundaries(z, target), n_bars);
    for extra in 1..4 {
        if best.len() >= target || target + extra > n_bars {
            break;
        }
        let cand = snap(&boundaries(z, target + extra), n_bars);
        if cand.len() > best.len() {
            best = cand;
        }
    }
    best
}

fn snap(starts: &[usize], n_bars: usize) -> Vec<usize> {
    let p = PHRASE_BARS as f64;
    let mut snapped: Vec<usize> = starts[1..].iter().map(|&s| (round_half_even(s as f64 / p) * p) as usize).collect();
    snapped.push(0);
    snapped.sort_unstable();
    snapped.dedup();
    let mut merged = vec![0usize];
    for &s in &snapped[1..] {
        if s < n_bars && s - merged[merged.len() - 1] >= PHRASE_BARS {
            merged.push(s);
        }
    }
    if n_bars - merged[merged.len() - 1] < PHRASE_BARS && merged.len() > 1 {
        merged.pop();
    }
    merged
}

fn col_mean(z: &Matrix, a: usize, b: usize) -> Vec<f32> {
    (0..z.rows).map(|r| np::mean_f32(&z.row(r)[a..b])).collect()
}

/// np.linalg.norm(u - v) / sqrt(size) for float32 vectors.
fn dist(u: &[f32], v: &[f32]) -> f64 {
    let d: Vec<f32> = u.iter().zip(v).map(|(a, b)| a - b).collect();
    let sq: f64 = d.iter().map(|x| (*x as f64) * (*x as f64)).sum();
    (sq.sqrt() as f32) as f64 / (u.len() as f64).sqrt()
}

fn letters(vecs: &[Vec<f32>]) -> Vec<String> {
    let mut out = Vec::new();
    let mut protos: Vec<&Vec<f32>> = Vec::new();
    for v in vecs {
        let dists: Vec<f64> = protos.iter().map(|p| dist(v, p)).collect();
        let best = if dists.is_empty() { 0 } else { np::argmax(&dists.iter().map(|d| -d).collect::<Vec<_>>()) };
        if !dists.is_empty() && dists[best] < LETTER_DISTANCE {
            out.push(((b'A' + best as u8) as char).to_string());
        } else {
            protos.push(v);
            out.push(((b'A' + (protos.len() - 1).min(25) as u8) as char).to_string());
        }
    }
    out
}

fn confidences(vecs: &[Vec<f32>]) -> Vec<f64> {
    if vecs.len() < 2 {
        return vec![0.0; vecs.len()];
    }
    let gap: Vec<f64> = (1..vecs.len()).map(|i| dist(&vecs[i], &vecs[i - 1])).collect();
    std::iter::once(gap[0]).chain(gap.iter().cloned()).map(|g| round_dec(clip(g / 1.5, 0.0, 1.0), 3)).collect()
}

fn is_electronic(ctx: &Ctx, card: &Card) -> bool {
    match card.core.bpm {
        Some(bpm) if bpm != 0.0 && bpm > 105.0 => {}
        _ => return false,
    }
    let perc = card.feature("percussiveness").unwrap_or_else(|| {
        let e = |x: &[f32]| pairwise_sum(&x.iter().map(|v| v * v).collect::<Vec<f32>>()) as f64;
        let (h, p) = (e(&ctx.harmonic), e(&ctx.percussive));
        p / (h + p + 1e-9)
    });
    perc > 0.25
}

fn label(i: usize, sections: &[Section], electronic: bool) -> &'static str {
    let e = sections[i].energy.unwrap_or(0.0);
    let last = sections.len() - 1;
    let nxt = if i < last { Some(sections[i + 1].energy.unwrap_or(0.0)) } else { None };
    if i == 0 && e < 0.5 {
        return "intro";
    }
    if i == last && e < 0.5 {
        return "outro";
    }
    if e >= 0.68 {
        return if electronic { "drop" } else { "chorus" };
    }
    if let Some(n) = nxt {
        if n - e > 0.2 && n >= 0.6 {
            return "build";
        }
    }
    if e < 0.38 {
        return "breakdown";
    }
    "verse"
}

fn set_features(card: &mut Card) {
    let energies: Vec<f64> = card.sections.iter().filter_map(|s| s.energy).collect();
    let contrast = if energies.is_empty() { 0.0 } else { round_dec(np::std(&energies), 3) };
    card.set_feature("section_contrast", contrast);
    let n = card.sections.len() as f64;
    card.set_feature("section_count", n);
}
