//! "Try this in Ableton" suggestions (tastr/engine/stages/suggestions.py): drum lanes, bass and chords on the
//! bar grid, stored in extras, then turned into Suggestion entries with the Python engine's exact wording.

use super::{Ctx, HOP};
use crate::card::{Card, Section, Suggestion};
use crate::drums::{self, Drums};
use crate::features::{power_to_db, Matrix};
use crate::json::Json;
use crate::music;
use crate::np::{self, pairwise_sum, round_dec, round_half_even, round_int};

const STEPS_PER_BEAT: usize = 4;
const DRUM_RACK: [(&str, &str); 7] = [
    ("kick", "C1"),
    ("snare", "D1"),
    ("clap", "D#1"),
    ("hat", "F#1"),
    ("open_hat", "A#1"),
    ("tom", "A1"),
    ("perc", "G#1"),
];
const BASS_LOW_BINS: usize = 24;
const NO_BASS: f64 = 0.19952623149688797; // 10 ** -0.7
const NO_BASS_SECTION: f64 = 0.1778279410038923; // 10 ** -0.75
const BEAT_AGREE: f64 = 0.6;
const BEAT_MIN_REPEATS: usize = 2;
const CHORD_MIN_SCORE: f64 = 0.6;
const CORE_LANES: [&str; 3] = ["kick", "snare", "hat"];

type Pattern = &'static [(&'static str, &'static str)];

fn variations(style: &str) -> &'static [(&'static str, Pattern)] {
    match style {
        "four_floor" => &[
            (
                "Classic house/techno",
                &[
                    ("kick", "x...x...x...x..."),
                    ("clap", "....x.......x..."),
                    ("hat", "..x...x...x...x."),
                    ("open_hat", "......x.......x."),
                ],
            ),
            (
                "Rolling 16ths",
                &[
                    ("kick", "x...x...x...x..."),
                    ("clap", "....x.......x..."),
                    ("hat", "xoxoxoxoxoxoxoxo"),
                    ("perc", "...x..x....x..x."),
                ],
            ),
        ],
        "dnb" => &[
            ("Two-step", &[("kick", "x.........x....."), ("snare", "....x.......x..."), ("hat", "x.x.x.x.x.x.x.x.")]),
            (
                "Amen-ish break",
                &[("kick", "x.x.......xx...."), ("snare", "....x..x.x..x..x"), ("hat", "x.x.x.x.x.x.x.x.")],
            ),
        ],
        "halftime" => {
            &[("Halftime", &[("kick", "x.........x....."), ("snare", "........x......."), ("hat", "x.x.x.x.x.x.xxx.")])]
        }
        "hiphop" => &[
            ("Boom bap", &[("kick", "x......x..x....."), ("snare", "....x.......x..."), ("hat", "x.x.x.x.x.x.x.x.")]),
            (
                "Trap-style hats",
                &[("kick", "x.....x...x....."), ("snare", "........x......."), ("hat", "xxx.x.xxx.x.xxxx")],
            ),
        ],
        "generic" => {
            &[("Backbeat", &[("kick", "x.......x.x....."), ("snare", "....x.......x..."), ("hat", "x.x.x.x.x.x.x.x.")])]
        }
        "waltz" => &[
            ("Waltz", &[("kick", "x..........."), ("snare", "....x...x..."), ("hat", "x...x...x...")]),
            ("Jazz waltz", &[("kick", "x.......x..."), ("snare", "......x....."), ("hat", "x..xx..xx..x")]),
        ],
        _ => &[],
    }
}

fn style_label(style: &str) -> &'static str {
    match style {
        "four_floor" => "four on the floor",
        "dnb" => "drum and bass",
        "halftime" => "halftime",
        "hiphop" => "hip-hop",
        "generic" => "generic backbeat",
        _ => "3/4 waltz",
    }
}

fn style(bpm: f64, perc: Option<f64>, beats_per_bar: i64) -> &'static str {
    if beats_per_bar == 3 {
        return "waltz";
    }
    if (160.0..=185.0).contains(&bpm) {
        return "dnb";
    }
    if (136.0..=152.0).contains(&bpm) && perc.unwrap_or(0.0) > 0.25 {
        return "halftime";
    }
    if (118.0..136.0).contains(&bpm) {
        return "four_floor";
    }
    if (80.0..105.0).contains(&bpm) {
        return "hiphop";
    }
    "generic"
}

fn step_list(pattern: &str) -> Vec<usize> {
    pattern.chars().enumerate().filter(|(_, c)| *c == 'x' || *c == 'o').map(|(i, _)| i + 1).collect()
}

fn pattern(profile: &[f64]) -> String {
    profile
        .iter()
        .map(|&v| {
            if v >= 0.72 {
                'x'
            } else if v >= 0.5 {
                'o'
            } else {
                '.'
            }
        })
        .collect()
}

fn lane_help(lanes: &[&str], model: bool) -> Json {
    let mut o = Json::obj();
    for l in lanes {
        if let Some(h) = drums::lane_help(l, model) {
            o.set(l, h);
        }
    }
    o
}

fn title(text: String) -> String {
    if text.chars().count() <= 80 {
        text
    } else {
        text.chars().take(79).collect::<String>() + "…"
    }
}

fn capitalize(s: &str) -> String {
    let mut c = s.chars();
    match c.next() {
        Some(f) => f.to_uppercase().collect::<String>() + &c.as_str().to_lowercase(),
        None => String::new(),
    }
}

fn steps_in_bar(card: &Card) -> usize {
    STEPS_PER_BEAT * if card.core.beats_per_bar != 0 { card.core.beats_per_bar as usize } else { 4 }
}

/// Mean over rows of a bars x steps table (numpy's axis-0 mean adds the rows in order).
fn mean_rows(rows: &[&[f64]], width: usize) -> Vec<f64> {
    let mut acc = vec![0.0f64; width];
    for r in rows {
        for (a, v) in acc.iter_mut().zip(r.iter()) {
            *a += v;
        }
    }
    acc.iter().map(|a| a / rows.len() as f64).collect()
}

fn fmax(x: &[f64]) -> f64 {
    x.iter().cloned().fold(f64::NEG_INFINITY, f64::max)
}

