//! Entry points for the benchmarks (tastr/engine/bench/): single engine steps on inputs that aren't a whole
//! audio file, so the benches score the Rust code the app ships. Datasets that only publish spectrograms (Beat
//! This!, Harmonix) give log-mel or mel frames instead of audio, and the key and drum benches want the engine's
//! intermediate features rather than a card.
//!
//! Each call is one text line, `<op> key=value ...`, plus one buffer of float64 values holding that op's
//! arrays one after another (matrices row-major); the answer is JSON. tastr/engine/rust.py wraps them.
//!
//! - `downbeat_phase bands= frames= beats= m=`: log-mel (bands x frames), beat frames -> {phase}
//! - `meter bands= frames= beats=`: the same -> {score, guess, threshold, phase3}
//! - `tempo n= bands= frames= fps= perc=0|1`: onset envelope and low-band flux (n frames on core's grid), log-mel
//!   at `fps`, and with perc=1 the percussive part's mel power (same shape) -> {bpm, confidence, stability,
//!   pulse, backbeat (the drum & bass check's balance and contrast, null when it didn't run), beats}
//! - `tempo_audio n=`: mono audio at 22,050 Hz -> {bpm, confidence, backbeat, beats}, as the card has them
//! - `sections frames= sr= hop= duration= downbeats= configs=s:p:c,...`: chroma (12 x frames), MFCC (13 x
//!   frames), RMS (frames), downbeat times -> {current (the stage's own seconds, phrase, cap),
//!   bar_times, starts: [[...] per config], empty below MIN_BARS_FOR_SPLIT bars}
//! - `key n= matrices=0|1`: mono audio at 22,050 Hz -> {profile, silent} (+ chroma, bass_cqt)
//! - `key_profile frames=`: chroma (12 x frames) -> {profile}
//! - `lane_activations n= hits= [open_hats=built-in|model]`: audio, drum model hits (lib::parse_drum_hits
//!   layout, `hits` values) -> {lanes, acts: [[per frame] per lane], hop}. `open_hats` overrides
//!   drums::MODEL_OPEN_HATS (where the open-hat lane comes from when there are model hits).
//! - `drum_grid n= beats= downbeats= bpm= beats_per_bar= hits= [open_hats=]`: audio, beat times, downbeat
//!   times, hits -> {lane: {pattern, velocity, detected}}

use crate::card::Card;
use crate::features::{mel_frequencies, Matrix};
use crate::json::Json;
use crate::stages::{core, sections, suggestions, Ctx, HOP};
use crate::{cqt, music, spectral, SR};

/// The bands of a 128-band log-mel spectrogram (Beat This!'s and the engine's own: 30 Hz to 11 kHz).
fn log_mel_freqs(bands: usize) -> Vec<f64> {
    let f = mel_frequencies(bands + 2, 30.0, 11000.0);
    f[1..f.len() - 1].to_vec()
}

struct Args<'a> {
    params: Vec<(&'a str, &'a str)>,
    data: &'a [f64],
    at: usize,
}

impl<'a> Args<'a> {
    fn get(&self, key: &str) -> Result<&'a str, String> {
        self.params.iter().find(|(k, _)| *k == key).map(|(_, v)| *v).ok_or(format!("missing {key}="))
    }

    fn num(&self, key: &str) -> Result<f64, String> {
        self.get(key)?.parse::<f64>().map_err(|e| format!("{key}: {e}"))
    }

    fn count(&self, key: &str) -> Result<usize, String> {
        Ok(self.num(key)? as usize)
    }

    fn opt_count(&self, key: &str) -> usize {
        self.count(key).unwrap_or(0)
    }

    fn take(&mut self, n: usize) -> Result<&'a [f64], String> {
        let end = self.at + n;
        if end > self.data.len() {
            return Err(format!("data cut short: want {end} values, have {}", self.data.len()));
        }
        let out = &self.data[self.at..end];
        self.at = end;
        Ok(out)
    }

    fn take_f32(&mut self, n: usize) -> Result<Vec<f32>, String> {
        Ok(self.take(n)?.iter().map(|&v| v as f32).collect())
    }

    fn matrix(&mut self, rows: usize, cols: usize) -> Result<Matrix, String> {
        Ok(Matrix { rows, cols, data: self.take_f32(rows * cols)? })
    }

    fn frames(&mut self, n: usize) -> Result<Vec<usize>, String> {
        Ok(self.take(n)?.iter().map(|&v| v.max(0.0) as usize).collect())
    }
}

