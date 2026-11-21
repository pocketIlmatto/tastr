//! Suggested DJ cue points: where a DJ would mix in, mark a section change, catch the drop or start mixing out.
//!
//! Every cue sits on a downbeat. The `start` cue is the first downbeat after any silence at the start of the
//! track (what Rekordbox's auto cue does, on bar 1 instead of the first beat). Every section start from the
//! sections stage gets a `section` cue, unless the start, drop or mix-out cue is within 2 bars of it (then that
//! cue marks the boundary). The `drop`, `mix_out` and `phrase` cues sit on the phrase grid, 4-bar steps counted
//! from `start`. The picker reads only the bar times, the per-bar energy (0 to 1), the section starts and where
//! the audio becomes audible, so it is cheap and the same on every build: it uses plain arithmetic and
//! comparisons, no floating-point library calls.

use crate::np::{clip, median, round_dec};

/// The most cues a track gets: room for every section start (the stage makes at most 12) plus `start`, `drop`
/// and `mix_out`; `phrase` cues fill what is left.
pub const MAX_CUES: usize = 16;
/// A `phrase` cue needs this many bars between it and every other cue.
pub const MIN_SPACING_BARS: usize = 8;
/// The phrase grid's step and the phrase length, in bars.
const GRID_BARS: usize = 4;
const PHRASE_BARS: usize = 8;
/// The mix-out point snaps to an 8-bar phrase start within this many bars of the fall; a section start this
/// close to the start, drop or mix-out cue counts as the same boundary.
const SNAP_BARS: usize = 2;
/// Bars of energy compared on each side of a boundary.
const WINDOW_BARS: usize = 4;
/// A phrase boundary with no section start becomes a cue only when the energy changes by at least this much.
const PHRASE_MIN_CHANGE: f64 = 0.15;
/// A drop needs the 8 bars before it at or below LOW, the 4 bars after at or above HIGH, and a rise of
/// at least DROP_MIN_RISE between the 4 bars on each side.
const DROP_LOW: f64 = 0.5;
const DROP_HIGH: f64 = 0.6;
const DROP_MIN_RISE: f64 = 0.2;
/// The outro starts after the last 4 bars whose mean energy reaches this share of the way from the track's
/// quiet level (25th percentile of bar energy) to its loud level (90th).
const OUTRO_LEVEL: f64 = 0.5;
/// With no outro (loud to the end), `mix_out` goes on the last phrase start at least this many bars before the end.
const NO_OUTRO_BARS: usize = 32;

/// One suggested cue point.
#[derive(Clone, Debug, PartialEq)]
pub struct Cue {
    /// Seconds from the start of the audio (a downbeat).
    pub time_s: f64,
    /// The bar it starts, counting the track's first downbeat as bar 1 (as sections' `start_bar` does).
    pub bar: i64,
    /// "start", "section", "phrase", "drop" or "mix_out".
    pub kind: &'static str,
    /// How strong the cue is, 0 to 1: the energy change across it, plus a little for a section start.
    pub score: f64,
}

impl Cue {
    /// As JSON: {"time_s", "bar", "kind", "score"}.
    pub fn json(&self) -> crate::json::Json {
        crate::json::Json::obj()
            .with("time_s", self.time_s)
            .with("bar", self.bar)
            .with("kind", self.kind)
            .with("score", self.score)
    }
}

/// When the audio first rises above silence, in seconds: the first frame whose RMS is within 48 dB of the
/// loudest frame's and above -70 dBFS. `rms` is per frame, `hop` samples apart at `sr`.
pub fn audible_start(rms: &[f32], sr: f64, hop: usize) -> f64 {
    // Compare power, not dB, so no logarithm is needed: 48 dB is a power ratio of 10^4.8, -70 dBFS is 1e-7.
    let peak = rms.iter().fold(0.0f64, |m, &v| m.max(v as f64 * v as f64));
    let floor = (peak / 63_095.734_448_019_3).max(1e-7);
    match rms.iter().position(|&v| v as f64 * v as f64 > floor) {
        Some(f) => f as f64 * hop as f64 / sr,
        None => 0.0,
    }
}