/// The most common value (Counter.most_common(1): ties go to the first seen).
fn most_common<T: PartialEq + Clone>(items: &[T]) -> Option<(T, usize)> {
    let mut counts: Vec<(T, usize)> = Vec::new();
    for it in items {
        if let Some(c) = counts.iter_mut().find(|(v, _)| v == it) {
            c.1 += 1;
        } else {
            counts.push((it.clone(), 1));
        }
    }
    let mut best: Option<(T, usize)> = None;
    for (v, c) in counts {
        if best.as_ref().is_none_or(|(_, bc)| c > *bc) {
            best = Some((v, c));
        }
    }
    best
}

fn counts_desc<T: PartialEq + Clone>(items: &[T]) -> Vec<(T, usize)> {
    let mut counts: Vec<(T, usize)> = Vec::new();
    for it in items {
        if let Some(c) = counts.iter_mut().find(|(v, _)| v == it) {
            c.1 += 1;
        } else {
            counts.push((it.clone(), 1));
        }
    }
    counts.sort_by_key(|c| std::cmp::Reverse(c.1)); // stable, like Counter.most_common
    counts
}

// --- Audio analysis --------------------------------------------------------------

/// Frame of each 16th note for every complete bar (bars x steps).
fn step_grid(card: &Card, sr: f64, n_frames: usize) -> Vec<Vec<i64>> {
    let down = &card.core.downbeat_times;
    let m = if card.core.beats_per_bar != 0 { card.core.beats_per_bar as usize } else { 4 };
    if down.len() < 2 {
        return Vec::new();
    }
    let beats = &card.core.beat_times;
    let hi = (n_frames as i64 - 1).max(0);
    down.windows(2)
        .map(|w| {
            let (a, b) = (w[0], w[1]);
            let in_bar: Vec<f64> = beats.iter().cloned().filter(|&t| t >= a - 1e-6 && t < b - 1e-6).collect();
            let edges: Vec<f64> = if in_bar.len() == m {
                in_bar.iter().cloned().chain(std::iter::once(b)).collect()
            } else {
                let step = (b - a) / m as f64;
                (0..=m).map(|i| if i == m { b } else { i as f64 * step + a }).collect()
            };
            let mut row = Vec::with_capacity(m * STEPS_PER_BEAT);
            for i in 0..m {
                for s in 0..STEPS_PER_BEAT {
                    let t = edges[i] + (edges[i + 1] - edges[i]) * s as f64 / STEPS_PER_BEAT as f64;
                    row.push((round_half_even(t * sr / HOP as f64) as i64).clamp(0, hi));
                }
            }
            row
        })
        .collect()
}

fn sample(env: &[f32], frames: &[Vec<i64>], radius: i64) -> Vec<Vec<f64>> {
    frames
        .iter()
        .map(|row| {
            row.iter()
                .map(|&f| {
                    let a = (f - radius).max(0) as usize;
                    let b = ((f + radius + 1).max(0) as usize).min(env.len());
                    if b > a {
                        env[a..b].iter().cloned().fold(f32::NEG_INFINITY, f32::max) as f64
                    } else {
                        0.0
                    }
                })
                .collect()
        })
        .collect()
}

fn bar_energy(ctx: &Ctx, card: &Card, sf: &[Vec<i64>]) -> Vec<f64> {
    let n = sf.len();
    if let Some((_, values)) = card.energy_series() {
        if values.len() >= n {
            return values[..n].to_vec();
        }
    }
    let rms = &ctx.rms;
    (0..n)
        .map(|i| {
            let a = sf[i][0] as usize;
            let b = if i + 1 < n { sf[i + 1][0] as usize } else { rms.len() };
            let b = b.max(a + 1).min(rms.len());
            np::mean_f32(&rms[a.min(b)..b]) as f64
        })
        .collect()
}

struct Lane {
    name: &'static str,
    pattern: String,
    velocity: Vec<f64>,
    detected: bool,
    steps: Vec<Vec<f64>>,
    peak: f64,
}

/// The bars at or above the median energy (every bar when fewer than two are).
fn busy_bars(energies: &[f64]) -> Vec<bool> {
    let med = np::median(energies);
    let busy: Vec<bool> = energies.iter().map(|&e| e >= med).collect();
    if busy.iter().filter(|b| **b).count() < 2 {
        return vec![true; energies.len()];
    }
    busy
}

/// The frames halfway between each step and the next (past the last, half a step on): where the floor is read.
fn between_steps(sf: &[Vec<i64>]) -> Vec<i64> {
    sf.iter()
        .flat_map(|row| {
            let w = row.len();
            (0..w)
                .map(|j| {
                    if j + 1 < w {
                        (row[j] + row[j + 1]).div_euclid(2)
                    } else {
                        row[w - 1] + (row[w - 1] - row[w - 2]).div_euclid(2)
                    }
                })
                .collect::<Vec<_>>()
        })
        .collect()
}

/// One lane's grid from its activations on the steps and between them, over the busy bars.
fn lane_grid(name: &'static str, acts: &[f64], floor_acts: &[f64], busy: &[bool], width: usize) -> Lane {
    let steps: Vec<Vec<f64>> = acts.chunks(width).map(|r| r.iter().map(|v| v.sqrt()).collect()).collect();
    let busy_rows: Vec<&[f64]> = steps.iter().zip(busy).filter(|(_, b)| **b).map(|(r, _)| r.as_slice()).collect();
    let profile = mean_rows(&busy_rows, width);
    let floor_flat: Vec<f64> =
        floor_acts.chunks(width).zip(busy).filter(|(_, b)| **b).flat_map(|(r, _)| r.iter().map(|v| v.sqrt())).collect();
    let floor = pairwise_sum(&floor_flat) / floor_flat.len() as f64;
    let peak = fmax(&profile);
    let norm: Vec<f64> = if peak > 0.0 { profile.iter().map(|v| v / peak).collect() } else { profile.clone() };
    Lane {
        name,
        pattern: pattern(&norm),
        velocity: norm.iter().map(|&v| round_dec(v, 3)).collect(),
        detected: peak > 0.0 && peak >= 3.0 * floor,
        steps,
        peak,
    }
}

/// Lanes beyond the core ones that are much quieter than the loudest lane aren't detected.
fn drop_quiet_extras(grid: &mut [Lane]) {
    let top = grid.iter().map(|g| g.peak).fold(f64::NEG_INFINITY, f64::max);
    for g in grid.iter_mut() {
        if !CORE_LANES.contains(&g.name) && g.peak < 0.2 * top {
            g.detected = false;
        }
    }
}

