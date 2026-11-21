//! What a DJ tool needs from a track, and nothing else: BPM, beats, downbeats, meter, key, sections and cue points.
//!
//! This is the API for using the engine from other Rust programs (docs/rust-engine.md, "Using the engine as a
//! library"). It runs only the core and sections stages, so it skips the drum patterns, vibe tags and
//! suggestions the app's card also has. The answers are the same as the card's for the same audio.
//!
//! ```no_run
//! let pcm: Vec<f32> = vec![0.0; 44_100 * 2 * 30]; // 30 s of interleaved stereo at 44.1 kHz
//! let r = tastr_engine::dj::analyze_pcm(&pcm, 44_100, 2, &Default::default()).unwrap();
//! println!("{:?} BPM, key {:?}", r.bpm, r.key.map(|k| k.camelot));
//! ```
//!
//! The result may gain fields in later versions (it is `#[non_exhaustive]`), so read it by field name.

use crate::card::{Card, Key, Section};
pub use crate::cues::Cue;
use crate::json::Json;
use crate::stages::{self, Ctx};
use crate::SR;

/// The stages a DJ result needs, in run order.
const STAGES: [&str; 2] = ["core", "sections"];

/// Choices for an analysis: `DjOptions::default()`, then the `with_*` methods for anything else.
#[derive(Clone, Debug, Default)]
#[non_exhaustive]
pub struct DjOptions {
    /// 3 or 4 to set the meter instead of detecting it. Other values are ignored.
    pub beats_per_bar: Option<i64>,
}

impl DjOptions {
    /// Set the meter (3 or 4) instead of detecting it.
    pub fn with_beats_per_bar(mut self, beats_per_bar: i64) -> DjOptions {
        self.beats_per_bar = Some(beats_per_bar);
        self
    }
}

/// The analysis of one track. Times are seconds from the start of the audio given.
#[derive(Clone, Debug)]
#[non_exhaustive]
pub struct DjResult {
    /// The engine's crate version.
    pub engine_version: &'static str,
    /// Length of the audio in seconds.
    pub duration: f64,
    /// Tempo in beats per minute, two decimals; None when the audio has no steady beat (or is silent).
    pub bpm: Option<f64>,
    /// The share of benchmark tracks with this tempo evidence whose BPM was right (0 to 1).
    pub bpm_likely_correct: Option<f64>,
    /// Half and double the BPM, the usual alternatives when a listener counts the beat differently.
    pub bpm_alternatives: Vec<f64>,
    /// Every beat. They sit on the analysis frame grid (about 23 ms apart at 22,050 Hz, hop 512).
    pub beat_times: Vec<f64>,
    /// The first beat of every bar.
    pub downbeat_times: Vec<f64>,
    /// 3 or 4.
    pub beats_per_bar: i64,
    /// True when `beats_per_bar` came from `DjOptions`, false when it was detected.
    pub meter_set: bool,
    /// The key, None for silence or audio without pitch.
    pub key: Option<Key>,
    /// Song sections with rough labels ("intro", "verse", "drop", ...), start and end on bar lines.
    pub sections: Vec<Section>,
    /// Suggested DJ cue points in time order, at most 16: `start` (bar 1 after any silence at the start),
    /// `section` and `phrase` (on the phrase grid), `drop` and `mix_out`. Empty without a beat grid.
    pub cues: Vec<Cue>,
    /// Mean loudness in dB (RMS, not LUFS).
    pub loudness_db: Option<f64>,
    /// Stages that failed, as (stage, message). Empty when everything ran.
    pub errors: Vec<(String, String)>,
}

/// Analyze interleaved PCM: `samples` holds `channels` values per frame at `sample_rate` Hz, in -1..1.
/// Channels are averaged and the audio resampled to the engine's 22,050 Hz first.
pub fn analyze_pcm(samples: &[f32], sample_rate: u32, channels: u16, opts: &DjOptions) -> Result<DjResult, String> {
    if sample_rate == 0 {
        return Err("sample rate must be above 0".into());
    }
    if channels == 0 {
        return Err("channels must be at least 1".into());
    }
    let ch = channels as usize;
    if !samples.len().is_multiple_of(ch) {
        return Err(format!("{} samples is not a whole number of {ch}-channel frames", samples.len()));
    }
    let mono: Vec<f32> = if ch == 1 {
        samples.to_vec()
    } else {
        samples.chunks_exact(ch).map(|f| f.iter().sum::<f32>() / ch as f32).collect()
    };
    Ok(analyze_mono(crate::resample::resample(&mono, sample_rate, SR), opts))
}

