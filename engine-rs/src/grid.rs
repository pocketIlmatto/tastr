//! A DJ beat grid: the tempo and the time of the first beat to a fraction of a millisecond, plus an anchor
//! wherever the tempo really changes (a live drummer, an edit).
//!
//! The beat tracker's beats come from an onset envelope on 23 ms frames whose long windows pull each beat a few
//! milliseconds to tens of milliseconds early. Here every tracked beat is moved to the attack it belongs to,
//! found with an energy-ratio onset function at sample resolution, and a straight line `t = t0 + k * period` is
//! fitted through the moved beats. Tracks whose tempo drifts get more than one line (anchors).
//!
//! The onset function at sample `t` is ln(energy in [t, t + W) / energy in [t - W, t)), on the whole band and on
//! a zero-phase low-passed copy (kicks). For an attack starting at `t*` both windows are symmetric about `t*`,
//! so the peak lands on the attack itself rather than a window's length before or after it.

use crate::np::round_dec;

/// Energy windows either side of a sample, in seconds.
const WINDOW_S: f64 = 0.010;
/// The onset function is evaluated every HOP_SAMPLES samples (0.36 ms at 22,050 Hz), then interpolated.
const HOP_SAMPLES: usize = 8;
/// Low band for the kick, Hz (one-pole low-pass run forwards and backwards, so it adds no delay).
const LOW_HZ: f64 = 150.0;
/// A tracked beat looks for its attack within this many seconds either side (and at most a fifth of a beat).
const SEARCH_S: f64 = 0.035;
/// Attacks weaker than this (natural-log energy ratio, summed over both bands) don't count.
const MIN_STRENGTH: f64 = 0.4;
/// Beats further than this from the fitted line are dropped and the line refitted (seconds), in rounds that
/// tighten to OUTLIER_S, so a line first pulled off by a drifting tempo doesn't throw most beats away.
const OUTLIER_ROUNDS_S: [f64; 4] = [0.06, 0.03, 0.018, 0.012];
const OUTLIER_S: f64 = 0.012;
/// Anchors: the split of the attacks into lines that minimises squared error plus ANCHOR_COST * jitter^2 *
/// ln(attacks) per line (a BIC-like penalty, so a hand-played steady tempo keeps one line and a drifting one
/// gets as many as it needs). Jitter is the spread of the attacks around lines through their neighbours
/// (NEIGHBOURS each side), at least MIN_JITTER_S; attacks further than OUTLIER_S from that local line are left
/// out. No line covers fewer than MIN_SEGMENT attacks.
const ANCHOR_COST: f64 = 6.0;
const NEIGHBOURS: usize = 8;
const MIN_JITTER_S: f64 = 0.001;
const MIN_SEGMENT: usize = 16;
const MAX_ANCHORS: usize = 16;
/// Too few attacks to trust a grid.
const MIN_POINTS: usize = 8;

/// Where the grid starts a (new) tempo.
#[derive(Clone, Debug, PartialEq)]
pub struct Anchor {
    /// Time of the anchor beat, seconds.
    pub time: f64,
    /// Beats per minute from here to the next anchor, three decimals.
    pub bpm: f64,
}

/// The fitted grid. Times are seconds from the start of the audio.
#[derive(Clone, Debug, PartialEq)]
pub struct Grid {
    /// The first anchor's tempo, three decimals.
    pub bpm: f64,
    /// The earliest grid beat at or after 0 s (the first anchor's grid carried back to the start).
    pub first_beat: f64,
    /// The grid beat at the first downbeat, carried back to the earliest whole bar at or after 0 s; None without
    /// downbeats.
    pub first_downbeat: Option<f64>,
    /// One anchor for a steady track, more where the tempo changes. The first is at `first_beat`.
    pub anchors: Vec<Anchor>,
    /// Root-mean-square distance of the attacks used from the grid, in milliseconds: small (1-3) for programmed
    /// music, larger for live playing.
    pub fit_ms: f64,
    /// How many beats found an attack and fed the fit.
    pub beats_used: usize,
}

