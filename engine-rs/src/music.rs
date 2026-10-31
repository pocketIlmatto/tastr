//! Music-theory helpers shared by the stages (tastr/engine/music.py and calibration.py).

use crate::consts;
use crate::features::Matrix;
use crate::np::{interp, pairwise_sum, round_dec};

pub const NOTE_NAMES: [&str; 12] = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"];
const MODE_ROTATION: [&str; 7] = ["Major", "Dorian", "Phrygian", "Lydian", "Mixolydian", "Minor", "Locrian"];
const ROMAN: [&str; 7] = ["I", "II", "III", "IV", "V", "VI", "VII"];
const KEY_INPUT_SCALE: f64 = 12.0;

fn mode_steps(name: &str) -> Option<[i64; 7]> {
    Some(match name {
        "Major" => [0, 2, 4, 5, 7, 9, 11],
        "Dorian" => [0, 2, 3, 5, 7, 9, 10],
        "Phrygian" => [0, 1, 3, 5, 7, 8, 10],
        "Lydian" => [0, 2, 4, 6, 7, 9, 11],
        "Mixolydian" => [0, 2, 4, 5, 7, 9, 10],
        "Minor" => [0, 2, 3, 5, 7, 8, 10],
        "Locrian" => [0, 1, 3, 5, 6, 8, 10],
        _ => return None,
    })
}

pub fn note_name(pc: i64) -> &'static str {
    NOTE_NAMES[pc.rem_euclid(12) as usize]
}

pub fn note_index(name: &str) -> i64 {
    NOTE_NAMES.iter().position(|n| *n == name).unwrap_or(0) as i64
}

/// Camelot wheel code (8A = A minor, 8B = C major).
pub fn camelot(tonic: i64, mode: &str) -> String {
    let major_pc = if mode == "major" { tonic } else { (tonic + 3) % 12 };
    let n = (7 * major_pc + 8) % 12;
    format!("{}{}", if n == 0 { 12 } else { n }, if mode == "major" { "B" } else { "A" })
}

/// Mean of the per-frame normalised chroma (12 x frames).
pub fn key_profile(chroma: &Matrix) -> [f64; 12] {
    let t = chroma.cols;
    let mut sums = vec![0.0f64; t];
    for r in 0..12 {
        for (s, &v) in sums.iter_mut().zip(chroma.row(r)) {
            *s += v as f64;
        }
    }
    let mut out = [0.0f64; 12];
    for (r, o) in out.iter_mut().enumerate() {
        let row: Vec<f64> = chroma.row(r).iter().zip(&sums).map(|(&v, s)| v as f64 / (s + 1e-9)).collect();
        *o = if t == 0 { f64::NAN } else { pairwise_sum(&row) / t as f64 };
    }
    out
}

pub struct KeyEstimate {
    pub tonic: i64,
    pub mode: &'static str,
    pub confidence: f64,
    pub alternatives: Vec<(String, f64)>,
}

fn key_name(i: usize) -> String {
    format!("{} {}", note_name((i / 2) as i64), if i % 2 == 1 { "minor" } else { "major" })
}

/// music.estimate_key: the learned key model's most likely key, with the next three as alternatives.
pub fn estimate_key(profile: &[f64; 12]) -> KeyEstimate {
    let mut z = [0.0f64; 24];
    for (k, zk) in z.iter_mut().enumerate() {
        let mut acc = 0.0;
        for (w, p) in consts::KEY_WEIGHTS[k].iter().zip(profile) {
            acc += w * (KEY_INPUT_SCALE * p);
        }
        *zk = acc + consts::KEY_BIAS[k];
    }
    let zmax = z.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let e: Vec<f64> = z.iter().map(|v| libm::exp(v - zmax)).collect();
    let total = pairwise_sum(&e);
    let p: Vec<f64> = e.iter().map(|v| v / total).collect();
    let mut order: Vec<usize> = (0..24).collect();
    order.sort_by(|&a, &b| p[b].total_cmp(&p[a]));
    let (best, second) = (order[0], order[1]);
    KeyEstimate {
        tonic: (best / 2) as i64,
        mode: if best % 2 == 1 { "minor" } else { "major" },
        confidence: round_dec(p[best] - p[second], 3),
        alternatives: order[1..4].iter().map(|&i| (key_name(i), round_dec(p[i], 3))).collect(),
    }
}