/// Runs one op; JSON out, or an error message.
pub fn call(line: &str, data: &[f64]) -> Result<Json, String> {
    let mut words = line.split_whitespace();
    let op = words.next().ok_or("empty call")?;
    let params = words.filter_map(|w| w.split_once('=')).collect();
    let mut a = Args { params, data, at: 0 };
    match op {
        "downbeat_phase" => downbeat_phase(&mut a),
        "meter" => meter(&mut a),
        "tempo" => tempo(&mut a),
        "tempo_audio" => tempo_audio(&mut a),
        "sections" => sections_op(&mut a),
        "key" => key(&mut a),
        "key_profile" => {
            let chroma = a.matrix(12, a.count("frames")?)?;
            Ok(Json::obj().with("profile", music::key_profile(&chroma).to_vec()))
        }
        "lane_activations" => lane_activations(&mut a),
        "drum_grid" => drum_grid(&mut a),
        _ => Err(format!("unknown op {op}")),
    }
}

fn spectrogram_and_beats(a: &mut Args) -> Result<(Matrix, Vec<f64>, Vec<usize>), String> {
    let (bands, frames) = (a.count("bands")?, a.count("frames")?);
    let log_mel = a.matrix(bands, frames)?;
    let beats = a.frames(a.count("beats")?)?;
    Ok((log_mel, log_mel_freqs(bands), beats))
}

fn downbeat_phase(a: &mut Args) -> Result<Json, String> {
    let (log_mel, freqs, beats) = spectrogram_and_beats(a)?;
    let m = a.count("m").unwrap_or(4);
    Ok(Json::obj().with("phase", core::downbeat_phase(&beats, &log_mel, &freqs, m, None)))
}

/// The meter score and, as if the meter were known to be 3/4, the phase of bar 1 (core reads both from one set
/// of beat cues).
fn meter(a: &mut Args) -> Result<Json, String> {
    let (log_mel, freqs, beats) = spectrogram_and_beats(a)?;
    let cues = (beats.len() >= 2 && log_mel.cols >= 2).then(|| core::beat_cues(&beats, &log_mel, &freqs));
    let score = core::meter_score(&beats, &log_mel, &freqs, cues.as_ref());
    let phase3 = core::downbeat_phase(&beats, &log_mel, &freqs, 3, cues.as_ref());
    Ok(Json::obj()
        .with("score", score)
        .with("guess", if score > core::METER_THRESHOLD { 3 } else { 4 })
        .with("threshold", core::METER_THRESHOLD)
        .with("phase3", phase3))
}