/// A grid from mono audio `y` at `sr` Hz and the tracker's beats and downbeats (seconds). None when there are
/// too few clear attacks.
pub fn fit(y: &[f32], sr: f64, beats: &[f64], downbeats: &[f64], beats_per_bar: i64) -> Option<Grid> {
    if beats.len() < MIN_POINTS || y.is_empty() || sr <= 0.0 {
        return None;
    }
    let period = crate::np::median(&beats.windows(2).map(|w| w[1] - w[0]).collect::<Vec<_>>());
    if !(period.is_finite() && period > 0.0) {
        return None;
    }
    let onset = OnsetFunction::new(y, sr);
    let search = SEARCH_S.min(0.2 * period);
    // (beat count, attack time, strength)
    let counts = beat_counts(beats);
    let mut points: Vec<(f64, f64, f64)> = Vec::new();
    for (&b, &k) in beats.iter().zip(&counts) {
        if let Some((t, s)) = onset.peak(b - search, b + search) {
            if s >= MIN_STRENGTH {
                points.push((k, t, s));
            }
        }
    }
    if points.len() < MIN_POINTS {
        return None;
    }
    let mut segments = Vec::new();
    segment(&points, period, &mut segments);
    segments.sort_by(|a, b| a.k0.total_cmp(&b.k0));
    let first = &segments[0];
    // Carry the first segment's line back to the earliest beat at or after 0 s.
    let t_first = first.at(first.k0);
    let first_beat = t_first - libm::floor(t_first / first.period + 1e-9) * first.period;
    let mut anchors = vec![Anchor { time: round_dec(first_beat, 4), bpm: round_dec(60.0 / first.period, 3) }];
    for s in &segments[1..] {
        anchors.push(Anchor { time: round_dec(s.at(s.k0), 4), bpm: round_dec(60.0 / s.period, 3) });
    }
    let first_downbeat = downbeats.first().map(|&d| {
        let seg = segments.iter().rev().find(|s| s.at(s.k0) <= d + 0.5 * s.period).unwrap_or(first);
        let on_grid = seg.at(libm::round((d - seg.intercept) / seg.period));
        let bar = beats_per_bar.max(1) as f64 * first.period;
        // Carry back along the first segment when the downbeat lies in it.
        if std::ptr::eq(seg, first) {
            on_grid - libm::floor(on_grid / bar + 1e-9) * bar
        } else {
            on_grid
        }
    });
    let (sse, w): (f64, f64) =
        segments.iter().map(|s| (s.sse, s.weight)).fold((0.0, 0.0), |a, b| (a.0 + b.0, a.1 + b.1));
    Some(Grid {
        bpm: anchors[0].bpm,
        first_beat: anchors[0].time,
        first_downbeat: first_downbeat.map(|t| round_dec(t, 4)),
        anchors,
        fit_ms: round_dec(1000.0 * libm::sqrt(sse / w.max(1e-12)), 2),
        beats_used: segments.iter().map(|s| s.used).sum(),
    })
}

/// Each beat's count from the first, stepping beat to beat by the local beat length (the median gap of the
/// gaps around it), so a skipped beat counts as two and a tempo change doesn't throw the count off.
fn beat_counts(beats: &[f64]) -> Vec<f64> {
    let gaps: Vec<f64> = beats.windows(2).map(|w| w[1] - w[0]).collect();
    let mut k = vec![0.0; beats.len()];
    for (i, g) in gaps.iter().enumerate() {
        let lo = i.saturating_sub(8);
        let hi = (i + 9).min(gaps.len());
        let local = crate::np::median(&gaps[lo..hi]);
        k[i + 1] = k[i] + libm::round(g / local).max(1.0);
    }
    k
}

/// A line through some of the attacks: t = intercept + period * k.
#[derive(Clone, Debug)]
struct Segment {
    intercept: f64,
    period: f64,
    /// First beat count of the segment.
    k0: f64,
    /// Weighted sum of squared residuals and the weights, over the points kept.
    sse: f64,
    weight: f64,
    used: usize,
    /// sse plus OUTLIER_S squared for every dropped point (weighted), to compare splits fairly.
    cost: f64,
}

impl Segment {
    fn at(&self, k: f64) -> f64 {
        self.intercept + self.period * k
    }
}

