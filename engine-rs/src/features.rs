//! The shared signals the stages read (tastr/engine/signals.py and the band envelopes in drums.py), each
//! following the librosa or scipy call it replaces. Arrays are row-major: a `rows x frames` feature stores row 0's
//! frames first.

use crate::spectral::{self, HOP, N_FFT};

/// A rows x cols f32 array.
pub struct Matrix {
    pub rows: usize,
    pub cols: usize,
    pub data: Vec<f32>,
}

impl Matrix {
    pub fn row(&self, r: usize) -> &[f32] {
        &self.data[r * self.cols..(r + 1) * self.cols]
    }
}

// ---- frequencies ----

fn hz_to_mel(f: f64) -> f64 {
    // librosa's default Slaney scale: linear below 1 kHz, logarithmic above.
    let f_sp = 200.0 / 3.0;
    let min_log_hz = 1000.0;
    let min_log_mel = min_log_hz / f_sp;
    let logstep = libm::log(6.4) / 27.0;
    if f >= min_log_hz {
        min_log_mel + libm::log(f / min_log_hz) / logstep
    } else {
        f / f_sp
    }
}

fn mel_to_hz(m: f64) -> f64 {
    let f_sp = 200.0 / 3.0;
    let min_log_hz = 1000.0;
    let min_log_mel = min_log_hz / f_sp;
    let logstep = libm::log(6.4) / 27.0;
    if m >= min_log_mel {
        min_log_hz * libm::exp(logstep * (m - min_log_mel))
    } else {
        f_sp * m
    }
}

/// librosa.mel_frequencies(n, fmin, fmax).
pub fn mel_frequencies(n: usize, fmin: f64, fmax: f64) -> Vec<f64> {
    let (lo, hi) = (hz_to_mel(fmin), hz_to_mel(fmax));
    // np.linspace: start + i * step, with the last point exactly at the end.
    let step = if n > 1 { (hi - lo) / (n - 1) as f64 } else { 0.0 };
    (0..n).map(|i| mel_to_hz(if i + 1 == n && n > 1 { hi } else { lo + i as f64 * step })).collect()
}

pub fn fft_frequencies(sr: f64, n_fft: usize) -> Vec<f64> {
    // np.fft.rfftfreq(n_fft, 1 / sr)
    (0..=n_fft / 2).map(|k| k as f64 * sr / n_fft as f64).collect()
}

/// librosa.filters.mel(sr, n_fft, n_mels, fmin, fmax): Slaney-normalised triangles, as float32.
pub fn mel_basis(sr: f64, n_fft: usize, n_mels: usize, fmin: f64, fmax: f64) -> Matrix {
    let fft = fft_frequencies(sr, n_fft);
    let mel_f = mel_frequencies(n_mels + 2, fmin, fmax);
    let bins = fft.len();
    let mut data = vec![0.0f32; n_mels * bins];
    for i in 0..n_mels {
        let (d0, d1) = (mel_f[i + 1] - mel_f[i], mel_f[i + 2] - mel_f[i + 1]);
        let enorm = 2.0 / (mel_f[i + 2] - mel_f[i]);
        for (k, &f) in fft.iter().enumerate() {
            let lower = -(mel_f[i] - f) / d0;
            let upper = (mel_f[i + 2] - f) / d1;
            let w = 0.0f64.max(lower.min(upper)) as f32; // stored as float32, then scaled in place
            data[i * bins + k] = (w as f64 * enorm) as f32;
        }
    }
    Matrix { rows: n_mels, cols: bins, data }
}

/// A filter bank, its frame count, and each row's nonzero bin range.
type Basis = (Matrix, usize, Vec<(usize, usize)>);

/// Mel (or any) bases applied to a power spectrogram that arrives one frame at a time.
pub struct MelBank {
    bases: Vec<Basis>,
    out: Vec<Vec<f32>>,
}

