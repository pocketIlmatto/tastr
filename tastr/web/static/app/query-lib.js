// Searches in the browser on a hosted server (TAS-49; UI designed in the sandbox, v174). On the first search the page
// fetches the person's own library once as a small SQLite file (GET /api/query/library: their tracks, no owner or
// path columns) and hands it to a worker running sql.js (static/sql-worker.js); every search then runs there, with no
// round trip. On your own machine (tastr serve) none of this runs: the server answers /api/query as before.
// States: none -> loading -> ready -> stale (the library changed; the next search refreshes first); loading -> error.

import { $, h, tracks, trackHooks } from "./core.js";

const MAX_ROWS = 5000;  // as the server's /api/query
const STOP_AFTER_MS = 1000;  // Run turns into "Running… Stop" after this long
let cfg = null;  // /api/config's query_browser, or null: the server runs searches
const lib = { state: "none", at: 0, tracks: 0, key: "", added: 0, err: "", where: "", pct: 0, kb: 0, pending: null, buf: null };
let worker = null, workerHas = false, busy = null;  // busy: {resolve, reject, timer} of the running query
let rerun = () => {};  // runs the current mode's latest search again (Try again, Refresh)

export const inBrowser = () => !!cfg;
export function setup(queryBrowser, rerunLatest) {
  cfg = queryBrowser || null;
  rerun = rerunLatest;
  trackHooks.add(noticeChanges);
  paint();
}

// What the page knows about the library, to notice it changed after the copy was made (no polling).
const libKey = () => `${tracks.length}:${Math.max(0, ...tracks.map((t) => t.id))}:${tracks.reduce((m, t) => (t.analyzed_at > m ? t.analyzed_at : m), "")}`;
function noticeChanges() {
  if (lib.state !== "ready" || libKey() === lib.key) return;
  lib.state = "stale"; lib.added = Math.max(0, tracks.length - lib.ownTracks); paint();
}

// Every search goes through here: run now, or wait for the library copy first (hosted only). Only the latest search
// waits; one queued earlier while loading is dropped.
export function withLib(run) {
  if (!cfg || lib.state === "ready") return run();
  lib.pending = run;
  if (lib.state !== "loading") load();
}

function talk(msg) {
  return new Promise((resolve, reject) => {
    worker.onmessage = (e) => (e.data.error ? reject(Object.assign(new Error(e.data.error), { where: e.data.where })) : resolve(e.data));
    worker.onerror = (e) => { e.preventDefault(); reject(Object.assign(new Error(e.message || "the worker failed"), { where: "engine" })); };
    worker.postMessage(msg);
  });
}
async function startWorker() {
  worker?.terminate();
  worker = new Worker("/static/sql-worker.js");
  workerHas = false;
  await talk({ base: new URL(cfg.base, location.href).href, lib: lib.buf });  // a copy: the page keeps lib.buf
  workerHas = true;
}

async function download() {
  const r = await fetch("/api/query/library");
  if (!r.ok) throw new Error(r.status === 401 ? "you're signed out; reload the page" : `the server answered ${r.status}`);
  const total = +r.headers.get("X-Library-Bytes") || 0;
  lib.kb = Math.max(1, Math.round(total / 1024)); paint();
  if (!r.body || !total) return new Uint8Array(await r.arrayBuffer()).buffer;
  const reader = r.body.getReader(), parts = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    parts.push(value); got += value.length;
    lib.pct = Math.min(99, Math.round((got / total) * 100)); paint();
  }
  const out = new Uint8Array(got);
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.length; }
  return out.buffer;
}

async function load() {
  const refreshing = !!lib.at;
  Object.assign(lib, { state: "loading", pct: 0, err: "", where: "", kb: 0, refreshing });
  paint();
  const key = libKey(), own = tracks.length;
  try {
    try { lib.buf = await download(); } catch (e) { throw Object.assign(e instanceof TypeError ? new Error("network error") : e, { where: "library" }); }
    lib.pct = 100; paint();
    await startWorker();
    const n = await talk({ sql: "SELECT count(*) FROM tracks", max: 1 });
    Object.assign(lib, { state: "ready", at: Date.now(), tracks: n.rows[0][0], key, ownTracks: own, added: 0 });
  } catch (e) {
    worker?.terminate(); worker = null; workerHas = false;
    Object.assign(lib, { state: "error", err: e.message, where: e.where || "library", pending: null, buf: null });
    paint();
    clearResults();
    return;
  }
  paint();
  const go = lib.pending; lib.pending = null;
  if (go) go();
  else noticeChanges();
}