/// Weighted least-squares line through (k, t) with weights s, dropping points further than OUTLIER_S from it
/// and refitting (OUTLIER_ROUNDS_S). `period` is the fallback slope for fewer than two points.
fn fit_line(points: &[(f64, f64, f64)], period: f64) -> Segment {
    let mut keep = vec![true; points.len()];
    let mut seg = Segment {
        intercept: 0.0,
        period,
        k0: points.first().map_or(0.0, |p| p.0),
        sse: 0.0,
        weight: 0.0,
        used: 0,
        cost: 0.0,
    };
    for limit in OUTLIER_ROUNDS_S {
        let (mut sw, mut sk, mut st, mut skk, mut skt) = (0.0, 0.0, 0.0, 0.0, 0.0);
        for (p, _) in points.iter().zip(&keep).filter(|(_, &k)| k) {
            let (k, t, w) = *p;
            sw += w;
            sk += w * k;
            st += w * t;
            skk += w * k * k;
            skt += w * k * t;
        }
        let det = sw * skk - sk * sk;
        if sw <= 0.0 {
            break;
        }
        if det.abs() > 1e-12 * sw * skk.max(1.0) {
            seg.period = (sw * skt - sk * st) / det;
            seg.intercept = (st - seg.period * sk) / sw;
        } else {
            seg.intercept = (st - seg.period * sk) / sw;
        }
        for (p, k) in points.iter().zip(keep.iter_mut()) {
            *k = (p.1 - seg.at(p.0)).abs() <= limit;
        }
    }
    let (mut sse, mut w, mut used, mut dropped) = (0.0, 0.0, 0, 0.0);
    for (p, &k) in points.iter().zip(&keep) {
        if !k {
            dropped += p.2 * OUTLIER_S * OUTLIER_S;
            continue;
        }
        if used == 0 {
            seg.k0 = p.0;
        }
        let r = p.1 - seg.at(p.0);
        sse += p.2 * r * r;
        w += p.2;
        used += 1;
    }
    seg.cost = sse + dropped;
    seg.sse = sse;
    seg.weight = w;
    seg.used = used;
    seg
}

/// Plain least-squares line through (k, t) from prefix sums: (sse, n) for points a..b.
struct Sums {
    n: Vec<f64>,
    k: Vec<f64>,
    t: Vec<f64>,
    kk: Vec<f64>,
    kt: Vec<f64>,
    tt: Vec<f64>,
}

impl Sums {
    fn new(points: &[(f64, f64, f64)]) -> Sums {
        let mut s = Sums { n: vec![0.0], k: vec![0.0], t: vec![0.0], kk: vec![0.0], kt: vec![0.0], tt: vec![0.0] };
        for &(k, t, _) in points {
            s.n.push(s.n.last().unwrap() + 1.0);
            s.k.push(s.k.last().unwrap() + k);
            s.t.push(s.t.last().unwrap() + t);
            s.kk.push(s.kk.last().unwrap() + k * k);
            s.kt.push(s.kt.last().unwrap() + k * t);
            s.tt.push(s.tt.last().unwrap() + t * t);
        }
        s
    }

    fn sse(&self, a: usize, b: usize) -> f64 {
        let d = |v: &Vec<f64>| v[b] - v[a];
        let (n, k, t, kk, kt, tt) = (d(&self.n), d(&self.k), d(&self.t), d(&self.kk), d(&self.kt), d(&self.tt));
        let skk = kk - k * k / n;
        let skt = kt - k * t / n;
        let stt = tt - t * t / n;
        (if skk > 0.0 { stt - skt * skt / skk } else { stt }).max(0.0)
    }
}

/// Each attack's distance from a least-squares line through its NEIGHBOURS on either side (not itself).
fn local_residuals(points: &[(f64, f64, f64)]) -> Vec<f64> {
    (0..points.len())
        .map(|i| {
            let lo = i.saturating_sub(NEIGHBOURS);
            let hi = (i + NEIGHBOURS + 1).min(points.len());
            let (k, t): (Vec<f64>, Vec<f64>) =
                points[lo..hi].iter().enumerate().filter(|(j, _)| lo + j != i).map(|(_, p)| (p.0, p.1)).unzip();
            if k.len() < 2 {
                return 0.0;
            }
            let (slope, icpt) = crate::np::polyfit1(&k, &t);
            points[i].1 - (icpt + slope * points[i].0)
        })
        .collect()
}

