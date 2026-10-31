//! `tastr-engine <audio file> [out dir]`: one file through the native build. Prints the timings and a
//! fingerprint of every output (to compare with the WebAssembly build), and with an out dir writes each output
//! there as `<name>.f32` (little-endian, row-major) plus `manifest.json` ({name: [rows, cols]}). A .f32 input is
//! raw mono audio already at 22,050 Hz and skips decoding.
//!
//! For checking single steps against the Python engine on the same input, a few commands take a .f32 input and
//! write their outputs the same way:
//!
//! - `tastr-engine rhythm <onset envelope.f32> [bpm] <out dir>`: tempogram mean, tempo, beats (and beats at the
//!   given bpm), onsets.
//! - `tastr-engine chroma <harmonic audio.f32> <out dir>`: tuning, chroma and the bass CQT.
//! - `tastr-engine ward <features.f32> <rows> <k> <out dir>`: Ward boundaries of the rows of a matrix.
//!
//! `tastr-engine card <audio file> <out.json> [beats per bar]` writes the whole card (TrackCard.to_dict() JSON,
//! "source" null) and prints the timings.

use std::time::Instant;
use tastr_engine::Matrix;

fn read_f32(path: &str) -> Vec<f32> {
    let bytes = std::fs::read(path).expect("read file");
    bytes.as_chunks::<4>().0.iter().map(|b| f32::from_le_bytes(*b)).collect()
}

fn write(dir: &str, out: &[(&'static str, Matrix)]) {
    std::fs::create_dir_all(dir).expect("out dir");
    let mut manifest = Vec::new();
    for (name, m) in out {
        let bytes: Vec<u8> = m.data.iter().flat_map(|v| v.to_le_bytes()).collect();
        std::fs::write(format!("{dir}/{name}.f32"), bytes).expect("write");
        manifest.push(format!("\"{name}\": [{}, {}]", m.rows, m.cols));
    }
    std::fs::write(format!("{dir}/manifest.json"), format!("{{{}}}\n", manifest.join(", "))).expect("write");
}

/// Audio at 22,050 Hz from a file (a .f32 file is taken as that already).
fn load(file: &str) -> Vec<f32> {
    if file.ends_with(".f32") {
        return read_f32(file);
    }
    tastr_engine::load(std::fs::read(file).expect("read file")).unwrap_or_else(|e| {
        eprintln!("{e}");
        std::process::exit(1)
    })
}

fn usage() -> ! {
    eprintln!("usage: tastr-engine <audio file> [out dir] | rhythm <onset.f32> [bpm] <out dir> | chroma <harmonic.f32> <out dir> | ward <z.f32> <rows> <k> <out dir> | card <audio file> <out.json> [beats per bar]");
    std::process::exit(2);
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let arg = |i: usize| args.get(i).map(String::as_str).unwrap_or_else(|| usage());
    match arg(1) {
        "rhythm" => {
            let (bpm, out) = if args.len() > 4 { (Some(arg(3).parse().expect("bpm")), arg(4)) } else { (None, arg(3)) };
            write(out, &tastr_engine::rhythm_outputs(&read_f32(arg(2)), bpm));
        }
        "chroma" => write(arg(3), &tastr_engine::chroma_outputs(&read_f32(arg(2)))),
        "ward" => {
            let data = read_f32(arg(2));
            let rows: usize = arg(3).parse().expect("rows");
            let k: usize = arg(4).parse().expect("k");
            let z = Matrix { rows, cols: data.len() / rows, data };
            let b = tastr_engine::segment::agglomerative(&z, k);
            let data: Vec<f32> = b.iter().map(|&v| v as f32).collect();
            write(arg(5), &[("boundaries", Matrix { rows: 1, cols: data.len(), data })]);
        }
        "card" => {
            let t0 = Instant::now();
            let y = load(arg(2));
            let t1 = Instant::now();
            let meter = args.get(4).map(|m| m.parse().expect("beats per bar"));
            let json = tastr_engine::card_json(y, meter);
            let t2 = Instant::now();
            std::fs::write(arg(3), json + "\n").expect("write");
            println!("{{\"load\": {:.3}, \"card\": {:.3}}}", (t1 - t0).as_secs_f64(), (t2 - t1).as_secs_f64());
        }
        file => {
            let t0 = Instant::now();
            let y = load(file);
            let t1 = Instant::now();
            let seconds = y.len() as f64 / tastr_engine::SR as f64;
            let out = tastr_engine::signals(y);
            let t2 = Instant::now();
            println!(
                "{{\"seconds\": {:.3}, \"load\": {:.3}, \"signals\": {:.3}, \"fingerprint\": \"{:016x}\"}}",
                seconds,
                (t1 - t0).as_secs_f64(),
                (t2 - t1).as_secs_f64(),
                tastr_engine::fingerprint(out.iter().map(|(_, m)| m.data.as_slice()))
            );
            if let Some(dir) = args.get(2) {
                write(dir, &out);
            }
        }
    }
}