/// Analyze an audio file's bytes (WAV, MP3, FLAC, Ogg Vorbis, AAC/M4A, ...), decoded by the engine.
#[cfg(feature = "decode")]
pub fn analyze_file(bytes: Vec<u8>, opts: &DjOptions) -> Result<DjResult, String> {
    Ok(analyze_mono(crate::load(bytes)?, opts))
}

/// Analyze mono audio already at the engine's rate (`tastr_engine::SR`, 22,050 Hz).
pub fn analyze_mono(y: Vec<f32>, opts: &DjOptions) -> DjResult {
    let mut ctx = Ctx::with_bass(y, opts.beats_per_bar, false);
    from_card(stages::run_only(&mut ctx, &STAGES))
}

fn from_card(card: Card) -> DjResult {
    let alternatives = match card.extras.get("core.bpm_alternatives") {
        Some(Json::Arr(v)) => v.iter().filter_map(|j| if let Json::Num(n) = j { Some(*n) } else { None }).collect(),
        _ => Vec::new(),
    };
    let c = card.core;
    DjResult {
        engine_version: crate::VERSION,
        duration: card.duration,
        bpm: c.bpm,
        bpm_likely_correct: c.bpm_likely_correct,
        bpm_alternatives: alternatives,
        beat_times: c.beat_times,
        downbeat_times: c.downbeat_times,
        beats_per_bar: c.beats_per_bar,
        meter_set: c.meter_set,
        key: c.key,
        sections: card.sections,
        cues: card.cues,
        loudness_db: c.loudness_db,
        errors: card.errors,
    }
}