/// music.refine_mode: a modal flavour from the scale degrees that stand out.
pub fn refine_mode(profile: &[f64; 12], tonic: i64, mode: &str) -> &'static str {
    let mut p = [0.0f64; 12];
    for (i, v) in p.iter_mut().enumerate() {
        *v = profile[(i as i64 + tonic).rem_euclid(12) as usize];
    }
    let m = p.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let m = if m == 0.0 { 1.0 } else { m };
    for v in p.iter_mut() {
        *v /= m;
    }
    if mode == "minor" {
        if p[1] > p[2] * 1.1 && p[1] > 0.35 {
            return "Phrygian";
        }
        if p[9] > p[8] * 1.15 && p[9] > 0.35 {
            return "Dorian";
        }
        if p[11] > p[10] * 1.2 && p[11] > 0.4 {
            return "Harmonic Minor";
        }
        return "Minor";
    }
    if p[10] > p[11] * 1.15 && p[10] > 0.35 {
        return "Mixolydian";
    }
    if p[6] > p[5] * 1.15 && p[6] > 0.35 {
        return "Lydian";
    }
    "Major"
}

pub struct Chord {
    pub name: String,
    pub root: Option<i64>,
    pub quality: Option<&'static str>,
    pub score: f64,
}

fn templates() -> Vec<(String, i64, &'static str, [f64; 12])> {
    let mut out = Vec::new();
    for root in 0..12i64 {
        for (quality, iv) in [("maj", [0, 4, 7]), ("min", [0, 3, 7])] {
            let mut t = [0.0f64; 12];
            for (i, s) in iv.iter().enumerate() {
                t[((root + s) % 12) as usize] = if i == 0 { 1.2 } else { 1.0 };
            }
            let norm = libm::sqrt(t.iter().map(|v| v * v).sum::<f64>());
            for v in t.iter_mut() {
                *v /= norm;
            }
            let name = format!("{}{}", note_name(root), if quality == "maj" { "" } else { "m" });
            out.push((name, root, quality, t));
        }
    }
    out
}

/// music.match_chord: the best major or minor triad for a chroma vector.
pub fn match_chord(v: &[f64; 12]) -> Chord {
    let norm = libm::sqrt(v.iter().map(|x| x * x).sum::<f64>());
    if norm == 0.0 {
        return Chord { name: "N".into(), root: None, quality: None, score: 0.0 };
    }
    let u: Vec<f64> = v.iter().map(|x| x / norm).collect();
    let mut best: Option<(f64, (String, i64, &'static str))> = None;
    for (name, root, quality, t) in templates() {
        let s: f64 = t.iter().zip(&u).map(|(a, b)| a * b).sum();
        if best.as_ref().is_none_or(|(bs, _)| s > *bs) {
            best = Some((s, (name, root, quality)));
        }
    }
    let (s, (name, root, quality)) = best.unwrap();
    Chord { name, root: Some(root), quality: Some(quality), score: round_dec(s, 3) }
}

pub fn scale_pcs(tonic: i64, scale: &str) -> Vec<i64> {
    let steps: [i64; 7] = if scale == "Harmonic Minor" {
        [0, 2, 3, 5, 7, 8, 11]
    } else {
        mode_steps(scale).unwrap_or_else(|| mode_steps(if scale == "major" { "Major" } else { "Minor" }).unwrap())
    };
    steps.iter().map(|s| (tonic + s).rem_euclid(12)).collect()
}

pub fn degree_of(pc: i64, tonic: i64, scale: &str) -> Option<usize> {
    scale_pcs(tonic, scale).iter().position(|&p| p == pc.rem_euclid(12))
}

pub fn roman_numeral(root: i64, quality: &str, tonic: i64, scale: &str) -> String {
    let base = match degree_of(root, tonic, scale) {
        Some(d) => ROMAN[d].to_string(),
        None => match (root - tonic).rem_euclid(12) {
            1 => "bII",
            3 => "bIII",
            6 => "#IV",
            8 => "bVI",
            10 => "bVII",
            11 => "VII",
            _ => "?",
        }
        .to_string(),
    };
    if quality == "maj" {
        base
    } else {
        base.to_lowercase()
    }
}

pub fn chord_scale(root: i64, quality: &str, tonic: i64, key_scale: &str) -> String {
    if mode_steps(key_scale).is_none() || degree_of(root, tonic, key_scale).is_none() {
        return format!("{} {}", note_name(root), if quality == "maj" { "Mixolydian" } else { "Dorian" });
    }
    let major = mode_steps("Major").unwrap();
    let idx = MODE_ROTATION.iter().position(|m| *m == key_scale).unwrap();
    let major_tonic = (tonic - major[idx]).rem_euclid(12);
    let deg = major.iter().position(|&s| s == (root - major_tonic).rem_euclid(12)).unwrap();
    format!("{} {}", note_name(root), MODE_ROTATION[deg])
}

/// calibration.likely_correct: the calibrated chance an answer with this raw confidence is right.
pub fn likely_correct(kind: &str, raw: f64) -> f64 {
    let knots: &[(f64, f64)] = if kind == "bpm" { &consts::BPM_KNOTS } else { &consts::KEY_KNOTS };
    let xs: Vec<f64> = knots.iter().map(|k| k.0).collect();
    let ys: Vec<f64> = knots.iter().map(|k| k.1).collect();
    round_dec(interp(raw, &xs, &ys), 3)
}
