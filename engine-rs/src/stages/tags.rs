//! Vibe tags: rules over core numbers and features (docs/vibes.md).

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

/// Tag scores in the order the rules ran; a tag scored twice keeps its higher score.
#[derive(Default)]
struct Scores(Vec<(String, f64)>);

impl Scores {
    fn add(&mut self, tag: &str, score: f64) {
        if score > 0.0 {
            let s = round_dec(score.min(1.0), 3);
            if let Some(slot) = self.0.iter_mut().find(|(t, _)| t == tag) {
                slot.1 = slot.1.max(s);
            } else {
                self.0.push((tag.to_string(), s));
            }
        }
    }
}

/// The inputs the rules read.
struct Vibe {
    bpm: Option<f64>,
    minor: Option<bool>,
    energy: Option<f64>,
    bright: Option<f64>,
    perc: Option<f64>,
    pulse: Option<f64>,
    dance: Option<f64>,
    low: Option<f64>,
    contrast: Option<f64>,
    swing: Option<(f64, f64)>, // (confidence, percent)
}

fn energy_rules(v: &Vibe, out: &mut Scores) {
    let Some(e) = v.energy else { return };
    out.add("high energy", (e - 0.6) / 0.3);
    if let Some(b) = v.bpm {
        out.add("chill", if b < 115.0 { (0.45 - e) / 0.25 } else { (0.35 - e) / 0.25 });
    }
    if v.minor == Some(true) && e < 0.5 {
        out.add("melancholic", 0.5 + (0.4 - e));
    }
}

fn brightness_rules(v: &Vibe, out: &mut Scores) {
    let (Some(br), Some(mi)) = (v.bright, v.minor) else { return };
    out.add("dark", (0.45 - br) / 0.3 + if mi { 0.25 } else { -0.2 });
    out.add("bright", (br - 0.5) / 0.3 + if mi { -0.15 } else { 0.15 });
    if let (Some(e), false) = (v.energy, mi) {
        out.add("euphoric", (e - 0.55) / 0.25 + (br - 0.45));
    }
}

fn groove_rules(v: &Vibe, out: &mut Scores) {
    if let (Some(p), Some(b)) = (v.pulse, v.bpm) {
        if (115.0..=150.0).contains(&b) {
            out.add("driving", (p - 0.73) / 0.35);
        }
    }
    if let Some(d) = v.dance {
        out.add("danceable", (d - 0.55) / 0.3);
    }
    if let (Some(p), Some(c)) = (v.pulse, v.contrast) {
        out.add("hypnotic", (p - 0.4) / 0.4 - 3.0 * c - 0.3);
    }
}

fn texture_rules(v: &Vibe, out: &mut Scores) {
    if let Some(l) = v.low {
        out.add("bass-heavy", (l - 0.25) / 0.25);
    }
    if let Some(pc) = v.perc {
        out.add("percussive", (pc - 0.35) / 0.25);
        if let Some(p) = v.pulse {
            out.add("atmospheric", (0.2 - pc) / 0.15 + (0.4 - p) - 0.6);
        }
    }
    if let Some(c) = v.contrast {
        out.add("dynamic", (c - 0.2) / 0.15);
    }
    if let Some((sc, sp)) = v.swing {
        if sc > 0.3 {
            out.add("swung", (sp - 54.0) / 8.0);
        }
    }
}

fn tempo_rules(v: &Vibe, out: &mut Scores) {
    let Some(b) = v.bpm else { return };
    for (name, lo, hi) in TEMPO_STYLES {
        if lo <= b && b <= hi {
            out.add(name, 0.5);
        }
    }
}

/// Every positive tag score, high to low (ties keep the order the rules ran in).
pub fn vibe_scores(bpm: Option<f64>, mode: Option<&str>, energy: Option<f64>, card: &Card) -> Vec<(String, f64)> {
    let f = |k: &str| card.feature(k);
    let v = Vibe {
        bpm,
        minor: mode.map(|m| m == "minor"),
        energy,
        bright: f("brightness"),
        perc: f("percussiveness"),
        pulse: f("pulse_clarity"),
        dance: f("danceability"),
        low: f("low_end"),
        contrast: f("section_contrast"),
        swing: f("swing_confidence").zip(f("swing_percent")),
    };
    let mut out = Scores::default();
    for rules in [energy_rules, brightness_rules, groove_rules, texture_rules, tempo_rules] {
        rules(&v, &mut out);
    }
    out.0.sort_by(|a, b| b.1.total_cmp(&a.1));
    out.0
}

pub fn run(_ctx: &mut Ctx, card: &mut Card) -> Result<(), String> {
    tag(card);
    Ok(())
}

