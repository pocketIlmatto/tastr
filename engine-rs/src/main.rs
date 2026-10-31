//! `tastr-engine <audio file> [out dir]`: one file through the native build. Prints the timings and a
//! fingerprint of every output (to compare with the WebAssembly build), and with an out dir writes each output
//! there as `<name>.f32` (little-endian, row-major) plus `manifest.json` ({name: [rows, cols]}). A .f32 input is
//! raw mono audio already at 22,050 Hz and skips decoding.

use std::time::Instant;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: tastr-engine <audio file> [out dir]");
        std::process::exit(2);
    }
    let bytes = std::fs::read(&args[1]).expect("read file");
    let t0 = Instant::now();
    let y = if args[1].ends_with(".f32") {
        bytes.chunks_exact(4).map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]])).collect()
    } else {
        tastr_engine::load(bytes).unwrap_or_else(|e| {
            eprintln!("{e}");
            std::process::exit(1)
        })
    };
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
        std::fs::create_dir_all(dir).expect("out dir");
        let mut manifest = Vec::new();
        for (name, m) in &out {
            let bytes: Vec<u8> = m.data.iter().flat_map(|v| v.to_le_bytes()).collect();
            std::fs::write(format!("{dir}/{name}.f32"), bytes).expect("write");
            manifest.push(format!("\"{name}\": [{}, {}]", m.rows, m.cols));
        }
        std::fs::write(format!("{dir}/manifest.json"), format!("{{{}}}\n", manifest.join(", "))).expect("write");
    }
}
