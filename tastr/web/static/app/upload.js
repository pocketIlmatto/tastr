// Uploading files, folders and links, analysis progress and the in-browser engine.

import { $, api, fmtLeft, h, httpError, jobTitle, nap, progBar } from "./core.js";
import { loadTracks, paintPending, pending, refresh, renderList } from "./library.js";
import { serverCfg } from "./daw.js";

export function setStatus(msg, isErr, busy) {
  const el = $("status");
  el.replaceChildren(...(busy ? [h("span", { class: "spin", "aria-hidden": "true" })] : []), msg);
  el.className = isErr ? "error" : "muted";
}
// The server analyzes one request's files one after another, so send one request per file:
// each placeholder can then turn into a track (or a failure) as soon as its own file is done.
// Each request starts a background job (?background=1), polled for the step it's on; an older server that answers
// with the results straight away still works, without steps.
const A_TYPICAL = 40;  // seconds per track to start the time left from (hosted on Modal, upload included); then the running average
const UP_SHARE = 0.15; // the part of a row's bar the upload takes
// [stage, step name, share of the analysis, "while ..."]: the engine's stages (tastr/engine/stages) and the steps around them
export const A_STEPS = [["waiting", "Waiting for a worker", 0.06, "waiting for a worker"], ["decode", "Decoding audio", 0.08, "decoding the audio"],
  ["core", "Finding tempo, key and beats", 0.34, "finding tempo, key and beats"], ["sections", "Finding sections", 0.2, "finding sections"],
  ["tags", "Tagging vibes", 0.1, "tagging vibes"], ["suggestions", "Writing patterns", 0.16, "writing patterns"], ["views", "Drawing views", 0.06, "drawing views"]];