/// Split `points` into lines (see ANCHOR_COST) by dynamic programming over where each line starts.
fn segment(points: &[(f64, f64, f64)], period: f64, out: &mut Vec<Segment>) {
    let local = local_residuals(points);
    let clean: Vec<(f64, f64, f64)> =
        points.iter().zip(&local).filter(|(_, r)| r.abs() <= OUTLIER_S).map(|(p, _)| *p).collect();
    let n = clean.len();
    if n < 2 * MIN_SEGMENT {
        out.push(fit_line(points, period));
        return;
    }
    let mut abs: Vec<f64> = local.iter().filter(|r| r.abs() <= OUTLIER_S).map(|r| r.abs()).collect();
    abs.sort_by(|a, b| a.total_cmp(b));
    let jitter = (1.4826 * abs[abs.len() / 2]).max(MIN_JITTER_S);
    let sums = Sums::new(&clean);
    let mut penalty = ANCHOR_COST * jitter * jitter * libm::log(n as f64);
    let starts = loop {
        // best[j]: the cheapest cover of clean[..j]; from[j]: where its last line starts.
        let mut best = vec![f64::INFINITY; n + 1];
        let mut from = vec![0usize; n + 1];
        best[0] = 0.0;
        for j in MIN_SEGMENT..=n {
            for i in 0..=j - MIN_SEGMENT {
                if !best[i].is_finite() {
                    continue;
                }
                let c = best[i] + sums.sse(i, j) + penalty;
                if c < best[j] {
                    best[j] = c;
                    from[j] = i;
                }
            }
        }
        let mut starts = Vec::new();
        let mut j = n;
        while j > 0 {
            starts.push(from[j]);
            j = from[j];
        }
        starts.reverse();
        if starts.len() <= MAX_ANCHORS {
            break starts;
        }
        penalty *= 2.0;
    };
    // Lines through the clean attacks; then each boundary moves to the first attack (outliers included) that
    // the next line explains better than the previous one, and every line refits robustly on its stretch.
    let ends: Vec<usize> = starts.iter().skip(1).copied().chain([n]).collect();
    let rough: Vec<Segment> = starts.iter().zip(&ends).map(|(&a, &b)| fit_line(&clean[a..b], period)).collect();
    let mut cuts = vec![f64::NEG_INFINITY];
    for s in 1..rough.len() {
        let (prev, next) = (&rough[s - 1], &rough[s]);
        let (gap_lo, gap_hi) = (clean[starts[s] - 1].0, clean[starts[s]].0);
        let cut = points
            .iter()
            .filter(|p| p.0 > gap_lo && p.0 <= gap_hi)
            .find(|p| (p.1 - next.at(p.0)).abs() < (p.1 - prev.at(p.0)).abs())
            .map_or(gap_hi, |p| p.0);
        cuts.push(cut);
    }
    cuts.push(f64::INFINITY);
    for w in cuts.windows(2) {
        let part: Vec<(f64, f64, f64)> = points.iter().filter(|p| p.0 >= w[0] && p.0 < w[1]).copied().collect();
        out.push(fit_line(&part, period));
    }
}

/// The energy-ratio onset function on the whole band and the low band, every HOP_SAMPLES samples.
struct OnsetFunction {
    values: Vec<f64>,
    sr: f64,
    /// W in samples.
    window: usize,
}

impl OnsetFunction {
    fn new(y: &[f32], sr: f64) -> OnsetFunction {
        let w = libm::round(WINDOW_S * sr).max(4.0) as usize;
        let low = zero_phase_low_pass(y, sr, LOW_HZ);
        let full = cumulative_energy(y.iter().map(|&v| v as f64));
        let lowc = cumulative_energy(low.iter().copied());
        let floor = |c: &[f64]| 1e-3 * c[c.len() - 1] / y.len() as f64 * w as f64 + 1e-12;
        let (ff, fl) = (floor(&full), floor(&lowc));
        let ratio = |c: &[f64], t: usize, eps: f64| {
            let after = c[t + w] - c[t];
            let before = c[t] - c[t - w];
            libm::log((after + eps) / (before + eps)).max(0.0)
        };
        let n = y.len();
        let values = (0..n.div_ceil(HOP_SAMPLES))
            .map(|i| {
                let t = i * HOP_SAMPLES;
                if t < w || t + w > n {
                    0.0
                } else {
                    ratio(&full, t, ff) + ratio(&lowc, t, fl)
                }
            })
            .collect();
        OnsetFunction { values, sr, window: w }
    }