impl MelBank {
    pub fn new(bases: Vec<Matrix>, frames: usize) -> Self {
        let out = bases.iter().map(|m| vec![0.0f32; m.rows * frames]).collect();
        let bases = bases
            .into_iter()
            .map(|m| {
                // Only the bins a triangle covers; the rest are zero.
                let spans = (0..m.rows)
                    .map(|r| {
                        let w = m.row(r);
                        match (w.iter().position(|&v| v != 0.0), w.iter().rposition(|&v| v != 0.0)) {
                            (Some(a), Some(b)) => (a, b + 1),
                            _ => (0, 0),
                        }
                    })
                    .collect();
                (m, frames, spans)
            })
            .collect();
        MelBank { bases, out }
    }

    /// Frame `t`'s power row.
    pub fn push(&mut self, t: usize, power: &[f32]) {
        for ((m, frames, spans), out) in self.bases.iter().zip(self.out.iter_mut()) {
            for (r, &(a, b)) in spans.iter().enumerate() {
                let w = m.row(r);
                let mut acc = 0.0f64;
                for k in a..b {
                    acc += w[k] as f64 * power[k] as f64;
                }
                out[r * frames + t] = acc as f32;
            }
        }
    }

    pub fn finish(self) -> Vec<Matrix> {
        self.bases
            .into_iter()
            .zip(self.out)
            .map(|((m, frames, _), data)| Matrix { rows: m.rows, cols: frames, data })
            .collect()
    }
}

/// librosa.feature.melspectrogram for several filter banks at once, from a frames x bins magnitude spectrogram
/// (squared here, as power).
pub fn mels_from_magnitude(mag: &[f32], frames: usize, bases: Vec<Matrix>) -> Vec<Matrix> {
    let bins = bases[0].cols;
    let mut bank = MelBank::new(bases, frames);
    let mut power = vec![0.0f32; bins];
    for t in 0..frames {
        for (p, &m) in power.iter_mut().zip(&mag[t * bins..(t + 1) * bins]) {
            *p = m * m;
        }
        bank.push(t, &power);
    }
    bank.finish()
}

/// The same from audio, one STFT frame at a time (n_fft 2048, hop 512), never holding its spectrogram.
pub fn mels_from_audio(y: &[f32], bases: Vec<Matrix>) -> Vec<Matrix> {
    let frames = 1 + y.len() / HOP;
    let mut bank = MelBank::new(bases, frames);
    let mut power = vec![0.0f32; N_FFT / 2 + 1];
    spectral::stft_frames(y, N_FFT, HOP, |t, col| {
        for (p, c) in power.iter_mut().zip(col) {
            let m = libm::hypotf(c.re, c.im);
            *p = m * m;
        }
        bank.push(t, &power);
    });
    bank.finish()
}

// ---- level ----

/// librosa.power_to_db(x, ref, amin=1e-10, top_db), in float32.
pub fn power_to_db(x: &[f32], reference: f64, top_db: Option<f64>) -> Vec<f32> {
    let amin = 1e-10f32;
    // numpy takes the reference as a float64 scalar, so the subtraction runs in float64.
    let r = 10.0 * libm::log10(1e-10f64.max(reference.abs()));
    let mut out: Vec<f32> = x.iter().map(|&v| ((10.0f32 * libm::log10f(amin.max(v))) as f64 - r) as f32).collect();
    if let Some(top) = top_db {
        let floor = out.iter().cloned().fold(f32::NEG_INFINITY, f32::max) - top as f32;
        for v in out.iter_mut() {
            *v = v.max(floor);
        }
    }
    out
}

/// np.percentile(x, q) with linear interpolation.
pub fn percentile(x: &[f32], q: f64) -> f64 {
    let mut v: Vec<f32> = x.to_vec();
    v.sort_by(|a, b| a.total_cmp(b));
    let pos = q / 100.0 * (v.len() - 1) as f64;
    let (i, frac) = (pos.floor() as usize, pos - pos.floor());
    let lo = v[i] as f64;
    if i + 1 >= v.len() {
        lo
    } else {
        lo + (v[i + 1] as f64 - lo) * frac
    }
}

/// librosa.feature.rms(y=y): frames of 2048 every 512, centred with zero padding.
pub fn rms(y: &[f32]) -> Vec<f32> {
    let frames = 1 + y.len() / HOP;
    let pad = (N_FFT / 2) as isize;
    (0..frames)
        .map(|f| {
            let start = (f * HOP) as isize - pad;
            let mut acc = 0.0f64;
            for k in 0..N_FFT as isize {
                let i = start + k;
                if i >= 0 && (i as usize) < y.len() {
                    let v = y[i as usize];
                    acc += (v * v) as f64;
                }
            }
            ((acc / N_FFT as f64) as f32).sqrt()
        })
        .collect()
}

