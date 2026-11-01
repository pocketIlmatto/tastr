//! The pipeline stages (tastr/engine/stages/): core, sections, tags, suggestions and views, run in that order
//! over one shared set of signals, each writing its part of the card.

pub mod core;
pub mod sections;
pub mod suggestions;
pub mod tags;
pub mod views;

use crate::card::Card;
use crate::features::{self, Matrix};
use crate::{cqt, rhythm, spectral};

pub const HOP: usize = spectral::HOP;

/// Everything the stages read, computed once (the Python engine's ctx.cache).
pub struct Ctx {
    pub y: Vec<f32>,
    pub sr: f64,
    pub harmonic: Vec<f32>,
    pub percussive: Vec<f32>,
    /// |STFT| of the mix, frames x 1025, by frame.
    pub mag: Vec<f32>,
    pub frames: usize,
    pub onset: Vec<f32>,
    pub rms: Vec<f32>,
    pub mfcc: Matrix,
    pub centroid: Vec<f32>,
    pub mel_perc: Matrix,
    pub mel_perc_freqs: Vec<f64>,
    pub low_flux: Vec<f32>,
    pub air_flux: Vec<f32>,
    pub log_mel: Matrix,
    pub log_mel_freqs: Vec<f64>,
    pub env_mid: Vec<f32>,
    pub env_air: Vec<f32>,
    pub chroma: Matrix,
    pub bass_cqt: Matrix,
    pub tempogram: Option<Vec<f64>>,
    /// The optional drum model's hits, run by the server (engine/adt.py); None for the built-in lanes only.
    pub drum_hits: Option<crate::drums::ModelHits>,
    /// Caller's meter (3 or 4) instead of detecting it.
    pub beats_per_bar: Option<i64>,
}

impl Ctx {
    pub fn new(y: Vec<f32>, beats_per_bar: Option<i64>) -> Ctx {
        let sr = crate::SR as f64;
        let spec = spectral::stft(&y);
        let mag = spectral::magnitude(&spec, 1);
        let (harmonic, percussive) = spectral::hpss_from(&spec, &mag, y.len());
        let frames = spec.frames;
        drop(spec);
        let mut sig = features::all(&y, &mag, frames, &percussive, sr);
        let mut take = |name: &str| {
            let i = sig.iter().position(|(n, _)| *n == name).expect("signal");
            sig.swap_remove(i).1
        };
        let onset = take("onset_envelope").data;
        let rms = take("rms").data;
        let mfcc = take("mfcc");
        let centroid = take("centroid").data;
        let mel_perc = take("mel_percussive");
        let low_flux = take("low_band_flux").data;
        let air_flux = take("air_flux").data;
        let log_mel = take("log_mel");
        let env_mid = take("envelope_mid").data;
        let env_air = take("envelope_air").data;
        let nyq = sr / 2.0;
        let lm = features::mel_frequencies(130, 30.0, 11000.0f64.min(nyq));
        let chroma = cqt::chroma_cqt(&harmonic, sr, HOP);
        let bass_cqt = cqt::bass_cqt(&harmonic, sr, HOP);
        Ctx {
            sr,
            harmonic,
            percussive,
            mag,
            frames,
            onset,
            rms,
            mfcc,
            centroid,
            mel_perc,
            mel_perc_freqs: features::mel_frequencies(96, 0.0, nyq),
            low_flux,
            air_flux,
            log_mel,
            log_mel_freqs: lm[1..lm.len() - 1].to_vec(),
            env_mid,
            env_air,
            chroma,
            bass_cqt,
            tempogram: None,
            drum_hits: None,
            beats_per_bar,
            y,
        }
    }

    /// core's 384-lag tempogram mean, computed on first use.
    pub fn tempogram(&mut self) -> &[f64] {
        if self.tempogram.is_none() {
            let tg = rhythm::tempogram(&self.onset, 384);
            self.tempogram = Some(rhythm::tempogram_mean(&tg, 384, self.onset.len()));
        }
        self.tempogram.as_deref().unwrap()
    }
}

type StageFn = fn(&mut Ctx, &mut Card) -> Result<(), String>;

/// The stages in run order: (name, version, required stages, run).
const STAGES: [(&str, &str, &[&str], StageFn); 5] = [
    ("core", "0.4.0", &[], core::run),
    ("sections", "0.2.0", &["core"], sections::run),
    ("tags", "0.2.0", &["core", "sections"], tags::run),
    ("suggestions", "0.5.0", &["core", "sections", "tags"], suggestions::run),
    ("views", "0.1.0", &[], views::run),
];

/// run_pipeline: every stage over the audio; a failing stage is recorded in card.errors and the stages that
/// need it are skipped.
pub fn run(ctx: &mut Ctx) -> Card {
    let mut card = Card {
        duration: crate::np::round_dec(ctx.y.len() as f64 / ctx.sr, 3),
        sample_rate: ctx.sr as i64,
        extras: crate::json::Json::obj(),
        ..Default::default()
    };
    for (name, version, requires, f) in STAGES {
        let failed: Vec<&str> = requires
            .iter()
            .filter(|d| card.errors.iter().any(|(e, _)| e == *d) || !card.stages_run.iter().any(|(s, _)| s == *d))
            .cloned()
            .collect();
        if !failed.is_empty() {
            let list = failed.iter().map(|d| format!("'{d}'")).collect::<Vec<_>>().join(", ");
            card.errors.push((name.into(), format!("skipped: required stage(s) [{list}] did not complete")));
            continue;
        }
        match f(ctx, &mut card) {
            Ok(()) => card.stages_run.push((name.into(), version.into())),
            Err(e) => card.errors.push((name.into(), e)),
        }
    }
    card
}