/// The tags from the card's core numbers and features: those scoring at least MIN_SCORE in card.tags, every
/// positive score in extras["tags.vibe_scores"].
fn tag(card: &mut Card) {
    let mode = card.core.key.as_ref().map(|k| k.mode);
    let scores = vibe_scores(card.core.bpm, mode, card.core.energy, card);
    card.tags = scores.iter().filter(|(_, s)| *s >= MIN_SCORE).cloned().collect();
    let obj = Json::Obj(scores.into_iter().map(|(t, s)| (t, Json::Num(s))).collect());
    card.extras.set("tags.vibe_scores", obj);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::card::Key;

    /// The tags listed (score at least MIN_SCORE) and every score, for a card with these numbers.
    fn tags(bpm: Option<f64>, mode: Option<&'static str>, energy: Option<f64>, features: &[(&str, f64)]) -> Card {
        let mut card = Card::default();
        card.core.bpm = bpm;
        card.core.energy = energy;
        card.core.key = mode.map(|mode| Key {
            tonic: 0,
            mode,
            scale: "",
            camelot: String::new(),
            confidence: 1.0,
            likely_correct: 1.0,
        });
        for (k, v) in features {
            card.set_feature(k, *v);
        }
        tag(&mut card);
        card
    }

    fn names(card: &Card) -> Vec<&str> {
        card.tags.iter().map(|(t, _)| t.as_str()).collect()
    }

    fn tempo(bpm: f64) -> Vec<String> {
        let mut v: Vec<String> = names(&tags(Some(bpm), None, None, &[])).into_iter().map(String::from).collect();
        v.sort();
        v
    }

    #[test]
    fn bright_major_house() {
        let card = tags(Some(126.0), Some("major"), Some(0.9), &[("brightness", 0.7)]);
        let t = names(&card);
        for want in ["high energy", "bright", "euphoric", "house tempo", "techno tempo"] {
            assert!(t.contains(&want), "{want} missing from {t:?}");
        }
        assert!(!t.contains(&"chill") && !t.contains(&"dark"));
    }

    #[test]
    fn dark_minor_downtempo() {
        let card = tags(Some(85.0), Some("minor"), Some(0.2), &[("brightness", 0.2)]);
        let t = names(&card);
        for want in ["chill", "dark", "melancholic", "downtempo"] {
            assert!(t.contains(&want), "{want} missing from {t:?}");
        }
        assert!(!t.contains(&"high energy"));
    }

    #[test]
    fn only_bpm_gives_only_tempo_tags() {
        assert_eq!(tempo(126.0), ["house tempo", "techno tempo"]);
    }

    #[test]
    fn sorted_thresholded_unique() {
        let feats = [("brightness", 0.7), ("pulse_clarity", 0.9), ("percussiveness", 0.6)];
        let card = tags(Some(126.0), Some("major"), Some(0.9), &feats);
        let scores: Vec<f64> = card.tags.iter().map(|(_, s)| *s).collect();
        assert!(scores.windows(2).all(|w| w[0] >= w[1]));
        assert!(scores.iter().all(|s| (MIN_SCORE..=1.0).contains(s)));
        let mut t = names(&card);
        t.sort();
        t.dedup();
        assert_eq!(t.len(), card.tags.len());
    }

    #[test]
    fn swing_needs_confidence() {
        let low = tags(Some(100.0), None, None, &[("swing_percent", 62.0), ("swing_confidence", 0.1)]);
        let high = tags(Some(100.0), None, None, &[("swing_percent", 62.0), ("swing_confidence", 0.6)]);
        assert!(!names(&low).contains(&"swung") && names(&high).contains(&"swung"));
    }

    #[test]
    fn scores_under_the_cutoff_are_kept_but_not_listed() {
        // energy 0.62 gives "high energy" about 0.07: positive, but under the cut-off
        let card = tags(None, None, Some(0.62), &[]);
        let scores = vibe_scores(None, None, Some(0.62), &card);
        let he = scores.iter().find(|(t, _)| t == "high energy").unwrap().1;
        assert!(0.0 < he && he < MIN_SCORE);
        assert!(!names(&card).contains(&"high energy"));
    }

    #[test]
    fn trap_tempo_covers_full_and_half_tempo_reads() {
        // 140 sits in trap, dubstep/halftime and techno ranges at once: tempo tags are hints, not a genre call
        assert_eq!(tempo(140.0), ["dubstep/halftime tempo", "techno tempo", "trap tempo"]);
        assert_eq!(tempo(165.0), ["drum & bass tempo", "trap tempo"]);
        assert_eq!(tempo(132.0), ["techno tempo", "trap tempo"]);
        assert_eq!(tempo(70.0), ["downtempo", "trap tempo"]); // trap's halftime feel often reads at half tempo
        assert!(!tempo(110.0).contains(&"trap tempo".to_string()));
        assert!(!tempo(172.0).contains(&"trap tempo".to_string()));
        let card = tags(Some(140.0), None, None, &[]);
        assert_eq!(card.tags.iter().find(|(t, _)| t == "trap tempo").unwrap().1, 0.5);
    }
}
