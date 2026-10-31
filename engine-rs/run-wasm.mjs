// node run-wasm.mjs <tastr_engine.wasm> <audio file> [out dir]: the same as the native `tastr-engine` binary, but
// through the WebAssembly build, to check the two give identical output. Prints the time and, with an out dir,
// writes y.f32, harmonic.f32 and percussive.f32 there.
import fs from "node:fs";

const [wasmPath, audioPath, outDir] = process.argv.slice(2);
const { instance } = await WebAssembly.instantiate(fs.readFileSync(wasmPath), {});
const w = instance.exports;
const bytes = fs.readFileSync(audioPath);
const t0 = performance.now();
const ptr = w.tastr_alloc(bytes.length);
new Uint8Array(w.memory.buffer, ptr, bytes.length).set(bytes);
if (w.tastr_signals(ptr, bytes.length) !== 0) throw new Error("could not decode " + audioPath);
const seconds = (performance.now() - t0) / 1000;
const names = ["y", "harmonic", "percussive"];
const arrays = names.map((_, i) => new Float32Array(w.memory.buffer, w.tastr_out_ptr(i), w.tastr_out_len(i)));
console.log(JSON.stringify({ total: +seconds.toFixed(3), samples: arrays[0].length,
                             memory_mb: +(w.memory.buffer.byteLength / 2 ** 20).toFixed(1) }));
if (outDir) {
  fs.mkdirSync(outDir, { recursive: true });
  names.forEach((n, i) => fs.writeFileSync(`${outDir}/${n}.f32`, Buffer.from(arrays[i].slice().buffer)));
}