fn tempo(a: &mut Args) -> Result<Json, String> {
    let n = a.count("n")?;
    let onset = a.take_f32(n)?;
    let low_flux = a.take_f32(n)?;
    let (bands, frames, fps) = (a.count("bands")?, a.count("frames")?, a.num("fps")?);
    let log_mel = a.matrix(bands, frames)?;
    let freqs = log_mel_freqs(bands);
    // the percussive part's mel power, for the drum & bass check (without it the check never fires)
    let perc = if a.opt_count("perc") == 1 { Some(a.matrix(bands, frames)?) } else { None };
    let mid = perc.map(|m| core::mid_drum_hits(&m, &freqs, &crate::features::air_flux(&m, &freqs)));
    let sr = SR as f64;
    // core's beat frames onto the spectrogram's own frame rate
    let at_fps = |f: &[f64]| -> Vec<f64> { f.iter().map(|&x| x * HOP as f64 / sr * fps).collect() };
    let index = |f: &[f64]| -> Vec<usize> {
        at_fps(f)
            .iter()
            .map(|&x| crate::np::clip(crate::np::round_half_even(x), 0.0, frames as f64 - 1.0) as usize)
            .collect()
    };
    let meter_of = |f: &[f64]| core::meter_score(&index(f), &log_mel, &freqs, None);
    let seen = std::cell::Cell::new(None);
    let backbeat_of = |f: &[f64]| {
        let v = mid.as_ref().map_or((0.0, 0.0), |m| core::fast_backbeat(&at_fps(f), m));
        seen.set(Some(v));
        v
    };
    let (bpm, beats, conf, stability, pulse) =
        core::tempo_from_envelopes(&onset, &low_flux, sr, n, &mut None, &meter_of, &backbeat_of);
    Ok(Json::obj()
        .with("bpm", bpm)
        .with("confidence", conf)
        .with("stability", stability)
        .with("pulse", pulse)
        .with("backbeat", backbeat_json(seen.get()))
        .with("beats", beats))
}

fn backbeat_json(v: Option<(f64, f64)>) -> Json {
    v.map_or(Json::Null, |(balance, contrast)| Json::from(vec![balance, contrast]))
}

/// Core's tempo for audio, as a card has it, with the drum & bass check's (balance, contrast) when it ran.
fn tempo_audio(a: &mut Args) -> Result<Json, String> {
    let ctx = audio_ctx(a)?;
    let n_frames = ctx.onset.len();
    let meter_of =
        |f: &[f64]| core::meter_score(&core::beat_index(f, n_frames), &ctx.log_mel, &ctx.log_mel_freqs, None);
    let seen = std::cell::Cell::new(None);
    let backbeat_of = |f: &[f64]| {
        let v = core::fast_backbeat(f, &core::mid_drum_hits(&ctx.mel_perc, &ctx.mel_perc_freqs, &ctx.air_flux));
        seen.set(Some(v));
        v
    };
    let (bpm, beats, conf, _, _) =
        core::tempo_from_envelopes(&ctx.onset, &ctx.low_flux, ctx.sr, n_frames, &mut None, &meter_of, &backbeat_of);
    Ok(Json::obj()
        .with("bpm", bpm)
        .with("confidence", conf)
        .with("backbeat", backbeat_json(seen.get()))
        .with("beats", beats))
}

fn sections_op(a: &mut Args) -> Result<Json, String> {
    let frames = a.count("frames")?;
    let chroma = a.matrix(12, frames)?;
    let mfcc = a.matrix(13, frames)?;
    let rms = a.take_f32(frames)?;
    let downbeats = a.take(a.count("downbeats")?)?.to_vec();
    let (sr, hop, duration) = (a.num("sr")?, a.count("hop")?, a.num("duration")?);
    let bar_times = sections::bar_times(&downbeats, duration);
    let n_bars = bar_times.len();
    let d = sections::SETTINGS;
    let mut out =
        Json::obj().with("current", vec![d.seconds, d.phrase as f64, d.cap]).with("bar_times", bar_times.clone());
    if n_bars < sections::MIN_BARS_FOR_SPLIT {
        return Ok(out.with("starts", Json::Arr(Vec::new())));
    }
    let z = sections::bar_features(&chroma, &mfcc, &rms, sr, hop, &bar_times, duration);
    let mut starts = Vec::new();
    for c in a.get("configs").unwrap_or("").split(',').filter(|c| !c.is_empty()) {
        let p: Vec<f64> =
            c.split(':').map(|v| v.parse::<f64>().map_err(|e| format!("configs: {e}"))).collect::<Result<_, _>>()?;
        if p.len() != 3 {
            return Err(format!("configs: want seconds:phrase:cap, got {c}"));
        }
        let s = sections::Settings { seconds: p[0], phrase: p[1] as usize, cap: p[2] };
        starts.push(Json::from(sections::segment_with(&z, duration, n_bars, s)));
    }
    if starts.is_empty() {
        starts.push(Json::from(sections::segment_with(&z, duration, n_bars, sections::SETTINGS)));
    }
    out.set("starts", Json::Arr(starts));
    Ok(out)
}

