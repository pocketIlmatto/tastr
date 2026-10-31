// node run-wasm.mjs <tastr_engine.wasm> <audio file> [out dir]: the same as the native `tastr-engine` binary, but
// through the WebAssembly build, to check the two give identical output. Prints the time and memory and, with an
// out dir, writes each output there as <name>.f32 plus manifest.json, like the binary.
// node run-wasm.mjs <tastr_engine.wasm> card <audio file> <out.json> [beats per bar]: the whole card, like
// `tastr-engine card`.
import fs from "node:fs";

const args = process.argv.slice(2);
const { instance } = await WebAssembly.instantiate(fs.readFileSync(args[0]), {});
const w = instance.exports;
if (args[1] === "card") {
  const bytes = fs.readFileSync(args[2]);
  const t0 = performance.now();
  const ptr = w.tastr_alloc(bytes.length);
  new Uint8Array(w.memory.buffer, ptr, bytes.length).set(bytes);
  const n = w.tastr_card(ptr, bytes.length, Number(args[4] || 0));
  if (n < 0) {
    console.error("could not decode " + args[2]);
    process.exit(1);
  }
  const json = new TextDecoder().decode(new Uint8Array(w.memory.buffer, w.tastr_card_ptr(), n));
  fs.writeFileSync(args[3], json + "\n");
  console.log(JSON.stringify({ card: +((performance.now() - t0) / 1000).toFixed(3),
                               memory_mb: +(w.memory.buffer.byteLength / 2 ** 20).toFixed(1) }));
  process.exit(0);
}
const [, audioPath, outDir] = args;
const bytes = fs.readFileSync(audioPath);
const t0 = performance.now();
const ptr = w.tastr_alloc(bytes.length);
new Uint8Array(w.memory.buffer, ptr, bytes.length).set(bytes);
const count = w.tastr_signals(ptr, bytes.length);
if (count < 0) {
  console.error("could not decode " + audioPath);
  process.exit(1);
}
const seconds = (performance.now() - t0) / 1000;
const outputs = [];
for (let i = 0; i < count; i++) {
  const name = new TextDecoder().decode(new Uint8Array(w.memory.buffer, w.tastr_out_name(i), w.tastr_out_name_len(i)));
  const data = new Float32Array(w.memory.buffer, w.tastr_out_ptr(i), w.tastr_out_len(i)).slice();
  const rows = w.tastr_out_rows(i);
  outputs.push({ name, rows, cols: data.length / rows, data });
}
console.log(JSON.stringify({ total: +seconds.toFixed(3), samples: outputs[0].data.length,
                             memory_mb: +(w.memory.buffer.byteLength / 2 ** 20).toFixed(1) }));
if (outDir) {
  fs.mkdirSync(outDir, { recursive: true });
  for (const o of outputs) fs.writeFileSync(`${outDir}/${o.name}.f32`, Buffer.from(o.data.buffer));
  fs.writeFileSync(`${outDir}/manifest.json`,
                   JSON.stringify(Object.fromEntries(outputs.map((o) => [o.name, [o.rows, o.cols]]))) + "\n");
}