/// A quiet snare, clap or perc lane that just follows the kick is the kick bleeding in.
fn drop_kick_echoes(grid: &mut [Lane]) {
    let (kick_peak, kick_vel) = (grid[0].peak, grid[0].velocity.clone());
    for g in grid.iter_mut() {
        if !["snare", "clap", "perc"].contains(&g.name) {
            continue;
        }
        if g.detected && kick_peak > 0.0 && g.peak < 0.35 * kick_peak {
            let (a, b) = (&g.velocity, &kick_vel);
            if np::std(a) > 0.0 && np::std(b) > 0.0 && corrcoef(a, b) > 0.9 {
                g.detected = false;
            }
        }
    }
}

fn drum_grid(ctx: &Ctx, card: &Card, drums: &Drums, sf: &[Vec<i64>]) -> Result<Vec<Lane>, String> {
    let busy = busy_bars(&bar_energy(ctx, card, sf));
    let step_s = card.core.bpm.filter(|&b| b != 0.0).map(|b| 15.0 / b);
    let width = sf[0].len();
    let flat: Vec<i64> = sf.iter().flatten().cloned().collect();
    let acts = drums.lane_activations(&flat, &ctx.env_mid, &ctx.env_air, ctx.sr, HOP, step_s, 2)?;
    let floor_acts = drums.lane_activations(&between_steps(sf), &ctx.env_mid, &ctx.env_air, ctx.sr, HOP, step_s, 0)?;
    let mut grid: Vec<Lane> = (drums.lanes().into_iter().enumerate())
        .map(|(li, lane)| lane_grid(lane, &acts[li], &floor_acts[li], &busy, width))
        .collect();
    drop_quiet_extras(&mut grid);
    drop_kick_echoes(&mut grid);
    Ok(grid)
}

fn corrcoef(a: &[f64], b: &[f64]) -> f64 {
    let (ma, mb) = (np::mean(a), np::mean(b));
    let (mut sab, mut saa, mut sbb) = (0.0, 0.0, 0.0);
    for (x, y) in a.iter().zip(b) {
        let (dx, dy) = (x - ma, y - mb);
        sab += dx * dy;
        saa += dx * dx;
        sbb += dy * dy;
    }
    let f = 1.0 / (a.len() as f64 - 1.0);
    (sab * f / (saa * f).sqrt() / (sbb * f).sqrt()).clamp(-1.0, 1.0)
}

struct SecMeta {
    idx: usize,
    label: String,
    letter: Option<String>,
    start_bar: i64,
    end_bar: Option<i64>,
    energy: Option<f64>,
}

impl SecMeta {
    fn json(&self) -> Json {
        Json::obj()
            .with("idx", self.idx)
            .with("label", self.label.clone())
            .with("letter", self.letter.clone())
            .with("start_bar", self.start_bar)
            .with("end_bar", self.end_bar)
    }

    fn span(&self, n_bars: usize) -> (usize, usize) {
        let a = (self.start_bar - 1).max(0) as usize;
        let b = (n_bars as i64).min(self.end_bar.unwrap_or(self.start_bar)).max(0) as usize;
        (a, b)
    }
}

