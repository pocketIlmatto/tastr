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
    // symphonia 0.5 refuses a whole file whose ID3v2 tag has a compressed frame ("id3v2: compressed frames are
    // not supported"; some FMA MP3s). Tags carry no audio, so then try again past them; files it reads
    // already decode exactly as before.
    let id3 = id3v2_len(&bytes);
    let retry = (id3 > 0).then(|| bytes[id3..].to_vec());
    match decode_from(bytes) {
        Err(e) if e.contains("id3v2") && retry.is_some() => decode_from(retry.unwrap_or_default()),
        r => r,
    }
}

/// The length of the ID3v2 tags at the start of `b` (several can follow each other), 0 if none.
fn id3v2_len(b: &[u8]) -> usize {
    let mut at = 0;
    while b.len() >= at + 10 && &b[at..at + 3] == b"ID3" && b[at + 6..at + 10].iter().all(|&x| x < 0x80) {
        let size = b[at + 6..at + 10].iter().fold(0usize, |n, &x| (n << 7) | x as usize);
        let footer = if b[at + 5] & 0x10 != 0 { 10 } else { 0 };
        at += 10 + size + footer;
    }
    at.min(b.len())
}

fn decode_from(bytes: Vec<u8>) -> Result<Decoded, String> {
    let edit = mp4_edit(&bytes);
    let mss = MediaSourceStream::new(Box::new(Cursor::new(bytes)), Default::default());
    // Gapless trims the encoder delay and padding MP3/AAC files declare, as libsndfile/mpg123 do.
    let fmt = FormatOptions { enable_gapless: true, ..Default::default() };
    let probed = symphonia::default::get_probe()
        .format(&Hint::new(), mss, &fmt, &MetadataOptions::default())
        .map_err(|e| format!("unrecognised audio: {e}"))?;
    let mut format = probed.format;
    let track = format.tracks().iter().find(|t| t.codec_params.codec != CODEC_TYPE_NULL).ok_or("no audio track")?;
    let track_id = track.id;
    let sr = track.codec_params.sample_rate.unwrap_or(0);
    let mut decoder = symphonia::default::get_codecs()
        .make(&track.codec_params, &DecoderOptions::default())
        .map_err(|e| format!("unsupported codec: {e}"))?;
    let (mut samples, sr) = decode_mono(format.as_mut(), decoder.as_mut(), track_id, sr)?;
    if sr == 0 || samples.is_empty() {
        return Err("no audio decoded".into());
    }
    if let Some(e) = edit {
        samples = e.apply(samples, sr);
    }
    Ok(Decoded { samples, sr })
}

/// Every packet of the track, decoded and mixed to mono: (samples, sample rate, which starts as `sr`).
fn decode_mono(
    format: &mut dyn symphonia::core::formats::FormatReader,
    decoder: &mut dyn symphonia::core::codecs::Decoder,
    track_id: u32,
    mut sr: u32,
) -> Result<(Vec<f32>, u32), String> {
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
        push_mono(&mut samples, b.samples(), channels);
    }
    Ok((samples, sr))
}

/// Append interleaved frames as mono: np.mean over channels in float32, a left-to-right sum, then one division.
fn push_mono(out: &mut Vec<f32>, interleaved: &[f32], channels: usize) {
    let inv = channels as f32;
    for frame in interleaved.chunks_exact(channels) {
        let mut s = 0.0f32;
        for &v in frame {
            s += v;
        }
        out.push(if channels == 1 { s } else { s / inv });
    }
}

/// The first edit of an MP4's sound track: skip `skip` units of `media_timescale`, then play `seconds`.
struct Mp4Edit {
    skip: u64,
    media_timescale: u64,
    seconds: f64,
}

impl Mp4Edit {
    /// The samples the edit plays. symphonia 0.5 ignores MP4 edit lists, which is where AAC files say how much
    /// priming to drop.
    fn apply(&self, samples: Vec<f32>, sr: u32) -> Vec<f32> {
        let skip = ((self.skip as f64) * sr as f64 / self.media_timescale as f64).round() as usize;
        let keep = (self.seconds * sr as f64).round() as usize;
        let end = (skip + keep).min(samples.len());
        samples[skip.min(end)..end].to_vec()
    }
}

fn be32(b: &[u8], i: usize) -> Option<u64> {
    b.get(i..i + 4).map(|s| u32::from_be_bytes([s[0], s[1], s[2], s[3]]) as u64)
}

fn be64(b: &[u8], i: usize) -> Option<u64> {
    b.get(i..i + 8).map(|s| u64::from_be_bytes(s.try_into().unwrap()))
}

/// The boxes directly inside `b`: (type, contents).
fn boxes(b: &[u8]) -> Vec<(&[u8], &[u8])> {
    let mut out = Vec::new();
    let mut i = 0;
    while i + 8 <= b.len() {
        let (mut size, mut head) = (be32(b, i).unwrap_or(0) as usize, 8);
        if size == 1 {
            size = be64(b, i + 8).unwrap_or(0) as usize;
            head = 16;
        } else if size == 0 {
            size = b.len() - i;
        }
        if size < head || i + size > b.len() {
            break;
        }
        out.push((&b[i + 4..i + 8], &b[i + head..i + size]));
        i += size;
    }
    out
}

fn child<'a>(b: &'a [u8], kind: &[u8]) -> Option<&'a [u8]> {
    boxes(b).into_iter().find(|(k, _)| *k == kind).map(|(_, c)| c)
}

/// The timescale field of an mvhd or mdhd box, version 0 or 1.
fn timescale(full: &[u8]) -> Option<u64> {
    if full.first()? == &1 {
        be32(full, 20)
    } else {
        be32(full, 12)
    }
}

fn mp4_edit(bytes: &[u8]) -> Option<Mp4Edit> {
    let (moov, movie_ts) = movie(bytes)?;
    let (trak, mdia) = sound_track(moov)?;
    let media_ts = timescale(child(mdia, b"mdhd")?)?;
    let (dur, skip) = first_edit(child(child(trak, b"edts")?, b"elst")?)?;
    if movie_ts == 0 || media_ts == 0 || skip == u32::MAX as u64 || skip == u64::MAX {
        return None; // an empty edit (a gap); leave the track as it is
    }
    Some(Mp4Edit { skip, media_timescale: media_ts, seconds: dur as f64 / movie_ts as f64 })
}

/// An MP4's moov box and its movie timescale (None for anything that isn't an MP4).
fn movie(bytes: &[u8]) -> Option<(&[u8], u64)> {
    if bytes.get(4..8)? != b"ftyp" {
        return None;
    }
    let moov = child(bytes, b"moov")?;
    Some((moov, timescale(child(moov, b"mvhd")?)?))
}

/// The first sound trak in moov and its mdia box (None also when a trak before it has no mdia).
fn sound_track(moov: &[u8]) -> Option<(&[u8], &[u8])> {
    for (kind, trak) in boxes(moov) {
        if kind != b"trak" {
            continue;
        }
        let mdia = child(trak, b"mdia")?;
        if child(mdia, b"hdlr").and_then(|h| h.get(8..12)) == Some(b"soun") {
            return Some((trak, mdia));
        }
    }
    None
}

/// The first entry of an elst box: (segment duration in movie units, media time), version 0 or 1.
fn first_edit(elst: &[u8]) -> Option<(u64, u64)> {
    if be32(elst, 4)? < 1 {
        return None;
    }
    if elst.first()? == &1 {
        Some((be64(elst, 8)?, be64(elst, 16)?))
    } else {
        Some((be32(elst, 8)?, be32(elst, 12)?))
    }
}