function clearResults() {  // after a failed load, so old rows don't look like an answer
  $("q-count").textContent = ""; $("sql-result").replaceChildren(); $("sql-error").textContent = "";
}

// One SELECT on the copy: {columns, rows}, like /api/query. Rejects with the SQLite message, or "stopped".
export async function query(sql) {
  if (busy) stop();  // a newer search replaces one still running
  if (!worker || !workerHas) await startWorker();
  return new Promise((resolve, reject) => {
    busy = { resolve, reject, timer: setTimeout(() => paintRun(true), STOP_AFTER_MS) };
    talk({ sql, max: MAX_ROWS }).then((r) => finish(() => resolve(r)), (e) => finish(() => reject(e)));
  });
}
function finish(then) {
  if (!busy) return;
  clearTimeout(busy.timer); busy = null; paintRun(false); then();
}
// Stop a runaway query: end the worker (the only way to interrupt sql.js); the next search starts a fresh one.
export function stop() {
  if (!busy) return;
  worker?.terminate(); worker = null; workerHas = false;
  const { reject } = busy;
  finish(() => reject(new Error("Stopped.")));
}

const hhmm = (t) => new Date(t).toTimeString().slice(0, 5);
function paintRun(running) {
  const b = $("run-sql");
  if (lib.state === "loading") { b.disabled = true; b.textContent = "Loading…"; b.onclick = null; return; }
  b.disabled = false;
  b.textContent = running ? "Running… Stop" : "Run";
  b.dataset.stop = running ? "1" : "";
}
export const stopRequested = () => $("run-sql").dataset.stop === "1";

function paint() {
  const box = $("q-lib");
  box.className = "q-lib";
  paintRun(!!busy && $("run-sql").dataset.stop === "1");
  if (!cfg || lib.state === "none") return box.replaceChildren();
  const again = (label) => h("button", { type: "button", class: label === "Try again" ? "" : "linklike", onclick: () => { lib.pending = rerun; load(); } }, label);
  if (lib.state === "loading") {
    box.classList.add("busy");
    const what = lib.refreshing ? "Refreshing your library copy…"
      : `Getting your library ready to search${lib.kb ? ` (${lib.kb.toLocaleString()} KB, once)` : ""}…`;
    return box.replaceChildren(h("span", { class: "spin", "aria-hidden": "true" }), h("span", {}, what),
      h("span", { class: "q-lib-bar", "aria-hidden": "true" }, h("i", { style: `width:${lib.pct}%` })));
  }
  if (lib.state === "error") {
    box.classList.add("bad");
    const engine = lib.where === "engine";
    return box.replaceChildren(h("b", {}, engine ? "Search couldn't start in this browser." : "Couldn't load your library for searching."), " ",
      h("span", {}, engine ? "The search engine (WebAssembly) didn't load. A blocker or a strict network can stop it."
        : `The download stopped (${lib.err}). Your tracks are fine.`), " ", again("Try again"));
  }
  if (lib.state === "stale") {
    box.classList.add("stale");
    const n = lib.added ? ` (${lib.added} new track${lib.added === 1 ? "" : "s"})` : "";
    return box.replaceChildren(h("span", {}, `Your library changed since ${hhmm(lib.at)}${n}. The next search updates the copy first, or `), again("update now"), ".");
  }
  box.replaceChildren(h("span", { class: "muted", title: "Searches run in your browser on this copy, so they're instant and never change anything in Tastr." },
    `Searching a copy of your library from ${hhmm(lib.at)} · ${lib.tracks} track${lib.tracks === 1 ? "" : "s"} · `), again("Refresh"));
}

// The stale state refreshes on the next search: withLib sees "stale" isn't "ready" and loads first.