fn section_grooves(grid: &[Lane], energies: &[f64], secs: &[SecMeta]) -> Vec<(Json, Vec<(&'static str, String)>)> {
    let mut out = Vec::new();
    for sec in secs {
        let (a, b) = sec.span(energies.len());
        if b <= a {
            continue;
        }
        let med = np::median(&energies[a..b]);
        let busy: Vec<bool> = energies[a..b].iter().map(|&e| e >= med).collect();
        let mut pat = Vec::new();
        for g in grid {
            let width = g.steps[0].len();
            if g.peak <= 0.0 {
                pat.push((g.name, ".".repeat(width)));
                continue;
            }
            let rows: Vec<&[f64]> =
                g.steps[a..b].iter().zip(&busy).filter(|(_, k)| **k).map(|(r, _)| r.as_slice()).collect();
            let profile: Vec<f64> = mean_rows(&rows, width).iter().map(|v| (v / g.peak).min(1.0)).collect();
            pat.push((g.name, pattern(&profile)));
        }
        out.push((sec.json().with("energy", sec.energy), pat));
    }
    out
}

fn common_loop<T: PartialEq + Clone>(seq: &[T], length: usize) -> Option<Vec<T>> {
    if seq.len() < length {
        return None;
    }
    let windows: Vec<Vec<T>> = (0..=seq.len() - length).step_by(length).map(|i| seq[i..i + length].to_vec()).collect();
    most_common(&windows).map(|(w, _)| w)
}

fn bar_slices(sf: &[Vec<i64>], n_frames: usize) -> Vec<(usize, usize)> {
    (0..sf.len())
        .map(|i| {
            let a = sf[i][0] as usize;
            let b = if i + 1 < sf.len() { sf[i + 1][0] as usize } else { n_frames };
            (a, b.max(a + 1))
        })
        .collect()
}

/// Sum of row r of a matrix over columns a..b, as numpy slices (clamped).
fn row_sum(m: &Matrix, r: usize, a: usize, b: usize) -> f32 {
    let row = m.row(r);
    let b = b.min(row.len());
    if a >= b {
        return 0.0;
    }
    pairwise_sum(&row[a..b])
}

fn argmax_f32(v: &[f32]) -> usize {
    let mut best = 0;
    for (i, &x) in v.iter().enumerate() {
        if x > v[best] {
            best = i;
        }
    }
    best
}

fn pc_of(m: &Matrix, a: usize, b: usize) -> Option<i64> {
    let vec: Vec<f32> = (0..12).map(|r| row_sum(m, r, a, b)).collect();
    if pairwise_sum(&vec) > 0.0 {
        Some(argmax_f32(&vec) as i64)
    } else {
        None
    }
}

struct Bass {
    notes: Vec<String>,
    lp: Vec<String>,
    degrees: Vec<Option<String>>,
    movement: &'static str,
    rhythm: Option<String>,
    bar_notes: Vec<Option<String>>,
    low: Vec<f64>,
    reference: f64,
    flux: Vec<Vec<f64>>,
    beats: Vec<Vec<Option<i64>>>,
}

fn degrees(notes: &[Option<String>], tonic: Option<i64>, scale: &str) -> Vec<Option<String>> {
    let Some(tonic) = tonic else { return Vec::new() };
    notes
        .iter()
        .map(|n| {
            n.as_ref().map(|n| match music::degree_of(music::note_index(n), tonic, scale) {
                Some(d) => (d + 1).to_string(),
                None => format!("({n})"),
            })
        })
        .collect()
}

/// The bass CQT folded to 12 pitch classes: the octaves added in order (reshape(3, 12, -1).sum(axis=0)).
fn bass_chroma(cqt: &Matrix) -> Matrix {
    let t = cqt.cols;
    let mut chroma = Matrix { rows: 12, cols: t, data: cqt.data[..12 * t].to_vec() };
    for o in 1..cqt.rows / 12 {
        for (c, v) in chroma.data.iter_mut().zip(&cqt.data[o * 12 * t..(o + 1) * 12 * t]) {
            *c += v;
        }
    }
    chroma
}

/// Power in the lowest BASS_LOW_BINS bins, per frame.
fn low_power(cqt: &Matrix) -> Vec<f32> {
    let mut low_power = vec![0.0f32; cqt.cols];
    for r in 0..BASS_LOW_BINS {
        for (p, v) in low_power.iter_mut().zip(cqt.row(r)) {
            *p += v * v;
        }
    }
    low_power
}

/// Per bar: its pitch class, its mean low power (NaN for an empty bar) and each beat's pitch class.
#[allow(clippy::type_complexity)]
fn bar_pitches(
    chroma: &Matrix,
    low_power: &[f32],
    sf: &[Vec<i64>],
) -> (Vec<Option<i64>>, Vec<f64>, Vec<Vec<Option<i64>>>) {
    let t = chroma.cols;
    let beats = sf[0].len() / STEPS_PER_BEAT;
    let (mut all_pcs, mut low, mut beat_pcs) = (Vec::new(), Vec::new(), Vec::new());
    for (i, (a, b)) in bar_slices(sf, t).into_iter().enumerate() {
        all_pcs.push(pc_of(chroma, a, b));
        let bb = b.min(t);
        low.push(if a < bb { np::mean_f32(&low_power[a..bb]) as f64 } else { f64::NAN });
        let row: Vec<Option<i64>> = (0..beats)
            .map(|k| {
                let fa = sf[i][k * STEPS_PER_BEAT + 1] as usize;
                let fb = if k + 1 < beats { sf[i][(k + 1) * STEPS_PER_BEAT] as usize } else { b };
                pc_of(chroma, fa, fb.max(fa + 1))
            })
            .collect();
        beat_pcs.push(row);
    }
    (all_pcs, low, beat_pcs)
}

/// How the bass line moves: "pedal", "static", "stepwise", "fourths/fifths" or "leaps".
fn movement(pcs: &[i64]) -> &'static str {
    let (_, most_count) = most_common(pcs).unwrap();
    let intervals: Vec<i64> = pcs
        .windows(2)
        .filter(|w| w[0] != w[1])
        .map(|w| (w[1] - w[0]).rem_euclid(12).min((w[0] - w[1]).rem_euclid(12)))
        .collect();
    let share = |f: &dyn Fn(i64) -> bool| intervals.iter().filter(|&&iv| f(iv)).count() as f64 / intervals.len() as f64;
    if most_count as f64 / pcs.len() as f64 > 0.7 {
        "pedal"
    } else if intervals.is_empty() {
        "static"
    } else if share(&|iv| iv <= 2) > 0.6 {
        "stepwise"
    } else if share(&|iv| iv == 5 || iv == 7) > 0.5 {
        "fourths/fifths"
    } else {
        "leaps"
    }
}

/// Spectral flux of the bass CQT in dB, averaged over its bins, per frame.
fn bass_flux(cqt: &Matrix) -> Vec<f32> {
    let t = cqt.cols;
    let power: Vec<f32> = cqt.data.iter().map(|v| v * v + 1e-10f32).collect();
    let log_s = power_to_db(&power, 1.0, Some(80.0));
    let mut flux = vec![0.0f32; t];
    for r in 0..cqt.rows {
        let row = &log_s[r * t..(r + 1) * t];
        for c in 0..t {
            let d = if c == 0 { 0.0 } else { row[c] - row[c - 1] };
            flux[c] += d.max(0.0);
        }
    }
    for v in flux.iter_mut() {
        *v /= cqt.rows as f32;
    }
    flux
}

fn bass_summary(ctx: &Ctx, sf: &[Vec<i64>], tonic: Option<i64>, scale: &str) -> Option<Bass> {
    let cqt = &ctx.bass_cqt;
    let (all_pcs, low, beat_pcs) = bar_pitches(&bass_chroma(cqt), &low_power(cqt), sf);
    let reference = if low.is_empty() { 0.0 } else { np::percentile(&low, 90.0) };
    let bar_pcs: Vec<Option<i64>> =
        all_pcs.iter().zip(&low).map(|(p, &v)| p.filter(|_| reference > 0.0 && v >= NO_BASS * reference)).collect();
    let pcs: Vec<i64> = all_pcs.iter().flatten().cloned().collect();
    if pcs.is_empty() {
        return None;
    }
    let notes: Vec<String> = pcs.iter().map(|&p| music::note_name(p).to_string()).collect();
    let lp = common_loop(&notes, 4).unwrap_or_else(|| notes[..notes.len().min(4)].to_vec());
    let movement = movement(&pcs);
    let bar_flux = sample(&bass_flux(cqt), sf, 2);
    let rows: Vec<&[f64]> = bar_flux.iter().map(|r| r.as_slice()).collect();
    let prof = mean_rows(&rows, sf[0].len());
    let pm = fmax(&prof);
    let rhythm = if pm > 0.0 { Some(pattern(&prof.iter().map(|v| v / pm).collect::<Vec<_>>())) } else { None };
    let lp_opt: Vec<Option<String>> = lp.iter().cloned().map(Some).collect();
    Some(Bass {
        degrees: degrees(&lp_opt, tonic, scale),
        notes,
        lp,
        movement,
        rhythm,
        bar_notes: bar_pcs.iter().map(|p| p.map(|p| music::note_name(p).to_string())).collect(),
        low,
        reference,
        flux: bar_flux,
        beats: beat_pcs,
    })
}

