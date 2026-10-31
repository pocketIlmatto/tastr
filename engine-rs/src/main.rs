//! `tastr-engine <audio file> [out dir]`: decode, resample and HPSS one file natively. Prints the timings and a
//! fingerprint of the output (to compare with the WebAssembly build), and with an out dir writes y.f32,
//! harmonic.f32 and percussive.f32 there. A .f32 input is raw mono audio at 22,050 Hz and skips decoding.

use std::time::Instant;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: tastr-engine <audio file> [out dir]");
        std::process::exit(2);
    }
    let bytes = std::fs::read(&args[1]).expect("read file");
    let t0 = Instant::now();
    // A .f32 file is raw mono audio already at 22,050 Hz: HPSS only, to compare it with Python's on the same input.
    let d = if args[1].ends_with(".f32") {
        let samples = bytes.chunks_exact(4).map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]])).collect();
        tastr_engine::decode::Decoded { samples, sr: tastr_engine::SR }
    } else {
        tastr_engine::decode::decode(bytes).unwrap_or_else(|e| {
            eprintln!("{e}");
            std::process::exit(1)
        })
    };
    let t1 = Instant::now();
    let y = tastr_engine::resample::resample(&d.samples, d.sr, tastr_engine::SR);
    let t2 = Instant::now();
    let (h, p) = tastr_engine::spectral::hpss(&y);
    let t3 = Instant::now();
    println!(
        "{{\"seconds\": {:.3}, \"decode\": {:.3}, \"resample\": {:.3}, \"hpss\": {:.3}, \"fingerprint\": \"{:016x}\"}}",
        y.len() as f64 / tastr_engine::SR as f64,
        (t1 - t0).as_secs_f64(),
        (t2 - t1).as_secs_f64(),
        (t3 - t2).as_secs_f64(),
        tastr_engine::fingerprint(&[&y, &h, &p])
    );
    if let Some(dir) = args.get(2) {
        std::fs::create_dir_all(dir).expect("out dir");
        for (name, a) in [("y", &y), ("harmonic", &h), ("percussive", &p)] {
            let bytes: Vec<u8> = a.iter().flat_map(|v| v.to_le_bytes()).collect();
            std::fs::write(format!("{dir}/{name}.f32"), bytes).expect("write");
        }
    }
}