let queue = Promise.resolve();
function analyzeBatch(items) {
  if (!items.length) return;
  const rows = items.map((it) => ({ name: it.name, active: false }));
  pending.unshift(...rows);
  renderList();
  queue = queue.then(() => runBatch(items, rows));
}
// How far through the analysis a row is (0-1): the steps before its current one, and a slow creep through the current one.
function analysisFrac(row) {
  let acc = 0;
  for (const [k, , share] of A_STEPS) {
    if (k === row.stage) return acc + share * Math.min(0.9, (Date.now() - row.stageAt) / 1000 / (A_TYPICAL * share));
    acc += share;
  }
  return row.stage ? acc : 0;
}
async function followJob(job, row, update) {
  for (let misses = 0; ;) {
    if (job.state === "done") return job;
    if (job.state === "error") throw new Error(job.error || "The analysis failed.");
    if (job.stage !== row.stage) { row.stage = job.stage; row.stageAt = Date.now(); }
    const step = A_STEPS.find(([k]) => k === job.stage);
    row.say = job.stage === "fetch" ? "Fetching from the link"
      : job.state === "queued" ? `Waiting for a worker${job.position ? ` · ${job.position} ahead` : ""}` : step ? `${step[1]}…` : "Analyzing…";
    update();
    await nap(1200);
    try { job = await api(`/api/analyze/jobs/${job.id}`); misses = 0; }
    catch (e) {
      if (/no such job/.test(e.message)) throw new Error("The server lost track of this analysis (it may have restarted). Try again.");
      if (++misses > 20) throw e;  // offline for a while; otherwise keep the last state and try again
    }
  }
}
async function runBatch(items, rows) {
  const n = items.length;
  let done = 0, failed = 0, lastId = null, perFile = A_TYPICAL, timed = 0;
  for (let i = 0; i < n; i++) {
    const row = rows[i], link = !!items[i].link, ready = !!items[i].uploaded;  // ready: already in the bucket (TAS-66)
    Object.assign(row, { active: true, t0: Date.now(), secs: 0, frac: 0, stage: null,
      say: link ? "Fetching from the link" : ready ? "Analyzing…" : "Uploading 0%" });
    rows.slice(i + 1).forEach((q, k) => {
      q.say = items[i + 1 + k].uploaded ? (k ? `Uploaded · ${k} ahead of it` : "Uploaded · up next") : k ? `Waiting · ${k} ahead of it` : "Up next";
    });
    let uploaded = link || ready ? 1 : 0, inBrowser = false;  // true while the browser makes the whole card (browser engine)
    const update = () => {
      row.secs = (Date.now() - row.t0) / 1000;
      const a = row.stage ? analysisFrac(row) : 0;
      row.frac = UP_SHARE * uploaded + (1 - UP_SHARE) * a;
      const left = (1 - row.frac) * perFile + (n - i - 1) * perFile;
      const step = A_STEPS.find(([k]) => k === row.stage);
      const what = row.stage === "fetch" ? "fetching from the link" : step ? step[1].toLowerCase()
        : inBrowser ? "analyzing in your browser"
        : uploaded < 1 ? `uploading ${Math.round(uploaded * 100)}%` : "analyzing";
      const el = $("status");
      el.className = "muted";
      el.replaceChildren(h("span", { class: "spin", "aria-hidden": "true" }), `Analyzing ${i + 1} of ${n} · ${what} · ${fmtLeft(left)}`, progBar((i + row.frac) / n, "Overall progress"));
      jobTitle("analyze", (i + row.frac) / n);
      paintPending();
    };
    update();
    const tick = setInterval(update, 1000);
    let results;
    try {
      let r = await items[i].send((f) => {
        uploaded = f; inBrowser = false;
        row.say = f < 1 ? `Uploading ${Math.round(f * 100)}%` : "Analyzing…";  // "Analyzing…" until the job says which step
        update();
      }, () => {
        inBrowser = true;
        row.say = "Analyzing in your browser";
        update();
      });
      uploaded = 1;
      if (r && r.id && !r.results) r = await followJob(r, row, update);
      results = (r && r.results) || [];
    } catch (e) { results = [{ track_id: null, title: row.name, error: e.message || "The upload failed with no reply from the server.", stage: row.stage }]; }
    clearInterval(tick);
    const bad = results.filter((r) => r.error);
    const ok = results.filter((r) => r.track_id != null);
    done += ok.length; failed += bad.length;
    if (ok.length) {
      lastId = ok[ok.length - 1].track_id;
      perFile = (perFile * (timed + 1) + (Date.now() - row.t0) / 1000) / (timed + 2); timed++;  // running average, seeded with A_TYPICAL
    }
    // Keep a failed row only for what failed; a single result keeps the name the user gave (the server's title carries a hash prefix).
    pending.splice(pending.indexOf(row), 1, ...bad.map((r) => ({ name: results.length === 1 ? row.name : r.title, failed: true, error: r.error, stage: r.stage })));
    try { await loadTracks(); } catch (_) {}
    renderList();
  }
  jobTitle("analyze", null);
  setStatus(`Analyzed ${done} track(s)` + (failed ? `, ${failed} failed` : "") + ".", failed > 0);
  if (lastId != null) await refresh(lastId);
}
// POST with XMLHttpRequest, for its upload progress (fetch has none). Errors read like api()'s.
function postWithProgress(path, body, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", path);
    x.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    x.upload.onload = () => { if (onProgress) onProgress(1); };
    x.onload = () => {
      let j = null;
      try { j = JSON.parse(x.responseText); } catch (_) {}
      if (x.status >= 200 && x.status < 300) resolve(j);
      else reject(new Error(httpError(x.status, j && j.detail, x.statusText)));
    };
    x.onerror = () => reject(new Error("Couldn't reach the server."));
    x.send(body);
  });
}
// A file's length in minutes from its header, read by the browser without uploading it (null if it can't tell).
function audioMinutes(file) {
  return new Promise((resolve) => {
    const a = new Audio(), url = URL.createObjectURL(file);
    const done = (v) => { clearTimeout(t); URL.revokeObjectURL(url); a.removeAttribute("src"); resolve(v); };
    const t = setTimeout(() => done(null), 5000);
    a.preload = "metadata";
    a.onloadedmetadata = () => done(Number.isFinite(a.duration) ? a.duration / 60 : null);
    a.onerror = () => done(null);
    a.src = url;
  });
}
// A hosted server's size and length limits, checked before uploading so a file it would refuse fails at once
// (the server checks again).
async function checkUpload(f) {
  const L = serverCfg.limits;
  if (!L) return;
  if (L.max_upload_mb && f.size > L.max_upload_mb * 1024 * 1024)
    throw new Error(`${f.name} is ${Math.round(f.size / 1048576)} MB; this server takes files up to ${L.max_upload_mb} MB.`);
  const min = L.max_minutes ? await audioMinutes(f) : null;
  if (min != null && min > L.max_minutes)
    throw new Error(`${f.name} is ${Math.round(min)} minutes long. This server analyzes tracks up to ${L.max_minutes} minutes, so a long mix needs cutting into shorter files first.`);
}
function uploadFiles(files) {
  if (serverCfg.direct_upload) { directBatch(files); return; }
  analyzeBatch(files.map((f) => ({
    name: f.name,
    send: async (onProgress, onMeasure) => {
      await checkUpload(f);
      const fd = new FormData();
      const card = serverCfg.browser_engine ? await cardInBrowser(f, onMeasure) : null;
      fd.append("files", f);
      if (card) fd.append("card", card, "card.json");
      return postWithProgress("/api/analyze?background=1", fd, onProgress);
    },
  })));
}