/// librosa.feature.spectral_centroid(S=|D|, sr): per frame, the magnitude-weighted mean frequency.
pub fn spectral_centroid(mag: &[f32], frames: usize, bins: usize, sr: f64) -> Vec<f32> {
    let freq = fft_frequencies(sr, (bins - 1) * 2);
    (0..frames)
        .map(|t| {
            let row = &mag[t * bins..(t + 1) * bins];
            let total: f64 = row.iter().map(|&v| v as f64).sum();
            // util.normalize leaves a column alone when its sum is below the smallest normal float.
            let norm = if (total as f32) < f32::MIN_POSITIVE { 1.0 } else { total };
            row.iter().zip(&freq).map(|(&v, &f)| f * (v as f64 / norm)).sum::<f64>() as f32
        })
        .collect()
}

/// scipy.fft.dct(x, type=2, norm="ortho") down each column of a rows x cols matrix, first `keep` rows kept.
pub fn dct_ortho(x: &Matrix, keep: usize) -> Matrix {
    let n = x.rows;
    let mut data = vec![0.0f32; keep * x.cols];
    for k in 0..keep {
        let scale = if k == 0 { libm::sqrt(1.0 / (4.0 * n as f64)) } else { libm::sqrt(1.0 / (2.0 * n as f64)) };
        let cos: Vec<f64> =
            (0..n).map(|i| libm::cos(core::f64::consts::PI * k as f64 * (2 * i + 1) as f64 / (2 * n) as f64)).collect();
        for t in 0..x.cols {
            let mut acc = 0.0f64;
            for (i, c) in cos.iter().enumerate() {
                acc += x.data[i * x.cols + t] as f64 * c;
            }
            data[k * x.cols + t] = (2.0 * acc * scale) as f32;
        }
    }
    Matrix { rows: keep, cols: x.cols, data }
}

// ---- the signals in signals.py ----

/// signals.mfcc: 13 MFCCs of the full mix, from its 128-band mel power spectrogram.
pub fn mfcc(mel: &Matrix) -> Matrix {
    let shifted: Vec<f32> = mel.data.iter().map(|v| v + 1e-10).collect();
    let db = power_to_db(&shifted, 1.0, Some(80.0));
    dct_ortho(&Matrix { rows: mel.rows, cols: mel.cols, data: db }, 13)
}

/// librosa.onset.onset_strength(y=y_perc), from its 128-band mel power spectrogram: mean positive rise in dB
/// across the bands, with two frames' framing delay plus one lag frame put back at the start.
pub fn onset_strength(mel: &Matrix) -> Vec<f32> {
    let frames = mel.cols;
    let db = power_to_db(&mel.data, 1.0, Some(80.0));
    let pad = 1 + N_FFT / (2 * HOP);
    let mut out = vec![0.0f32; frames];
    for (t, o) in out.iter_mut().enumerate().skip(pad) {
        let (a, b) = (t - pad, t - pad + 1);
        if b >= frames {
            break;
        }
        let mut acc = 0.0f64;
        for m in 0..mel.rows {
            acc += 0.0f32.max(db[m * frames + b] - db[m * frames + a]) as f64;
        }
        *o = (acc / mel.rows as f64) as f32;
    }
    out
}

/// Mean positive rise per frame of a set of dB rows (each row's first frame repeated before it).
fn mean_rise(db: &[f32], rows: usize, frames: usize) -> Vec<f32> {
    (0..frames)
        .map(|t| {
            let mut acc = 0.0f64;
            for r in 0..rows {
                let prev = db[r * frames + t.saturating_sub(1)];
                acc += 0.0f32.max(db[r * frames + t] - prev) as f64;
            }
            (acc / rows as f64) as f32
        })
        .collect()
}

fn select_rows(m: &Matrix, keep: impl Fn(usize) -> bool) -> (Vec<f32>, usize) {
    let mut out = Vec::new();
    let mut n = 0;
    for r in 0..m.rows {
        if keep(r) {
            out.extend_from_slice(m.row(r));
            n += 1;
        }
    }
    (out, n)
}

