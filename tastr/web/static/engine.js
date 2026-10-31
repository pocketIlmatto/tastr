// The Rust engine in the browser (TASTR_BROWSER_ENGINE): this file runs as a worker that loads the engine's
// WebAssembly build (engine-rs/, served at /engine/tastr_engine.wasm), decodes an uploaded file and makes its
// whole card, exactly as the server's Rust engine would. The page sends the card with the file
// (tastr/engine/browser_card.py says what the server checks). Loaded as a page script it does nothing; the page
// only reads its versioned URL to start the worker.
// Message in: {wasm: absolute URL of the build, file: File}. Out: {json, version} or {error}.

if (typeof WorkerGlobalScope !== "undefined" && self instanceof WorkerGlobalScope) {
  self.onmessage = async (e) => {
    const { wasm, file } = e.data;
    try {
      const res = await fetch(wasm);
      if (!res.ok) throw new Error(`engine download failed (${res.status})`);
      const { instance } = await WebAssembly.instantiate(await res.arrayBuffer(), {});
      const w = instance.exports;
      const bytes = new Uint8Array(await file.arrayBuffer());
      const ptr = w.tastr_alloc(bytes.length);
      new Uint8Array(w.memory.buffer, ptr, bytes.length).set(bytes);
      const n = w.tastr_card(ptr, bytes.length, 0);
      if (n < 0) { self.postMessage({ error: "the engine can't decode this file" }); return; }
      const text = (p, len) => new TextDecoder().decode(new Uint8Array(w.memory.buffer, p, len));
      self.postMessage({ json: text(w.tastr_card_ptr(), n), version: text(w.tastr_version(), w.tastr_version_len()) });
    } catch (err) {
      self.postMessage({ error: String(err) });
    }
  };
}
