//! Audio bytes in, mono f32 at the file's own rate out (symphonia), averaged over channels like librosa.to_mono.

use std::io::Cursor;

use symphonia::core::audio::SampleBuffer;
use symphonia::core::codecs::{DecoderOptions, CODEC_TYPE_NULL};
use symphonia::core::errors::Error;
use symphonia::core::formats::FormatOptions;
use symphonia::core::io::MediaSourceStream;
use symphonia::core::meta::MetadataOptions;
use symphonia::core::probe::Hint;

pub struct Decoded {
    pub samples: Vec<f32>,
    pub sr: u32,
}

pub fn decode(bytes: Vec<u8>) -> Result<Decoded, String> {
    let mss = MediaSourceStream::new(Box::new(Cursor::new(bytes)), Default::default());
    // Gapless trims the encoder delay and padding MP3/AAC files declare, as libsndfile/mpg123 do.
    let fmt = FormatOptions { enable_gapless: true, ..Default::default() };
    let probed = symphonia::default::get_probe()
        .format(&Hint::new(), mss, &fmt, &MetadataOptions::default())
        .map_err(|e| format!("unrecognised audio: {e}"))?;
    let mut format = probed.format;
    let track = format.tracks().iter().find(|t| t.codec_params.codec != CODEC_TYPE_NULL).ok_or("no audio track")?;
    let track_id = track.id;
    let mut sr = track.codec_params.sample_rate.unwrap_or(0);
    let mut decoder = symphonia::default::get_codecs()
        .make(&track.codec_params, &DecoderOptions::default())
        .map_err(|e| format!("unsupported codec: {e}"))?;

    let mut samples = Vec::new();
    let mut buf: Option<SampleBuffer<f32>> = None;
    loop {
        let packet = match format.next_packet() {
            Ok(p) => p,
            Err(Error::IoError(e)) if e.kind() == std::io::ErrorKind::UnexpectedEof => break,
            Err(Error::ResetRequired) => break,
            Err(e) => return Err(format!("read error: {e}")),
        };
        if packet.track_id() != track_id {
            continue;
        }
        let audio = match decoder.decode(&packet) {
            Ok(a) => a,
            Err(Error::DecodeError(_)) => continue, // a damaged frame: skip it, as mpg123 does
            Err(e) => return Err(format!("decode error: {e}")),
        };
        let spec = *audio.spec();
        sr = spec.rate;
        let channels = spec.channels.count();
        let b = buf.get_or_insert_with(|| SampleBuffer::new(audio.capacity() as u64, spec));
        if b.capacity() < audio.capacity() * channels {
            *b = SampleBuffer::new(audio.capacity() as u64, spec);
        }
        b.copy_interleaved_ref(audio);
        let inv = channels as f32;
        for frame in b.samples().chunks_exact(channels) {
            // np.mean over channels in float32: a left-to-right sum, then one division.
            let mut s = 0.0f32;
            for &v in frame {
                s += v;
            }
            samples.push(if channels == 1 { s } else { s / inv });
        }
    }
    if sr == 0 || samples.is_empty() {
        return Err("no audio decoded".into());
    }
    Ok(Decoded { samples, sr })
}