    /// The strongest attack between `from` and `to` seconds: (time, strength).
    fn peak(&self, from: f64, to: f64) -> Option<(f64, f64)> {
        let h = HOP_SAMPLES as f64 / self.sr;
        let a = libm::ceil(from.max(0.0) / h) as usize;
        let b = (libm::floor(to / h) as usize).min(self.values.len().saturating_sub(1));
        if a > b {
            return None;
        }
        let (i, &v) =
            self.values[a..=b]
                .iter()
                .enumerate()
                .fold((0, &f64::NEG_INFINITY), |m, (j, x)| if *x > *m.1 { (j, x) } else { m });
        let i = a + i;
        if v <= 0.0 {
            return None;
        }
        // The function rises over the window before an attack and drops sharply once the attack enters the
        // "before" window, so the attack is where it falls through half its peak after the peak.
        let span = self.window / HOP_SAMPLES + 2;
        let end = (i + span).min(self.values.len() - 1);
        let half = 0.5 * v;
        for j in i + 1..=end {
            if self.values[j] < half {
                let (hi, lo) = (self.values[j - 1], self.values[j]);
                let frac = if hi > lo { (hi - half) / (hi - lo) } else { 0.0 };
                return Some(((j as f64 - 1.0 + frac) * h, v));
            }
        }
        Some((i as f64 * h, v))
    }
}

/// Running sum of squares, with a leading 0 (c[t] = energy of samples before t).
fn cumulative_energy(x: impl Iterator<Item = f64>) -> Vec<f64> {
    let mut c = vec![0.0];
    let mut s = 0.0;
    for v in x {
        s += v * v;
        c.push(s);
    }
    c
}