fn loop_beats(
    lp: &[Option<String>],
    notes: &[Option<String>],
    beat_pcs: &[Vec<Option<i64>>],
    a: usize,
    b: usize,
) -> Option<Vec<Option<Vec<String>>>> {
    let mut out = Vec::new();
    for (k, name) in lp.iter().enumerate() {
        let rows: Vec<&Vec<Option<i64>>> = (a + k..b)
            .step_by(lp.len())
            .filter(|&i| name.is_some() && notes[i] == *name)
            .map(|i| &beat_pcs[i])
            .collect();
        let mut row = None;
        if rows.len() >= BEAT_MIN_REPEATS {
            let width = rows.iter().map(|r| r.len()).min().unwrap_or(0);
            let picks: Vec<Option<String>> = (0..width)
                .map(|j| {
                    let beat: Vec<Option<i64>> = rows.iter().map(|r| r[j]).collect();
                    let (pc, n) = most_common(&beat).unwrap();
                    match pc {
                        Some(pc) if n as f64 / rows.len() as f64 >= BEAT_AGREE => {
                            Some(music::note_name(pc).to_string())
                        }
                        _ => None,
                    }
                })
                .collect();
            if picks.iter().all(|p| p.is_some()) {
                let picks: Vec<String> = picks.into_iter().flatten().collect();
                let mut uniq = picks.clone();
                uniq.sort();
                uniq.dedup();
                if uniq.len() > 1 {
                    row = Some(picks);
                }
            }
        }
        out.push(row);
    }
    if out.iter().any(|r| r.is_some()) {
        Some(out)
    } else {
        None
    }
}