impl DjResult {
    /// The result as JSON (the `tastr-engine dj` command prints this).
    pub fn to_json(&self) -> Json {
        let key = self.key.as_ref().map_or(Json::Null, |k| {
            Json::obj()
                .with("tonic", k.tonic)
                .with("name", format!("{} {}", crate::music::NOTE_NAMES[k.tonic as usize % 12], k.mode))
                .with("mode", k.mode)
                .with("camelot", k.camelot.clone())
                .with("likely_correct", k.likely_correct)
                .with("mode_uncertain", k.mode_uncertain)
        });
        let sections: Vec<Json> = self
            .sections
            .iter()
            .map(|s| {
                Json::obj()
                    .with("start", s.start)
                    .with("end", s.end)
                    .with("label", s.label.clone())
                    .with("start_bar", s.start_bar)
                    .with("end_bar", s.end_bar)
                    .with("energy", s.energy)
            })
            .collect();
        Json::obj()
            .with("engine", "tastr-engine")
            .with("engine_version", self.engine_version)
            .with("duration", self.duration)
            .with("bpm", self.bpm)
            .with("bpm_likely_correct", self.bpm_likely_correct)
            .with("bpm_alternatives", self.bpm_alternatives.clone())
            .with("beats_per_bar", self.beats_per_bar)
            .with("meter_set", self.meter_set)
            .with("key", key)
            .with("loudness_db", self.loudness_db)
            .with("beat_times", self.beat_times.clone())
            .with("downbeat_times", self.downbeat_times.clone())
            .with("sections", Json::Arr(sections))
            .with("cues", Json::Arr(self.cues.iter().map(Cue::json).collect()))
            .with("errors", Json::Obj(self.errors.iter().map(|(k, m)| (k.clone(), Json::from(m.clone()))).collect()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A kick-like click on every beat (accented on bar 1) over an A minor triad, `seconds` long.
    fn click_track(sr: u32, bpm: f64, offset: f64, seconds: f64) -> Vec<f32> {
        let n = (sr as f64 * seconds) as usize;
        let mut y = vec![0.0f32; n];
        let period = 60.0 / bpm;
        let len = (sr as f64 * 0.045) as usize;
        let mut k = 0;
        loop {
            let start = ((offset + k as f64 * period) * sr as f64) as usize;
            if start + len >= n {
                break;
            }
            let gain = if k % 4 == 0 { 1.0 } else { 0.6 };
            for i in 0..len {
                let t = i as f64 / sr as f64;
                y[start + i] += (gain * (-t / 0.007).exp() * (2.0 * std::f64::consts::PI * 60.0 * t).sin()) as f32;
            }
            k += 1;
        }
        for (i, v) in y.iter_mut().enumerate() {
            let t = i as f64 / sr as f64;
            let pad: f64 = [220.0, 261.63, 329.63].iter().map(|f| (2.0 * std::f64::consts::PI * f * t).sin()).sum();
            *v = 0.6 * *v + (0.05 * pad) as f32;
        }
        y
    }

    fn stereo(y: &[f32]) -> Vec<f32> {
        y.iter().flat_map(|&v| [v, v]).collect()
    }

    #[test]
    fn click_track_bpm_key_and_downbeats() {
        let y = click_track(44_100, 124.0, 0.25, 40.0);
        let r = analyze_pcm(&stereo(&y), 44_100, 2, &DjOptions::default()).unwrap();
        assert!(r.errors.is_empty(), "{:?}", r.errors);
        assert!((r.bpm.unwrap() - 124.0).abs() < 0.5, "bpm {:?}", r.bpm);
        assert_eq!(r.key.as_ref().unwrap().camelot, "8A");
        assert_eq!(r.beats_per_bar, 4);
        // Beats land on the clicks within one analysis frame (plus the onset peak's lead), and downbeats are
        // every fourth beat. Which click is bar 1 isn't checked: the downbeat finder listens for changes in
        // timbre and bass, which a click track doesn't have.
        let beat = 60.0 / 124.0;
        for &t in &r.beat_times[..8] {
            let k = ((t - 0.25) / beat).round();
            assert!((t - (0.25 + k * beat)).abs() < 0.03, "beat {t}");
        }
        let first = r.beat_times.iter().position(|&t| t == r.downbeat_times[0]).unwrap();
        for (i, &t) in r.downbeat_times.iter().take(6).enumerate() {
            assert_eq!(t, r.beat_times[first + 4 * i]);
        }
        assert!(!r.sections.is_empty());
        assert!((r.duration - 40.0).abs() < 0.01);
        assert_eq!(r.bpm_alternatives.len(), 2);
    }

    #[test]
    fn mono_stereo_and_sample_rates_agree() {
        let a = analyze_pcm(&click_track(44_100, 128.0, 0.1, 30.0), 44_100, 1, &DjOptions::default()).unwrap();
        let b = analyze_pcm(&stereo(&click_track(44_100, 128.0, 0.1, 30.0)), 44_100, 2, &DjOptions::default()).unwrap();
        let c = analyze_pcm(&click_track(48_000, 128.0, 0.1, 30.0), 48_000, 1, &DjOptions::default()).unwrap();
        assert_eq!(a.bpm, b.bpm);
        assert_eq!(a.beat_times, b.beat_times);
        for r in [&b, &c] {
            assert!((r.bpm.unwrap() - a.bpm.unwrap()).abs() < 0.5);
            assert_eq!(r.key.as_ref().map(|k| k.camelot.clone()), a.key.as_ref().map(|k| k.camelot.clone()));
        }
        let frame = crate::stages::HOP as f64 / SR as f64;
        for (x, y) in a.downbeat_times.iter().zip(&c.downbeat_times).take(5) {
            assert!((x - y).abs() <= frame + 1e-3, "{x} vs {y}");
        }
    }

    #[test]
    fn same_answers_as_the_card() {
        let y = crate::resample::resample(&click_track(44_100, 124.0, 0.25, 30.0), 44_100, SR);
        let card = crate::analyze(y.clone(), None, None);
        let r = analyze_mono(y, &DjOptions::default());
        assert_eq!(r.bpm, card.core.bpm);
        assert_eq!(r.beat_times, card.core.beat_times);
        assert_eq!(r.downbeat_times, card.core.downbeat_times);
        assert_eq!(r.key.map(|k| k.camelot), card.core.key.map(|k| k.camelot));
        assert_eq!(
            r.sections.iter().map(|s| s.start).collect::<Vec<_>>(),
            card.sections.iter().map(|s| s.start).collect::<Vec<_>>()
        );
    }

    #[test]
    fn meter_option_and_bad_input() {
        let y = click_track(44_100, 124.0, 0.25, 20.0);
        let opts = DjOptions::default().with_beats_per_bar(3);
        let r = analyze_pcm(&y, 44_100, 1, &opts).unwrap();
        assert_eq!(r.beats_per_bar, 3);
        assert!(r.meter_set);
        assert!(analyze_pcm(&y, 0, 1, &opts).is_err());
        assert!(analyze_pcm(&y, 44_100, 0, &opts).is_err());
        assert!(analyze_pcm(&[0.0; 3], 44_100, 2, &opts).is_err());
    }

    #[test]
    fn silence_has_no_tempo_or_key() {
        let r = analyze_pcm(&vec![0.0; 44_100 * 12], 44_100, 1, &DjOptions::default()).unwrap();
        assert_eq!(r.bpm, None);
        assert!(r.key.is_none());
        assert!(r.beat_times.is_empty());
    }

    /// 2.3 s of silence, then 16 bars of soft hats and a pad, 32 bars with a kick on every beat, 16 bars of pad
    /// again, at 124 BPM: (audio at 44.1 kHz, bar length in seconds).
    fn intro_drop_outro() -> (Vec<f32>, f64) {
        let sr = 44_100.0;
        let beat = 60.0 / 124.0;
        let lead = 2.3;
        let n = ((lead + 64.0 * 4.0 * beat + 1.0) * sr) as usize;
        let mut y = vec![0.0f32; n];
        let two_pi = 2.0 * std::f64::consts::PI;
        for b in 0..64 * 4 {
            let t0 = lead + b as f64 * beat;
            let loud = (64..192).contains(&b);
            let start = (t0 * sr) as usize;
            for i in 0..(0.06 * sr) as usize {
                let t = i as f64 / sr;
                let kick = if loud { (-t / 0.02).exp() * (two_pi * 55.0 * t).sin() } else { 0.0 };
                // a short high tick as the hat, off a cheap deterministic hash so it sounds noisy
                let h = ((i as u64).wrapping_mul(2_654_435_761) % 1000) as f64 / 500.0 - 1.0;
                let hat = 0.15 * (-t / 0.005).exp() * h;
                y[start + i] += (kick * if b % 4 == 0 { 1.0 } else { 0.8 } + hat) as f32;
            }
        }
        let first = (lead * sr) as usize;
        for (i, v) in y.iter_mut().enumerate().skip(first) {
            let t = i as f64 / sr;
            let pad: f64 = [220.0, 261.63, 329.63].iter().map(|f| (two_pi * f * t).sin()).sum();
            *v = 0.6 * *v + (0.04 * pad) as f32;
        }
        (y, 4.0 * beat)
    }

    #[test]
    fn cues_on_a_track_with_an_intro_a_drop_and_an_outro() {
        let (y, bar) = intro_drop_outro();
        // The meter is set: with no accents in the intro the detector hears 3/4, which isn't what this checks.
        let r = analyze_pcm(&y, 44_100, 1, &DjOptions::default().with_beats_per_bar(4)).unwrap();
        assert!(!r.cues.is_empty() && r.cues.len() <= crate::cues::MAX_CUES, "{:?}", r.cues);
        let start = &r.cues[0];
        assert_eq!(start.kind, "start");
        assert!(start.time_s >= 2.3 - bar / 8.0 && start.time_s < 2.3 + bar, "{start:?}");
        let near = |kind: &str, t: f64| r.cues.iter().any(|c| c.kind == kind && (c.time_s - t).abs() <= bar / 2.0);
        assert!(near("drop", 2.3 + 16.0 * bar), "{:?}", r.cues);
        assert!(near("mix_out", 2.3 + 48.0 * bar), "{:?}", r.cues);
        for w in r.cues.windows(2) {
            assert!(w[1].time_s > w[0].time_s);
        }
        for c in &r.cues {
            assert!(r.downbeat_times.contains(&c.time_s), "{c:?} is not on a downbeat");
        }
        let card = crate::analyze(crate::resample::resample(&y, 44_100, SR), Some(4), None);
        assert_eq!(card.cues, r.cues);
        let mut s = String::new();
        card.extras.write(&mut s);
        assert!(s.contains("\"sections.cues\""), "{s}");
    }

    #[test]
    fn json_has_the_fields() {
        let r = analyze_pcm(&click_track(44_100, 124.0, 0.25, 20.0), 44_100, 1, &DjOptions::default()).unwrap();
        let mut s = String::new();
        r.to_json().write(&mut s);
        for k in
            ["\"bpm\"", "\"beat_times\"", "\"downbeat_times\"", "\"8A\"", "\"A minor\"", "\"sections\"", "\"cues\""]
        {
            assert!(s.contains(k), "{k} missing from {s}");
        }
    }
}