/// The cues for a track, in time order. `bars` are the downbeat times, `energy` one value (0 to 1) per bar,
/// `section_starts` the sections' first bars (0-based), `audible` the time the audio first rises above silence.
pub fn pick(bars: &[f64], energy: &[f64], section_starts: &[usize], audible: f64) -> Vec<Cue> {
    let n = bars.len().min(energy.len());
    if n < 2 {
        return Vec::new();
    }
    let steps: Vec<f64> = bars[..n].windows(2).map(|w| w[1] - w[0]).collect();
    let bar_len = median(&steps);
    // Half a beat of slack in 4/4, so a first hit a hair early still counts the downbeat after it as bar 1's.
    let Some(i0) = (0..n).find(|&i| bars[i] >= audible - bar_len / 8.0) else {
        return Vec::new();
    };
    let e = &energy[..n];
    let mean = |a: usize, b: usize| -> f64 {
        let (a, b) = (a.min(n), b.min(n));
        if b <= a {
            return 0.0;
        }
        e[a..b].iter().sum::<f64>() / (b - a) as f64
    };
    let change = |k: usize| mean(k, k + WINDOW_BARS) - mean(k.saturating_sub(WINDOW_BARS).max(i0), k);
    let cue = |k: usize, kind: &'static str, score: f64| Cue {
        time_s: round_dec(bars[k], 3),
        bar: k as i64 + 1,
        kind,
        score: round_dec(clip(score, 0.0, 1.0), 3),
    };
    let on_grid = |k: usize, step: usize| k >= i0 && (k - i0).is_multiple_of(step);
    // The last bar a cue may start on: one window before the end, so every cue has music after it.
    let last = n.saturating_sub(WINDOW_BARS);

    let mut fixed = vec![cue(i0, "start", 1.0)];

    // drop: the biggest rise from a low stretch, on the 4-bar grid.
    let mut drop: Option<(usize, f64)> = None;
    for k in (i0 + GRID_BARS..last).step_by(GRID_BARS) {
        let before = mean(k.saturating_sub(2 * WINDOW_BARS).max(i0), k);
        let after = mean(k, k + WINDOW_BARS);
        let rise = change(k);
        if before <= DROP_LOW && after >= DROP_HIGH && rise >= DROP_MIN_RISE && drop.is_none_or(|(_, r)| rise > r) {
            drop = Some((k, rise));
        }
    }

    // mix_out: the last phrase start at or before the fall into the outro.
    let quiet = crate::np::percentile(e, 25.0);
    let loud = crate::np::percentile(e, 90.0);
    let level = quiet + OUTRO_LEVEL * (loud - quiet);
    let loud_until = (i0..n.saturating_sub(WINDOW_BARS - 1)).rev().find(|&k| mean(k, k + WINDOW_BARS) >= level);
    let mix_out = match loud_until {
        // The 4 loud bars run to k + WINDOW_BARS; the outro starts there unless that's the end.
        Some(k) if k + WINDOW_BARS + WINDOW_BARS <= n && loud - quiet > 0.2 => {
            let fall = k + WINDOW_BARS;
            let up = |x: usize| i0 + (x - i0).div_ceil(PHRASE_BARS) * PHRASE_BARS;
            // The phrase start nearest the fall, if one is close; else the 4-bar step at or before it.
            let near = up(fall.saturating_sub(SNAP_BARS).max(i0));
            Some(if near <= fall + SNAP_BARS { near } else { i0 + (fall - i0) / GRID_BARS * GRID_BARS })
        }
        _ => n.checked_sub(NO_OUTRO_BARS).filter(|&b| b > i0).map(|b| i0 + (b - i0) / PHRASE_BARS * PHRASE_BARS),
    };
    let mix_out = mix_out.filter(|&k| k > i0 && k < n && drop.is_none_or(|(d, _)| d != k));

    // Every section start after bar 1, each once, in order.
    let mut starts: Vec<usize> = section_starts.iter().cloned().filter(|&s| s > i0 && s < n).collect();
    starts.sort_unstable();
    starts.dedup();
    let drop = drop.map(|(k, _)| k);
    if let Some(k) = drop {
        fixed.push(cue(k, "drop", 0.5 + change(k)));
    }
    if let Some(k) = mix_out {
        fixed.push(cue(k, "mix_out", 0.5 - change(k)));
    }
    // A section start within 2 bars of the start, drop or mix-out is that same boundary: the grid cue stays,
    // since the grid counts from the first sound and the sections' 2-bar snap from the first downbeat.
    for &k in &starts {
        if fixed.iter().all(|c| (c.bar - 1).abs_diff(k as i64) > SNAP_BARS as u64) {
            fixed.push(cue(k, "section", 0.3 + 1.4 * change(k).abs()));
        }
    }

    // phrase cues: phrase starts where the energy changes, away from every other cue.
    let mut cands: Vec<Cue> = Vec::new();
    for k in (i0 + PHRASE_BARS..last).step_by(PHRASE_BARS) {
        let d = change(k).abs();
        if d >= PHRASE_MIN_CHANGE {
            let bonus = if on_grid(k, 4 * PHRASE_BARS) {
                0.1
            } else if on_grid(k, 2 * PHRASE_BARS) {
                0.05
            } else {
                0.0
            };
            cands.push(cue(k, "phrase", 1.4 * d + bonus));
        }
    }
    // Strongest first; on a tie the earlier one, so the order never depends on the sort.
    cands.sort_by(|a, b| b.score.total_cmp(&a.score).then(a.bar.cmp(&b.bar)));

    let mut out = fixed;
    for c in cands {
        if out.len() >= MAX_CUES {
            break;
        }
        if out.iter().all(|o| o.bar.abs_diff(c.bar) >= MIN_SPACING_BARS as u64) {
            out.push(c);
        }
    }
    out.truncate(MAX_CUES);
    out.sort_by_key(|c| c.bar);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Bars of 2 s from `offset`, with this energy per bar.
    fn grid(energy: &[f64], offset: f64) -> Vec<f64> {
        (0..energy.len()).map(|i| offset + 2.0 * i as f64).collect()
    }

    fn levels(parts: &[(usize, f64)]) -> Vec<f64> {
        parts.iter().flat_map(|&(n, v)| std::iter::repeat_n(v, n)).collect()
    }

    fn kinds(cues: &[Cue]) -> Vec<(i64, &'static str)> {
        cues.iter().map(|c| (c.bar, c.kind)).collect()
    }

    #[test]
    fn intro_build_drop_outro() {
        // 16 quiet bars, 8 building, 32 loud, 16 quiet: the drop on bar 25, the mix out on bar 57.
        let e = levels(&[(16, 0.1), (8, 0.35), (32, 0.9), (16, 0.15)]);
        let bars = grid(&e, 0.5);
        let cues = pick(&bars, &e, &[0, 16, 24, 56], 0.0);
        let k = kinds(&cues);
        assert_eq!(k[0], (1, "start"));
        assert!(k.contains(&(25, "drop")), "{k:?}");
        assert!(k.contains(&(57, "mix_out")), "{k:?}");
        assert!(k.contains(&(17, "section")), "{k:?}");
        assert!(cues.len() <= MAX_CUES);
        assert_eq!(cues.iter().find(|c| c.kind == "drop").unwrap().time_s, 48.5);
        for w in cues.windows(2) {
            assert!(w[1].bar > w[0].bar);
        }
    }

    #[test]
    fn start_skips_the_silence() {
        let e = levels(&[(40, 0.6)]);
        let bars = grid(&e, 0.0);
        // Sound starts at 5.1 s: bar 4 (6.0 s) is the first downbeat after it.
        assert_eq!(pick(&bars, &e, &[0], 5.1)[0], Cue { time_s: 6.0, bar: 4, kind: "start", score: 1.0 });
        // A hit a hair before a downbeat still makes that downbeat bar 1.
        assert_eq!(pick(&bars, &e, &[0], 6.1)[0].bar, 4);
    }

    #[test]
    fn every_section_start_is_a_cue() {
        // Sound from bar 3 (index 2); sections start at bars 13, 35 and 51 (indices 12, 34, 50), off the 4-bar
        // grid. The drop lands on the grid at bar 15 and the mix-out at bar 35, so they mark the first two
        // boundaries; bar 51 gets a section cue where it is.
        let e = levels(&[(2, 0.0), (10, 0.2), (22, 0.8), (40, 0.4)]);
        let bars = grid(&e, 0.0);
        let k = kinds(&pick(&bars, &e, &[0, 12, 34, 50], 4.0));
        assert_eq!(k, vec![(3, "start"), (15, "drop"), (35, "mix_out"), (51, "section")]);
    }

    #[test]
    fn spacing_cap_and_flat_tracks() {
        // Alternating energy every 4 bars and 50 section starts: the cap holds.
        let e: Vec<f64> = (0..200).map(|i| if (i / 4) % 2 == 0 { 0.2 } else { 0.9 }).collect();
        let bars = grid(&e, 0.0);
        let starts: Vec<usize> = (0..200).step_by(4).collect();
        let cues = pick(&bars, &e, &starts, 0.0);
        assert!(cues.len() <= MAX_CUES);
        let flat = levels(&[(64, 0.5)]);
        let cues = pick(&grid(&flat, 0.0), &flat, &[0], 0.0);
        // No energy change: the start, and a mix out 32 bars from the end.
        assert_eq!(kinds(&cues), vec![(1, "start"), (33, "mix_out")]);
        assert!(pick(&[1.0], &[0.5], &[0], 0.0).is_empty());
        assert!(pick(&grid(&flat, 0.0), &flat, &[0], 1e9).is_empty());
    }

    #[test]
    fn audible_start_finds_the_first_sound() {
        let mut rms = vec![0.0f32; 100];
        rms[40] = 1e-6; // noise far below the music
        for v in &mut rms[50..] {
            *v = 0.3;
        }
        assert!((audible_start(&rms, 100.0, 1) - 0.5).abs() < 1e-9);
        assert_eq!(audible_start(&[0.0; 10], 100.0, 1), 0.0);
    }
}