/// signals.low_band_flux: the kick band's (20-150 Hz) mean dB rise.
pub fn low_band_flux(mel: &Matrix, freqs: &[f64]) -> Vec<f32> {
    let (mut rows, n) = select_rows(mel, |r| freqs[r] >= 20.0 && freqs[r] < 150.0);
    if n == 0 {
        return vec![0.0; mel.cols];
    }
    for v in rows.iter_mut() {
        *v += 1e-10;
    }
    mean_rise(&power_to_db(&rows, 1.0, Some(80.0)), n, mel.cols)
}

/// drums._air_flux: the band above 6 kHz's mean dB rise, floored 50 dB under its loudest moments.
pub fn air_flux(mel: &Matrix, freqs: &[f64]) -> Vec<f32> {
    let (rows, n) = select_rows(mel, |r| freqs[r] >= 6000.0);
    if n == 0 {
        return vec![0.0; mel.cols];
    }
    let reference = percentile(&rows, 99.9);
    let shifted: Vec<f32> = rows.iter().map(|&v| v + 1e-10).collect();
    mean_rise(&power_to_db(&shifted, reference, Some(50.0)), n, mel.cols)
}

/// signals.log_mel: log1p(1000 * magnitude) of 128 mel bands, 30 Hz-11 kHz (from their power).
pub fn log_mel(mut mel: Matrix) -> Matrix {
    for v in mel.data.iter_mut() {
        *v = libm::log1pf(1000.0f32 * v.sqrt());
    }
    mel
}

// ---- drum band envelopes (drums._envelopes) ----

/// scipy.signal.butter(4, ..., fs=22050, output="sos") for the two bands, and sosfilt_zi of each. These depend
/// only on the sample rate, which is fixed, so they are scipy's own numbers; tests/test_rust_engine.py checks
/// they still match scipy's.
const MID_SOS: [[f64; 6]; 4] = [
    [0.004787197487092548, 0.009574394974185095, 0.004787197487092548, 1.0, -1.1623757864847162, 0.44518105760518106],
    [1.0, 2.0, 1.0, 1.0, -1.585670185413183, 0.6672250538005194],
    [1.0, -2.0, 1.0, 1.0, -1.1483853967012556, 0.7059711172957007],
    [1.0, -2.0, 1.0, 1.0, -1.8450116993133265, 0.8971008100553315],
];
const MID_ZI: [[f64; 2]; 4] = [
    [0.06292296177727806, -0.025356082824835213],
    [3.2532520642432754, -2.148119038985011],
    [-3.3209622235076464, 3.3209622235076464],
    [-0.0, 0.0],
];
const AIR_SOS: [[f64; 6]; 2] = [
    [0.07026769365421941, -0.14053538730843881, 0.07026769365421941, 1.0, 0.14461522961984596, 0.04439745846700652],
    [1.0, -2.0, 1.0, 1.0, 0.2008236659270588, 0.4503294490185849],
];
const AIR_ZI: [[f64; 2]; 2] = [[-0.07026769365421941, 0.07026769365421941], [-0.0, 0.0]];