/// A one-pole low-pass at `hz`, run forwards then backwards so it adds no delay.
fn zero_phase_low_pass(y: &[f32], sr: f64, hz: f64) -> Vec<f64> {
    let a = libm::exp(-2.0 * core::f64::consts::PI * hz / sr);
    let mut out: Vec<f64> = Vec::with_capacity(y.len());
    let mut s = 0.0;
    for &v in y {
        s = (1.0 - a) * v as f64 + a * s;
        out.push(s);
    }
    s = 0.0;
    for v in out.iter_mut().rev() {
        s = (1.0 - a) * *v + a * s;
        *v = s;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const SR: f64 = 22_050.0;

    /// Kick-like clicks starting at `starts` (seconds) over quiet noise.
    fn clicks(starts: &[f64], seconds: f64) -> Vec<f32> {
        let n = (seconds * SR) as usize;
        let mut y: Vec<f32> = (0..n).map(|i| 0.002 * libm::sin(i as f64 * 12.9898) as f32).collect();
        for &t0 in starts {
            let s = libm::round(t0 * SR) as usize;
            for i in 0..(0.06 * SR) as usize {
                if s + i < n {
                    let t = i as f64 / SR;
                    y[s + i] +=
                        (libm::exp(-t / 0.012) * libm::sin(2.0 * core::f64::consts::PI * 70.0 * t + 0.6)) as f32;
                }
            }
        }
        y
    }

    /// The tracker's view: beats on a coarse 23 ms grid, shifted early.
    fn coarse(starts: &[f64], shift: f64) -> Vec<f64> {
        let frame = 512.0 / SR;
        starts.iter().map(|t| libm::round((t - shift) / frame) * frame).collect()
    }

    #[test]
    fn steady_grid_to_a_fraction_of_a_millisecond() {
        for (bpm, offset) in [(124.0, 0.25), (128.0, 0.0731), (174.0, 0.41), (93.5, 0.6)] {
            let p = 60.0 / bpm;
            let starts: Vec<f64> = (0..).map(|k| offset + k as f64 * p).take_while(|&t| t < 59.0).collect();
            let g = fit(
                &clicks(&starts, 60.0),
                SR,
                &coarse(&starts, 0.02),
                &coarse(&starts[2..], 0.02).iter().step_by(4).cloned().collect::<Vec<_>>(),
                4,
            )
            .unwrap();
            assert_eq!(g.anchors.len(), 1, "{bpm}: {:?}", g.anchors);
            assert!((g.bpm - bpm).abs() < 0.01, "{bpm}: bpm {}", g.bpm);
            let first = offset - libm::floor(offset / p) * p;
            assert!((g.first_beat - first).abs() < 0.002, "{bpm}: first beat {} vs {first}", g.first_beat);
            let bar = 4.0 * p;
            let want = starts[2] - libm::floor(starts[2] / bar) * bar;
            assert!(
                (g.first_downbeat.unwrap() - want).abs() < 0.002,
                "{bpm}: downbeat {:?} vs {want}",
                g.first_downbeat
            );
            assert!(g.fit_ms < 1.0, "{bpm}: fit {}", g.fit_ms);
        }
    }

    #[test]
    fn a_tempo_change_gets_a_second_anchor() {
        let mut starts = Vec::new();
        let mut t = 0.5;
        for _ in 0..96 {
            starts.push(t);
            t += 60.0 / 120.0;
        }
        let change = t;
        for _ in 0..96 {
            starts.push(t);
            t += 60.0 / 126.0;
        }
        let g = fit(&clicks(&starts, t + 1.0), SR, &coarse(&starts, 0.01), &[], 4).unwrap();
        assert_eq!(g.anchors.len(), 2, "{:?}", g.anchors);
        assert!((g.anchors[0].bpm - 120.0).abs() < 0.02 && (g.anchors[1].bpm - 126.0).abs() < 0.02, "{:?}", g.anchors);
        assert!((g.anchors[1].time - change).abs() < 0.6, "{:?} vs {change}", g.anchors);
        assert_eq!(g.first_downbeat, None);
    }

    #[test]
    fn a_drifting_live_tempo_stays_within_a_few_ms() {
        // A drummer speeding up from 118 to 122 BPM over 120 beats: no single line fits within 20 ms.
        let mut starts = Vec::new();
        let mut t = 0.4;
        for k in 0..120 {
            starts.push(t);
            t += 60.0 / (118.0 + 4.0 * k as f64 / 120.0);
        }
        let g = fit(&clicks(&starts, t + 1.0), SR, &coarse(&starts, 0.01), &[], 4).unwrap();
        assert!(g.anchors.len() >= 2, "{g:?}");
        let mut worst: f64 = 0.0;
        for &s in &starts {
            let a = g.anchors.iter().rev().find(|a| a.time <= s + 0.1).unwrap();
            let p = 60.0 / a.bpm;
            let k = libm::round((s - a.time) / p);
            worst = worst.max((s - (a.time + k * p)).abs());
        }
        assert!(worst < 0.02, "worst {worst} with {:?}", g.anchors);
    }

    #[test]
    fn a_steady_drummer_with_jitter_keeps_one_anchor() {
        // 125 BPM played by hand: each hit up to 8 ms early or late (sd about 4.6 ms), no drift.
        let p = 60.0 / 125.0;
        let starts: Vec<f64> = (0..200)
            .map(|k| 0.3 + k as f64 * p + 0.008 * libm::sin(k as f64 * 7.31 + libm::sin(k as f64 * 1.7)))
            .collect();
        let g = fit(&clicks(&starts, starts[199] + 1.0), SR, &coarse(&starts, 0.01), &[], 4).unwrap();
        assert!(g.anchors.len() <= 2, "{:?}", g.anchors);
        assert!((g.bpm - 125.0).abs() < 0.05 && (g.first_beat - 0.3).abs() < 0.004, "{g:?}");
    }

    #[test]
    fn missing_and_extra_hits_dont_move_it() {
        let p = 60.0 / 125.0;
        let starts: Vec<f64> = (0..110).map(|k| 0.3 + k as f64 * p).collect();
        // Every fifth kick missing, and a loud off-beat hit in every bar.
        let kept: Vec<f64> = starts.iter().enumerate().filter(|(i, _)| i % 5 != 0).map(|(_, &t)| t).collect();
        let mut audio = kept.clone();
        audio.extend(starts.iter().step_by(4).map(|t| t + 0.5 * p));
        let g = fit(&clicks(&audio, 54.0), SR, &coarse(&starts, 0.015), &[], 4).unwrap();
        assert_eq!(g.anchors.len(), 1);
        assert!((g.bpm - 125.0).abs() < 0.01 && (g.first_beat - 0.3).abs() < 0.002, "{g:?}");
    }

    #[test]
    fn silence_or_too_few_beats_has_no_grid() {
        assert_eq!(
            fit(&vec![0.0; 22_050 * 10], SR, &(0..20).map(|k| k as f64 * 0.5).collect::<Vec<_>>(), &[], 4),
            None
        );
        assert_eq!(fit(&clicks(&[0.5, 1.0], 3.0), SR, &[0.5, 1.0], &[], 4), None);
    }

    #[test]
    fn low_pass_adds_no_delay() {
        let mut y = vec![0.0f32; 4001];
        y[2000] = 1.0;
        let out = zero_phase_low_pass(&y, SR, LOW_HZ);
        let peak = out.iter().enumerate().fold((0, f64::NEG_INFINITY), |m, (i, &v)| if v > m.1 { (i, v) } else { m }).0;
        assert_eq!(peak, 2000);
    }
}