struct Harmony {
    progression: Vec<String>,
    roman: Vec<String>,
    chords: Vec<Json>,
    chord_names: Vec<(String, String, String, String, usize)>,
    bar_chords: Vec<Option<String>>,
    lookup: Vec<(String, i64, &'static str)>,
}

fn harmony_summary(ctx: &Ctx, sf: &[Vec<i64>], tonic: i64, scale: &str) -> Option<Harmony> {
    let chroma = &ctx.chroma;
    let bars: Vec<music::Chord> = bar_slices(sf, chroma.cols)
        .into_iter()
        .map(|(a, b)| {
            let b = b.min(chroma.cols);
            let mut v = [0.0f64; 12];
            for (r, x) in v.iter_mut().enumerate() {
                *x = if a < b { np::mean_f32(&chroma.row(r)[a..b]) as f64 } else { f64::NAN };
            }
            music::match_chord(&v)
        })
        .collect();
    let ok = |c: &music::Chord| c.score >= CHORD_MIN_SCORE && c.root.is_some();
    let confident: Vec<&music::Chord> = bars.iter().filter(|c| ok(c)).collect();
    if confident.is_empty() {
        return None;
    }
    let names: Vec<String> = bars.iter().map(|c| c.name.clone()).collect();
    let bar_chords: Vec<Option<String>> =
        bars.iter().map(|c| if ok(c) { Some(c.name.clone()) } else { None }).collect();
    let progression = common_loop(&names, 4).unwrap_or_else(|| names[..names.len().min(4)].to_vec());
    // lookup: name -> the last confident match with that name (a dict comprehension keeps the last)
    let mut lookup: Vec<(String, i64, &'static str)> = Vec::new();
    for c in &confident {
        let entry = (c.name.clone(), c.root.unwrap(), c.quality.unwrap());
        if let Some(slot) = lookup.iter_mut().find(|(n, _, _)| *n == c.name) {
            *slot = entry;
        } else {
            lookup.push(entry);
        }
    }
    let find = |n: &str| lookup.iter().find(|(m, _, _)| m == n).cloned();
    let conf_names: Vec<String> = confident.iter().map(|c| c.name.clone()).collect();
    let mut chords = Vec::new();
    let mut chord_names = Vec::new();
    for (name, count) in counts_desc(&conf_names).into_iter().take(6) {
        let (_, root, quality) = find(&name).unwrap();
        let roman = music::roman_numeral(root, quality, tonic, scale);
        let cs = music::chord_scale(root, quality, tonic, scale);
        chords.push(
            Json::obj()
                .with("name", name.clone())
                .with("bars", count)
                .with("roman", roman.clone())
                .with("scale", cs.clone()),
        );
        chord_names.push((name, roman, cs, String::new(), count));
    }
    let roman = progression
        .iter()
        .map(|n| match find(n) {
            Some((_, root, q)) => music::roman_numeral(root, q, tonic, scale),
            None => "?".into(),
        })
        .collect();
    Some(Harmony { progression, roman, chords, chord_names, bar_chords, lookup })
}

fn section_loop(seq: &[Option<String>]) -> Vec<Option<String>> {
    if seq.iter().all(|v| v.is_none()) {
        return Vec::new();
    }
    let windows: Vec<Vec<Option<String>>> = if seq.len() >= 4 {
        (0..seq.len() - 3).step_by(4).map(|i| seq[i..(i + 4).min(seq.len())].to_vec()).collect()
    } else {
        Vec::new()
    };
    let windows: Vec<Vec<Option<String>>> = windows.into_iter().filter(|w| w.iter().any(|v| v.is_some())).collect();
    match most_common(&windows) {
        Some((w, _)) => w,
        None => seq[..seq.len().min(4)].to_vec(),
    }
}

fn opt_strs(v: &[Option<String>]) -> Json {
    Json::Arr(v.iter().map(|s| Json::from(s.clone())).collect())
}

fn strs(v: &[String]) -> Json {
    Json::Arr(v.iter().map(|s| Json::from(s.clone())).collect())
}

fn section_harmony(h: &Harmony, secs: &[SecMeta], tonic: i64, scale: &str) -> Vec<Json> {
    let mut out = Vec::new();
    for sec in secs {
        let (a, b) = sec.span(h.bar_chords.len());
        if b <= a {
            continue;
        }
        let prog = section_loop(&h.bar_chords[a..b]);
        let roman: Vec<Option<String>> = prog
            .iter()
            .map(|n| {
                n.as_ref().map(|n| {
                    let (_, root, q) = h.lookup.iter().find(|(m, _, _)| m == n).unwrap();
                    music::roman_numeral(*root, q, tonic, scale)
                })
            })
            .collect();
        out.push(sec.json().with("progression", opt_strs(&prog)).with("roman", opt_strs(&roman)));
    }
    out
}

fn section_bass(bass: &Bass, secs: &[SecMeta], tonic: Option<i64>, scale: &str) -> Vec<Json> {
    let notes = &bass.bar_notes;
    let mut out = Vec::new();
    for sec in secs {
        let (a, b) = sec.span(notes.len());
        if b <= a {
            continue;
        }
        let mut lp: Vec<Option<String>> = Vec::new();
        let mut rhythm: Option<String> = None;
        let has: Vec<usize> = (a..b).filter(|&i| notes[i].is_some()).collect();
        if !has.is_empty() && bass.reference > 0.0 && np::median(&bass.low[a..b]) >= NO_BASS_SECTION * bass.reference {
            lp = section_loop(&notes[a..b]);
            let rows: Vec<&[f64]> = has.iter().map(|&i| bass.flux[i].as_slice()).collect();
            let prof = mean_rows(&rows, bass.flux[0].len());
            let pm = fmax(&prof);
            rhythm = if pm > 0.0 { Some(pattern(&prof.iter().map(|v| v / pm).collect::<Vec<_>>())) } else { None };
        }
        let moves =
            if !lp.is_empty() && !bass.beats.is_empty() { loop_beats(&lp, notes, &bass.beats, a, b) } else { None };
        let mut j = sec
            .json()
            .with("loop", opt_strs(&lp))
            .with("degrees", opt_strs(&degrees(&lp, tonic, scale)))
            .with("rhythm", rhythm);
        if let Some(m) = moves {
            j.set("beats", Json::Arr(m.into_iter().map(|r| r.map_or(Json::Null, |r| strs(&r))).collect()));
        }
        out.push(j);
    }
    out
}

// --- Suggestion rules ------------------------------------------------------------

/// Lanes only the optional drum model finds, and their Drum Rack pad (General MIDI crash).
const MODEL_PADS: [(&str, &str); 1] = [("cymbal", "C#2")];

/// The Drum Rack pads; with the drum model, plus its own lanes that are in the grid.
fn rack(grid: &[Lane], model: bool) -> Vec<(&'static str, &'static str)> {
    let mut r = DRUM_RACK.to_vec();
    if model {
        r.extend(MODEL_PADS.iter().filter(|(k, _)| grid.iter().any(|g| g.name == *k)));
    }
    r
}

fn rack_json(rack: &[(&str, &str)]) -> Json {
    Json::Obj(rack.iter().map(|(k, v)| (k.to_string(), Json::from(*v))).collect())
}

#[allow(clippy::too_many_arguments)]
fn suggest(
    card: &Card,
    grid: &[Lane],
    model: bool,
    bass: Option<&Bass>,
    harm: Option<&Harmony>,
    by_section: &[(Json, Vec<(&'static str, String)>)],
    bass_by_section: &[Json],
    harm_by_section: &[Json],
) -> Vec<Suggestion> {
    let core = &card.core;
    let f = |k: &str| card.feature(k);
    let bpm = core.bpm.filter(|&b| b != 0.0);
    let key = core.key.as_ref();
    let mut out: Vec<Suggestion> = Vec::new();
    let mut add = |category: &'static str, t: String, body: String, data: Json| {
        out.push(Suggestion { category, title: title(t), body, data });
    };
    let root = key.map(|k| music::note_name(k.tonic));
    let scale = key.map(|k| {
        if k.scale.is_empty() {
            if k.mode == "minor" {
                "Minor"
            } else {
                "Major"
            }
        } else {
            k.scale
        }
    });
    let perc = f("percussiveness");
    let bpb = if core.beats_per_bar != 0 { core.beats_per_bar } else { 4 };
    let style = bpm.map(|b| style(b, perc, bpb));

    // --- Setup ---
    if let Some(bpm) = bpm {
        let warp = if perc.unwrap_or(0.0) > 0.3 { "Beats" } else { "Complex Pro" };
        let mut t = format!("Set the project to {bpm:.0} BPM");
        if key.is_some() {
            t += &format!(", {} {}", root.unwrap(), scale.unwrap());
        }
        let mut body = format!("Set the tempo to {bpm:.1} (half/double: {:.0}/{:.0}). ", bpm / 2.0, bpm * 2.0);
        let mut data = Json::obj().with("bpm", bpm).with("warp_mode", warp).with("template", "{setup}");
        if let Some(k) = key {
            let (r, s) = (root.unwrap(), scale.unwrap());
            let cam = if k.camelot.is_empty() {
                String::new()
            } else {
                format!(" Camelot {} if you are mixing it.", k.camelot)
            };
            body += &format!(
                "Turn on Scale in the Control Bar with root {r} and scale {s} so MIDI devices and the piano roll follow it.{cam} "
            );
            data.set("root", r);
            data.set("scale", s);
            data.set("camelot", k.camelot.clone());
        }
        body += &format!("If you drop the audio in, warp it in {warp} mode.");
        add("setup", t, body, data);
    } else if key.is_some() {
        let (r, s) = (root.unwrap(), scale.unwrap());
        add(
            "setup",
            format!("Set Scale to {r} {s}"),
            format!("Turn on Scale in the Control Bar with root {r} and scale {s}."),
            Json::obj().with("root", r).with("scale", s).with("template", "{setup}"),
        );
    }

    // --- Drums ---
    if let (Some(sc), Some(sp)) = (f("swing_confidence"), f("swing_percent")) {
        if sc > 0.3 && sp >= 54.0 {
            let amount = (round_half_even(sp / 2.0) * 2.0).clamp(50.0, 75.0) as i64;
            add(
                "drums",
                format!("Swing your 16ths to about {amount}%"),
                format!(
                    "The off-beat 16ths sit late. Drag 'Swing 16ths' from the Groove Pool (Core Library > Swing) onto \
                     your drum clip and start around {amount}%, or use a Groove with Timing at about {}%.",
                    (amount - 50) * 4
                ),
                Json::obj().with("swing_percent", amount).with("template", "{swing}"),
            );
        }
    }
    let detected: Vec<(&str, &str)> =
        grid.iter().filter(|g| g.detected).map(|g| (g.name, g.pattern.as_str())).collect();
    if !detected.is_empty() {
        let rows = detected
            .iter()
            .map(|(k, p)| {
                let steps = step_list(p).iter().map(|s| s.to_string()).collect::<Vec<_>>().join(", ");
                format!("{k} on steps {}", if steps.is_empty() { "none".to_string() } else { steps })
            })
            .collect::<Vec<_>>()
            .join("; ");
        let pads_of = rack(grid, model);
        let pads = detected
            .iter()
            .filter_map(|(k, _)| {
                pads_of.iter().find(|(r, _)| r == k).map(|(_, n)| format!("{} {n}", k.replace('_', " ")))
            })
            .collect::<Vec<_>>()
            .join(", ");
        let names: Vec<&str> = detected.iter().map(|(k, _)| *k).collect();
        let pat = Json::Obj(detected.iter().map(|(k, p)| (k.to_string(), Json::from(*p))).collect());
        let mut data = Json::obj()
            .with("pattern", pat)
            .with("pads", rack_json(&pads_of))
            .with("origin", "detected")
            .with("bpm", bpm.map(round_int))
            .with("lane_help", lane_help(&names, model))
            .with(
                "template",
                format!(
                    "Measured per section, since drums change between sections. Averaged over the busiest bars the \
                     whole track reads as {rows}. Program it on {{drumrack}}{{pads}} and vary the last bar of every 4."
                ),
            );
        if !by_section.is_empty() {
            let secs: Vec<Json> = by_section
                .iter()
                .map(|(meta, p)| {
                    let pj = Json::Obj(
                        p.iter()
                            .filter(|(k, _)| names.contains(k))
                            .map(|(k, v)| (k.to_string(), Json::from(v.clone())))
                            .collect(),
                    );
                    meta.clone().with("pattern", pj)
                })
                .collect();
            data.set("by_section", Json::Arr(secs));
        }
        add(
            "drums",
            "Detected groove".into(),
            format!(
                "Measured per section, since drums change between sections. Averaged over the busiest bars the whole \
                 track reads as {rows}. Program it on a Drum Rack ({pads}) and vary the last bar of every 4."
            ),
            data,
        );
    }
    if let Some(st) = style {
        for (name, pat) in variations(st) {
            let lanes: Vec<&str> = pat.iter().map(|(k, _)| *k).collect();
            let lower = name.to_lowercase();
            add(
                "drums",
                format!("Pattern idea: {name}"),
                format!("A {lower} pattern that sits at this tempo. Lanes map to default Drum Rack pads."),
                Json::obj()
                    .with("pattern", Json::Obj(pat.iter().map(|(k, v)| (k.to_string(), Json::from(*v))).collect()))
                    .with("pads", rack_json(&DRUM_RACK))
                    .with("origin", "template")
                    .with("style", style_label(st))
                    .with("style_key", st)
                    .with("bpm", round_int(bpm.unwrap()))
                    .with("lane_help", lane_help(&lanes, false))
                    .with(
                        "template",
                        format!(
                            "A {lower} pattern that sits at this tempo. Lanes map to the default {{drumrack}} pads."
                        ),
                    ),
            );
        }
    }

    // --- Bass ---
    if let Some(b) = bass.filter(|b| !b.lp.is_empty()) {
        let tip = match b.movement {
            "pedal" => {
                "It mostly sits on one note, so the interest is in rhythm and filter movement. Try an Auto Filter with \
                 an LFO synced to 1/8 or a Wavetable filter envelope with short decay."
            }
            "stepwise" => {
                "It moves stepwise, which suggests a melodic bassline. Try Glide in Operator or Drift with legato notes."
            }
            "fourths/fifths" => {
                "It moves in fourths and fifths, classic root motion. Keep the bass on chord roots and add an octave \
                 jump on the last 16th of the bar."
            }
            "leaps" => "It leaps around, so keep the sound tight (short release) so the notes don't smear.",
            _ => "It barely moves; rhythm does the work here.",
        };
        let rhythm = b.rhythm.clone().unwrap_or_default();
        let r: Vec<char> = rhythm.chars().collect();
        let offbeat =
            r.len() == steps_in_bar(card) && r[0] == '.' && (2..r.len()).step_by(4).all(|i| r[i] == 'x' || r[i] == 'o');
        let degs: Vec<String> = b.degrees.iter().map(|d| d.clone().unwrap_or_else(|| "None".into())).collect();
        let degrees = if degs.is_empty() { String::new() } else { format!(" (scale degrees {})", degs.join(" - ")) };
        let lp = b.lp.join(" - ");
        let mut body = format!("Per-bar bass notes loop as {lp}{degrees}. {tip}");
        if offbeat {
            body += " The bass hits the off-beats (house-style), so leave the downbeats to the kick.";
        }
        let mut data = Json::obj()
            .with("loop", strs(&b.lp))
            .with("degrees", opt_strs(&b.degrees))
            .with("rhythm", rhythm)
            .with("movement", b.movement);
        if !bass_by_section.is_empty() {
            data.set("by_section", Json::Arr(bass_by_section.to_vec()));
        }
        add("bass", format!("Bass loop: {lp} ({})", b.movement), body, data);
    }
    if let Some(bpm) = bpm {
        if f("low_end").unwrap_or(0.0) > 0.2 || matches!(style, Some("four_floor" | "dnb" | "halftime")) {
            let eighth = 60000.0 / bpm / 2.0;
            add(
                "bass",
                "Sidechain the bass to the kick".into(),
                format!(
                    "Put a Compressor on the bass, enable Sidechain from the kick, ratio 4:1, attack 0.1 ms, release \
                     about {:.0}-{eighth:.0} ms (an 8th note at this tempo is {eighth:.0} ms). Or use Live 12's \
                     Shaper/Envelope Follower for a drawn ducking curve.",
                    eighth * 0.6
                ),
                Json::obj().with("release_ms", round_int(eighth)).with("template", "{sidechain}"),
            );
        }
    }

    // --- Harmony ---
    let root_s = root.unwrap_or("None");
    let scale_s = scale.unwrap_or("None");
    if let Some(h) = harm.filter(|h| !h.progression.is_empty()) {
        let (prog, roman) = (h.progression.join(" - "), h.roman.join(" - "));
        let mut data = Json::obj().with("progression", strs(&h.progression)).with("roman", strs(&h.roman));
        if !harm_by_section.is_empty() {
            data.set("by_section", Json::Arr(harm_by_section.to_vec()));
        }
        add(
            "harmony",
            format!("Chord loop guess: {prog}"),
            format!(
                "The most common 4-bar loop reads as {prog} ({roman} in {root_s} {scale_s}). Sketch it with the Chord \
                 MIDI effect or Live 12's chord generators, then thin the voicing so it doesn't fight the bass."
            ),
            data,
        );
        for (c, (name, roman, cs, _, bars)) in h.chords.iter().zip(&h.chord_names).take(4) {
            add(
                "harmony",
                format!("Over {name} ({roman}): play {cs}"),
                format!("{name} shows up in {bars} bars. For leads or arps over it, {cs} fits."),
                c.clone(),
            );
        }
    }
    if let Some(k) = key {
        let pent = if k.mode == "minor" { "Minor Pentatonic" } else { "Major Pentatonic" };
        add(
            "harmony",
            format!("Safe lead scale: {root_s} {pent}"),
            format!(
                "For hooks that never clash, set Scale to {root_s} {pent} and run an Arpeggiator (Up/Down, 1/16) into a short pluck."
            ),
            Json::obj().with("root", root_s).with("scale", pent),
        );
    }

    // --- Arrangement ---
    let secs: Vec<&Section> = card.sections.iter().filter(|s| s.start_bar.is_some()).collect();
    if secs.len() > 1 {
        let names: Vec<String> = secs
            .iter()
            .map(|s| match &s.letter {
                Some(l) if !l.is_empty() => format!("{} {l}", capitalize(&s.label)),
                _ => capitalize(&s.label),
            })
            .collect();
        let bar = |v: Option<i64>| v.map_or("None".to_string(), |v| v.to_string());
        let layout = names
            .iter()
            .zip(&secs)
            .map(|(n, s)| format!("{n} (bars {}-{})", bar(s.start_bar), bar(s.end_bar)))
            .collect::<Vec<_>>()
            .join(", ");
        let locators: Vec<Json> = names
            .iter()
            .zip(&secs)
            .map(|(n, s)| Json::obj().with("bar", s.start_bar).with("name", n.clone()))
            .collect();
        add(
            "arrangement",
            format!("Arrangement map: {} sections", secs.len()),
            format!(
                "Rough layout: {layout}. Drop locators at those bars in Arrangement View to rebuild the structure, then \
                 fill in your own parts."
            ),
            Json::obj().with("locators", Json::Arr(locators)),
        );
        if let Some(s) = secs.iter().find(|s| s.label == "build") {
            let start = s.start_bar.unwrap();
            let bars = s.end_bar.unwrap_or(start) - start + 1;
            add(
                "arrangement",
                format!("Recreate the build at bar {start}"),
                format!(
                    "A {bars}-bar riser. Automate an Auto Filter high-pass up over it, add a snare roll that doubles \
                     every 2 bars, and cut the kick for the last beat before the drop."
                ),
                Json::obj().with("start_bar", start).with("bars", bars),
            );
        }
    }

    // --- Sound ---
    match f("brightness") {
        Some(b) if b < 0.35 => add(
            "sound",
            "It's a dark mix".into(),
            "Most energy sits low-mid. Roll off highs on pads (EQ Eight shelf around 6 kHz) and let only hats and one \
             lead live up top."
                .into(),
            Json::obj(),
        ),
        Some(b) if b > 0.6 => add(
            "sound",
            "It's a bright mix".into(),
            "Lots of top end. Saturator on the drum bus and a touch of Hybrid Reverb with a high low-cut will match it."
                .into(),
            Json::obj(),
        ),
        _ => {}
    }
    out
}

pub fn run(ctx: &mut Ctx, card: &mut Card) -> Result<(), String> {
    let key = card.core.key.clone();
    let tonic = key.as_ref().map(|k| k.tonic);
    let scale: &str = match &key {
        Some(k) if !k.scale.is_empty() => k.scale,
        Some(k) if k.mode == "minor" => "Minor",
        _ => "Major",
    };
    let n_frames = 1 + ctx.y.len() / HOP;
    let sf = step_grid(card, ctx.sr, n_frames);
    let (mut grid, mut bass, mut harm) = (Vec::new(), None, None);
    let mut model = false;
    if !sf.is_empty() {
        let mut drums = Drums::new(&ctx.mel_perc, &ctx.mel_perc_freqs, &ctx.air_flux);
        if let Some(hits) = &ctx.drum_hits {
            drums.set_model(hits, ctx.sr, HOP);
            card.extras.set("drum_model", "adtof"); // re-analysis keeps using it (service.reanalyze_track)
            model = true;
        }
        grid = drum_grid(ctx, card, &drums, &sf)?;
        bass = bass_summary(ctx, &sf, tonic, scale);
        if let Some(t) = tonic {
            harm = harmony_summary(ctx, &sf, t, scale);
        }
    }
    let secs: Vec<SecMeta> = card
        .sections
        .iter()
        .enumerate()
        .filter_map(|(i, s)| {
            s.start_bar.map(|sb| SecMeta {
                idx: i,
                label: s.label.clone(),
                letter: s.letter.clone(),
                start_bar: sb,
                end_bar: s.end_bar,
                energy: s.energy,
            })
        })
        .collect();
    let (mut by_section, mut sec_bass, mut sec_harm) = (Vec::new(), Vec::new(), Vec::new());
    if secs.len() > 1 {
        if !grid.is_empty() {
            by_section = section_grooves(&grid, &bar_energy(ctx, card, &sf), &secs);
        }
        if let Some(b) = &bass {
            sec_bass = section_bass(b, &secs, tonic, scale);
        }
        if let (Some(h), Some(t)) = (&harm, tonic) {
            sec_harm = section_harmony(h, &secs, t, scale);
        }
    }
    if !grid.is_empty() {
        let g = Json::Obj(
            grid.iter()
                .map(|l| {
                    (
                        l.name.to_string(),
                        Json::obj()
                            .with("pattern", l.pattern.clone())
                            .with("velocity", l.velocity.clone())
                            .with("detected", l.detected),
                    )
                })
                .collect(),
        );
        card.extras.set("suggestions.drum_grid", g);
    }
    if let Some(b) = &bass {
        card.extras.set(
            "suggestions.bass",
            Json::obj()
                .with("notes", strs(&b.notes))
                .with("loop", strs(&b.lp))
                .with("degrees", opt_strs(&b.degrees))
                .with("movement", b.movement)
                .with("rhythm", b.rhythm.clone())
                .with("bar_notes", opt_strs(&b.bar_notes)),
        );
    }
    if let Some(h) = &harm {
        card.extras.set(
            "suggestions.harmony",
            Json::obj()
                .with("progression", strs(&h.progression))
                .with("roman", strs(&h.roman))
                .with("chords", Json::Arr(h.chords.clone()))
                .with("bar_chords", opt_strs(&h.bar_chords)),
        );
    }
    card.suggestions = suggest(card, &grid, model, bass.as_ref(), harm.as_ref(), &by_section, &sec_bass, &sec_harm);
    Ok(())
}