// TASTR_DIRECT_UPLOAD (hosted, TAS-66): each file goes straight to the audio bucket, 3 at a time, each row with
// its own "Uploading N%": 1. the server checks the limits and hands out a signed link (refusals come before any
// bytes move); 2. the page PUTs the file there; 3. the server checks what arrived ("Checking the upload…").
// The uploaded files then take their turn to be analyzed as usual. A link that ran out is renewed once; a dropped
// connection or a storage refusal leaves a row with Retry, which starts that file over.
const UP_AT_ONCE = 3;
function directBatch(files) {
  if (!files.length) return;
  const rows = files.map((f) => ({ name: f.name, active: false, uploading: true, say: "Getting ready to upload…", frac: 0, up: 0 }));
  pending.unshift(...rows);
  renderList();
  queue = queue.then(async () => {
    const n = rows.length;
    const paint = () => {
      const avg = rows.reduce((a, r) => a + r.up, 0) / n, el = $("status");
      el.className = "muted";
      el.replaceChildren(h("span", { class: "spin", "aria-hidden": "true" }), `Uploading ${n} ${n === 1 ? "file" : "files"}… ${Math.round(avg * 100)}%`,
        progBar(avg * UP_SHARE, "Overall progress"));
      jobTitle("analyze", avg * UP_SHARE);
      paintPending();
    };
    paint();
    const ids = new Array(n).fill(null);
    let next = 0;
    await Promise.all(Array.from({ length: Math.min(UP_AT_ONCE, n) }, async () => {
      while (next < n) { const i = next++; ids[i] = await toBucket(files[i], rows[i], paint); }
    }));
    const keep = ids.flatMap((id, i) => (id ? [i] : []));
    if (!keep.length) { jobTitle("analyze", null); setStatus("Nothing was uploaded. See the list for why.", true); return; }
    await runBatch(keep.map((i) => ({
      name: files[i].name, uploaded: true,
      send: async (_onProgress, onMeasure) => {
        const fd = new FormData();
        fd.append("ids", ids[i]);
        const card = serverCfg.browser_engine ? await cardInBrowser(files[i], onMeasure) : null;
        if (card) fd.append("card", card, "card.json");
        return postWithProgress("/api/analyze/uploaded?background=1", fd);
      },
    })), keep.map((i) => rows[i]));
  });
}
// One file of a direct batch: its upload's id once the server has checked it, or null with the row turned into a
// failed one that says why.
async function toBucket(f, row, paint) {
  const fail = (why, retry) => {
    pending.splice(pending.indexOf(row), 1);
    Object.assign(row, { failed: true, uploading: false, retrying: false, up: 1, why: why + " ", retry: retry ? () => directBatch([f]) : null });
    pending.push(row);
    renderList(); paint();
    return null;
  };
  const post = (path, body) => api(path, { method: "POST", ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}) });
  const pct = () => Math.round(row.up * 100);
  try { await checkUpload(f); } catch (e) { return fail(`Not uploaded: ${e.message} Nothing was sent.`); }
  let up;
  try { up = (await post("/api/uploads", { files: [{ name: f.name, size: f.size }] })).uploads[0]; }
  catch (e) {
    return e.status ? fail(`Not uploaded: ${e.message} Nothing was sent.`)
      : fail("Not uploaded: couldn't reach the server. Nothing was sent.", true);
  }
  const onProgress = (x) => { row.up = x; row.frac = x * UP_SHARE; row.say = `Uploading ${pct()}%`; paint(); };
  onProgress(0);
  for (let tries = 0; ; tries++) {
    try { await putWithProgress(up, f, onProgress); break; }
    catch (e) {
      if (e.expired && !tries) {  // the link ran out on the way: a fresh one, and start again, once
        row.retrying = true; row.say = `Uploading ${pct()}% · the link expired, starting again…`; paint();
        try { [up] = await Promise.all([post(`/api/uploads/${up.id}/link`), nap(1000)]); }
        catch (_) { return fail(`Upload stopped at ${pct()}%: the connection dropped. Nothing was kept.`, true); }
        row.retrying = false; onProgress(0);
        continue;
      }
      if (e.dropped) return fail(`Upload stopped at ${pct()}%: the connection dropped. Nothing was kept.`, true);
      post(`/api/uploads/${up.id}/failed`, { status: e.status || 0 }).catch(() => {});  // logged for the admin
      return fail("Couldn't upload: the storage refused the file. That's a setup problem on the server, not your file; it's been logged for the admin.", true);
    }
  }
  row.say = "Checking the upload…"; paint();
  try { await post(`/api/uploads/${up.id}/check`); }
  catch (e) {
    // Too big or too long once read: the file itself is the problem, so no Retry. The server already deleted it.
    if (e.status === 413) return fail(`Not kept: ${e.message} The uploaded copy was deleted.`);
    return fail(`Couldn't upload: ${e.message}`, true);
  }
  row.say = "Uploaded · up next"; row.up = 1; paint();
  return up.id;
}
// PUT a file to a signed bucket link, with upload progress. Errors say what happened: `expired` (a 403, how the
// bucket says the link ran out), `dropped` (the connection went mid-upload), or neither (the bucket refused it:
// another status, or no answer before any bytes went, which is what a missing CORS rule looks like).
function putWithProgress(up, file, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    let sent = 0;
    x.open("PUT", up.url);
    x.setRequestHeader("Content-Type", up.content_type);
    x.upload.onprogress = (e) => { if (e.lengthComputable) { sent = e.loaded; if (onProgress) onProgress(e.loaded / e.total); } };
    x.onload = () => {
      if (x.status >= 200 && x.status < 300) { if (onProgress) onProgress(1); resolve(); return; }
      reject(Object.assign(new Error(`Storage refused the upload (${x.status}${x.statusText ? ` ${x.statusText}` : ""}).`),
        { status: x.status, expired: x.status === 403 }));
    };
    const stopped = () => reject(Object.assign(new Error("The upload to storage stopped."), { dropped: sent > 0 || !navigator.onLine }));
    x.onerror = stopped;
    x.onabort = x.ontimeout = () => reject(Object.assign(new Error("The upload to storage stopped."), { dropped: true }));
    x.send(file);
  });
}
// TASTR_BROWSER_ENGINE: make the whole card here with the Rust engine's WebAssembly build (static/engine.js, a
// worker per file so its memory goes when it's done) and send it with the file; the server stores it when it checks
// out (tastr/engine/browser_card.py). Opus (which the engine can't decode), long tracks and any failure leave the
// analysis to the server.
const ENGINE_MAX_MINUTES = 20;
async function cardInBrowser(file, onMeasure) {
  const cfg = serverCfg.browser_engine;
  const script = document.querySelector('script[src*="/static/engine.js"]');
  if (!cfg || !script || typeof Worker !== "function" || typeof WebAssembly !== "object" || /\.opus$/i.test(file.name)) return null;
  const min = await audioMinutes(file);
  if (min != null && min > ENGINE_MAX_MINUTES) return null;
  onMeasure();
  const out = await new Promise((resolve) => {
    const w = new Worker(script.src);
    const done = (v) => { w.terminate(); resolve(v); };
    w.onmessage = (e) => done(e.data);
    w.onerror = (e) => done({ error: e.message || "worker failed" });
    w.postMessage({ wasm: new URL(cfg.wasm, location.href).href, file });
  });
  if (out.error) { console.warn("browser analysis failed; the server will analyze this file", out.error); return null; }
  const msg = `{"format":"tastr-card","version":1,"engine":${JSON.stringify(cfg.engine)},` +
    `"engine_version":${JSON.stringify(out.version)},"card":${out.json}}`;
  return new Blob([msg], { type: "application/json" });
}
const drop = $("drop");
// Dropped folders are walked recursively; only audio files inside them are analyzed (files dropped directly are always sent).
const AUDIO_EXT = /\.(mp3|wav|flac|ogg|oga|opus|m4a|aac|aif|aiff|wma)$/i;
const isAudio = (f) => (f.type || "").startsWith("audio/") || AUDIO_EXT.test(f.name);
const readBatch = (reader) => new Promise((res, rej) => reader.readEntries(res, rej));
const entryFile = (entry) => new Promise((res, rej) => entry.file(res, rej));
async function walkEntry(entry, out, nested) {
  if (entry.isFile) {
    if (entry.name.startsWith(".")) return;
    const f = await entryFile(entry);
    if (!nested || isAudio(f)) out.push(f);
  } else if (entry.isDirectory) {
    const reader = entry.createReader();
    // readEntries returns at most ~100 entries per call, so keep reading until it comes back empty.
    for (let batch = await readBatch(reader); batch.length; batch = await readBatch(reader)) {
      for (const child of batch) await walkEntry(child, out, true);
    }
  }
}
async function collectDropped(dt) {
  // The entries must be taken synchronously; the DataTransfer is emptied once the event handler returns.
  const entries = [...(dt.items || [])].map((i) => (i.kind === "file" && i.webkitGetAsEntry ? i.webkitGetAsEntry() : null));
  if (!entries.length || entries.some((en) => !en)) return [...dt.files];
  const files = [];
  for (const en of entries) await walkEntry(en, files, false);
  return files;
}
async function uploadDropped(dt) {
  const hadFolder = [...(dt.items || [])].some((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()?.isDirectory);
  let files;
  try { files = await collectDropped(dt); } catch (_) { files = [...dt.files]; }
  if (!files.length && hadFolder) setStatus("No audio files found in the dropped folder(s).", true);
  else uploadFiles(files);
}

// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => { uploadDropped(e.dataTransfer); });
  $("file-input").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });
  $("folder-input").addEventListener("change", (e) => {
    const files = [...e.target.files].filter((f) => isAudio(f) && !f.name.startsWith("."));
    if (files.length) uploadFiles(files); else setStatus("No audio files found in that folder.", true);
    e.target.value = "";
  });
  $("url-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const url = $("url-input").value.trim();
    if (!url) return;
    analyzeBatch([{ name: url, link: true, send: () => api("/api/analyze-url?background=1", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) }) }]);
    $("url-input").value = "";
  });
}
