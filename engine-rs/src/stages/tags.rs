//! Vibe tags (tastr/engine/stages/tags.py): rules over core numbers and features.

use super::Ctx;
use crate::card::Card;
use crate::json::Json;
use crate::np::round_dec;

const MIN_SCORE: f64 = 0.2;

const TEMPO_STYLES: [(&str, f64, f64); 8] = [
    ("downtempo", 70.0, 100.0),
    ("trap tempo", 65.0, 85.0),
    ("trap tempo", 130.0, 170.0),
    ("hip-hop tempo", 80.0, 100.0),
    ("house tempo", 118.0, 130.0),
    ("techno tempo", 125.0, 145.0),
    ("dubstep/halftime tempo", 138.0, 150.0),
    ("drum & bass tempo", 160.0, 180.0),
];

/// Every positive tag score, high to low (ties keep the order the rules ran in).
pub fn vibe_scores(bpm: Option<f64>, mode: Option<&str>, energy: Option<f64>, card: &Card) -> Vec<(String, f64)> {
    let f = |k: &str| card.feature(k);
    let minor = mode.map(|m| m == "minor");
    let bright = f("brightness");
    let perc = f("percussiveness");
    let pulse = f("pulse_clarity");
    let dance = f("danceability");
    let low = f("low_end");
    let contrast = f("section_contrast");
    let mut scores: Vec<(String, f64)> = Vec::new();
    let mut add = |tag: &str, score: f64| {
        if score > 0.0 {
            let s = round_dec(score.min(1.0), 3);
            if let Some(slot) = scores.iter_mut().find(|(t, _)| t == tag) {
                slot.1 = slot.1.max(s);
            } else {
                scores.push((tag.to_string(), s));
            }
        }
    };
    if let Some(e) = energy {
        add("high energy", (e - 0.6) / 0.3);
        if let Some(b) = bpm {
            add("chill", if b < 115.0 { (0.45 - e) / 0.25 } else { (0.35 - e) / 0.25 });
        }
        if minor == Some(true) && e < 0.5 {
            add("melancholic", 0.5 + (0.4 - e));
        }
    }
    if let (Some(br), Some(mi)) = (bright, minor) {
        add("dark", (0.45 - br) / 0.3 + if mi { 0.25 } else { -0.2 });
        add("bright", (br - 0.5) / 0.3 + if mi { -0.15 } else { 0.15 });
        if let (Some(e), false) = (energy, mi) {
            add("euphoric", (e - 0.55) / 0.25 + (br - 0.45));
        }
    }
    if let (Some(p), Some(b)) = (pulse, bpm) {
        if (115.0..=150.0).contains(&b) {
            add("driving", (p - 0.45) / 0.35);
        }
    }
    if let Some(d) = dance {
        add("danceable", (d - 0.55) / 0.3);
    }
    if let (Some(p), Some(c)) = (pulse, contrast) {
        add("hypnotic", (p - 0.4) / 0.4 - 3.0 * c + 0.3);
    }
    if let Some(l) = low {
        add("bass-heavy", (l - 0.25) / 0.25);
    }
    if let Some(pc) = perc {
        add("percussive", (pc - 0.35) / 0.25);
        if let Some(p) = pulse {
            add("atmospheric", (0.2 - pc) / 0.15 + (0.4 - p));
        }
    }
    if let Some(c) = contrast {
        add("dynamic", (c - 0.2) / 0.15);
    }
    if let (Some(sc), Some(sp)) = (f("swing_confidence"), f("swing_percent")) {
        if sc > 0.3 {
            add("swung", (sp - 54.0) / 8.0);
        }
    }
    if let Some(b) = bpm {
        for (name, lo, hi) in TEMPO_STYLES {
            if lo <= b && b <= hi {
                add(name, 0.5);
            }
        }
    }
    scores.sort_by(|a, b| b.1.total_cmp(&a.1));
    scores
}

pub fn run(_ctx: &mut Ctx, card: &mut Card) -> Result<(), String> {
    let mode = card.core.key.as_ref().map(|k| k.mode);
    let scores = vibe_scores(card.core.bpm, mode, card.core.energy, card);
    card.tags = scores.iter().filter(|(_, s)| *s >= MIN_SCORE).cloned().collect();
    let obj = Json::Obj(scores.into_iter().map(|(t, s)| (t, Json::Num(s))).collect());
    card.extras.set("tags.vibe_scores", obj);
    Ok(())
}