type SosTable = (&'static str, &'static [[f64; 6]], &'static [[f64; 2]]);

pub fn sos_tables() -> [SosTable; 2] {
    [("mid", &MID_SOS, &MID_ZI), ("air", &AIR_SOS, &AIR_ZI)]
}

/// scipy.signal.sosfilt with initial state zi * x0 (transposed direct form II, section by section).
fn sosfilt(sos: &[[f64; 6]], zi: &[[f64; 2]], x0: f64, x: &mut [f64]) {
    let mut z: Vec<[f64; 2]> = zi.iter().map(|s| [s[0] * x0, s[1] * x0]).collect();
    for v in x.iter_mut() {
        let mut s = *v;
        for (c, zz) in sos.iter().zip(z.iter_mut()) {
            let xn = s;
            s = c[0] * xn + zz[0];
            zz[0] = c[1] * xn - c[4] * s + zz[1];
            zz[1] = c[2] * xn - c[5] * s;
        }
        *v = s;
    }
}

/// scipy.signal.sosfiltfilt(sos, x) with its defaults: odd extension of 3 * (2 * sections + 1) samples.
pub fn sosfiltfilt(sos: &[[f64; 6]], zi: &[[f64; 2]], x: &[f32]) -> Vec<f64> {
    let n = x.len();
    let edge = 3 * (2 * sos.len() + 1);
    if n <= edge {
        return x.iter().map(|&v| v as f64).collect(); // scipy refuses; the engine never sends audio this short
    }
    let xf = |i: usize| x[i] as f64;
    let mut ext = Vec::with_capacity(n + 2 * edge);
    for k in (1..=edge).rev() {
        ext.push(2.0 * xf(0) - xf(k));
    }
    ext.extend(x.iter().map(|&v| v as f64));
    for k in 1..=edge {
        ext.push(2.0 * xf(n - 1) - xf(n - 1 - k));
    }
    let x0 = ext[0];
    sosfilt(sos, zi, x0, &mut ext);
    ext.reverse();
    let y0 = ext[0];
    sosfilt(sos, zi, y0, &mut ext);
    ext.reverse();
    ext[edge..edge + n].to_vec()
}

/// drums._envelopes: |band-passed percussive part|, averaged over 4 ms (np.convolve "same"), in dB.
pub fn band_envelope(sos: &[[f64; 6]], zi: &[[f64; 2]], y_perc: &[f32], sr: f64) -> Vec<f32> {
    let x: Vec<f64> = sosfiltfilt(sos, zi, y_perc).into_iter().map(f64::abs).collect();
    let win = ((0.004 * sr) as usize).max(1);
    let n = x.len();
    // np.convolve(x, ones(win) / win, "same"): output i averages x[i - win/2 .. i + (win-1)/2 + ... ] as below.
    let back = win / 2; // samples before i
    let ahead = win - 1 - back; // samples after i
    let h = 1.0 / win as f64;
    (0..n)
        .map(|i| {
            let a = i.saturating_sub(back);
            let b = (i + ahead + 1).min(n);
            let mut acc = 0.0f64;
            for v in &x[a..b] {
                acc += v * h;
            }
            (20.0 * libm::log10(acc + 1e-7)) as f32
        })
        .collect()
}

/// Everything above, by the names signals.py uses, from the audio, its default STFT magnitude and the
/// percussive part.
pub fn all(y: &[f32], mag: &[f32], frames: usize, perc: &[f32], sr: f64) -> Vec<(&'static str, Matrix)> {
    let row = |data: Vec<f32>| Matrix { rows: 1, cols: data.len(), data };
    let nyq = sr / 2.0;
    let mut mix = mels_from_magnitude(
        mag,
        frames,
        vec![mel_basis(sr, N_FFT, 128, 0.0, nyq), mel_basis(sr, N_FFT, 128, 30.0, 11000.0f64.min(nyq))],
    );
    let lmel = log_mel(mix.pop().unwrap());
    let mfccs = mfcc(&mix.pop().unwrap());
    let mut drums =
        mels_from_audio(perc, vec![mel_basis(sr, N_FFT, 128, 0.0, nyq), mel_basis(sr, N_FFT, 96, 0.0, nyq)]);
    let mel_p = drums.pop().unwrap();
    let onsets = onset_strength(&drums.pop().unwrap());
    let mel_p_freqs = mel_frequencies(96, 0.0, nyq);
    let low = low_band_flux(&mel_p, &mel_p_freqs);
    let air = air_flux(&mel_p, &mel_p_freqs);
    let mut out = vec![
        ("onset_envelope", row(onsets)),
        ("rms", row(rms(y))),
        ("mfcc", mfccs),
        ("centroid", row(spectral_centroid(mag, frames, N_FFT / 2 + 1, sr))),
        ("mel_percussive", mel_p),
        ("low_band_flux", row(low)),
        ("air_flux", row(air)),
        ("log_mel", lmel),
    ];
    for (name, sos, zi) in sos_tables() {
        let env = band_envelope(sos, zi, perc, sr);
        out.push((if name == "mid" { "envelope_mid" } else { "envelope_air" }, row(env)));
    }
    out
}