fn matrix_json(m: &Matrix) -> Json {
    Json::Arr((0..m.rows).map(|r| Json::from(m.row(r).iter().map(|&v| v as f64).collect::<Vec<f64>>())).collect())
}

/// What the key model reads: the 12-bin key profile from the harmonic part's chroma (and, with matrices=1, the
/// chroma and bass CQT themselves, for the feature cache).
fn key(a: &mut Args) -> Result<Json, String> {
    let y = a.take_f32(a.count("n")?)?;
    let silent = y.is_empty() || !y.iter().any(|v| v.abs() > 1e-4);
    let (harmonic, _) = spectral::hpss(&y);
    let sr = SR as f64;
    let chroma = cqt::chroma_cqt(&harmonic, sr, HOP);
    let profile = music::key_profile(&chroma);
    let mut out = Json::obj().with("profile", profile.to_vec()).with("silent", silent);
    if a.opt_count("matrices") == 1 {
        out.set("chroma", matrix_json(&chroma));
        out.set("bass_cqt", matrix_json(&cqt::bass_cqt(&harmonic, sr, HOP)));
    }
    Ok(out)
}

fn audio_ctx(a: &mut Args) -> Result<Ctx, String> {
    let y = a.take_f32(a.count("n")?)?;
    Ok(Ctx::new(y, None))
}

fn read_hits(a: &mut Args, ctx: &mut Ctx) -> Result<(), String> {
    let n = a.opt_count("hits");
    ctx.drum_hits = crate::parse_drum_hits(a.take(n)?)?;
    match a.get("open_hats").unwrap_or("default") {
        "default" => {}
        "built-in" => ctx.model_open_hats = false,
        "model" => ctx.model_open_hats = true,
        v => return Err(format!("open_hats: {v} (built-in or model)")),
    }
    Ok(())
}

/// Every lane's strength at every analysis frame (no slop), as the drum benches score onsets on.
fn lane_activations(a: &mut Args) -> Result<Json, String> {
    let mut ctx = audio_ctx(a)?;
    read_hits(a, &mut ctx)?;
    let mut drums = crate::drums::Drums::new(&ctx.mel_perc, &ctx.mel_perc_freqs, &ctx.air_flux);
    drums.model_open_hats = ctx.model_open_hats;
    if let Some(hits) = &ctx.drum_hits {
        drums.set_model(hits, ctx.sr, HOP);
    }
    let frames: Vec<i64> = (0..=(ctx.y.len() / HOP) as i64).collect();
    let acts = drums.lane_activations(&frames, &ctx.env_mid, &ctx.env_air, ctx.sr, HOP, None, 0)?;
    Ok(Json::obj()
        .with("lanes", drums.lanes().iter().map(|l| l.to_string()).collect::<Vec<String>>())
        .with("acts", acts)
        .with("hop", HOP))
}

/// The drum grid for audio whose beats are known (the synthetic loops).
fn drum_grid(a: &mut Args) -> Result<Json, String> {
    let mut ctx = audio_ctx(a)?;
    let beats = a.take(a.count("beats")?)?.to_vec();
    let downbeats = a.take(a.count("downbeats")?)?.to_vec();
    read_hits(a, &mut ctx)?;
    let mut card = Card { duration: ctx.y.len() as f64 / ctx.sr, sample_rate: SR as i64, ..Default::default() };
    card.core.bpm = a.num("bpm").ok();
    card.core.beats_per_bar = a.num("beats_per_bar").map(|v| v as i64).unwrap_or(4);
    card.core.beat_times = beats;
    card.core.downbeat_times = downbeats;
    suggestions::drum_grid_json(&ctx, &card)
}
