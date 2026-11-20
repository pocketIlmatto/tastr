// The bank of deposits (features.md, "The bank"): its own page at its own address, beside the app. /bank lists every
// deposit newest first; /bank/<id> shows one. It reads /api/deposits (routes/deposits.py) and nothing in the app's
// views changes. Entry point of static/bank.html; it imports only core.js, so the app's modules don't load here.

import { $, VIBE_MIN, api, cap, h, isNum, mmss, svg } from "./core.js";

const PAGE = 200;  // deposits per fetch; "Show more" asks for the next ones
const SIZE_NAME = { moment: "Moment", track: "Track", collection: "Collection" };
const STEM_NAME = { vocals: "Vocals", drums: "Drums", bass: "Bass", other: "Other" };
const STAGES = [["fetch", "Find the audio", "finding the audio"], ["slice", "Cut the slice", "cutting the slice"],
  ["analyze", "Analyze it", "analyzing"], ["stems", "Separate the stems", "separating stems"]];
const SITES = { youtube: "YouTube", "youtu.be": "YouTube", soundcloud: "SoundCloud", bandcamp: "Bandcamp", mixcloud: "Mixcloud",
  vimeo: "Vimeo", spotify: "Spotify" };
const POLL_MS = 4000;  // a deposit that is still breaking down is read again this often while its page is open

// Dates as the Library's sidebar shows uploads (library.js fmtUp, fmtUpLong): the time today, the weekday this week,
// else the date. Kept here so this page doesn't load the Library's module graph.
const DAY = 864e5, dayStart = (t) => { const d = new Date(t); d.setHours(0, 0, 0, 0); return +d; };
const fmtUp = (t) => {
  if (!t) return "";
  const today = dayStart(Date.now()), d = new Date(t);
  if (t >= today) return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (t >= today - DAY) return "Yesterday";
  if (t >= today - 6 * DAY) return d.toLocaleDateString([], { weekday: "short" });
  return d.toLocaleDateString([], { month: "short", day: "numeric", ...(d.getFullYear() !== new Date().getFullYear() ? { year: "numeric" } : {}) });
};
const fmtUpLong = (t) => new Date(t).toLocaleString([], { weekday: "short", month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });

// ---- reading a deposit ----

const ctxOf = (dp) => dp.context || {};
const whenOf = (dp) => Date.parse(dp.created_at) || 0;
const artistOf = (dp) => (dp.track && dp.track.artist) || ctxOf(dp).artist || "";
export function siteOf(dp) {
  const s = ctxOf(dp).site;
  if (s) return SITES[String(s).toLowerCase()] || cap(String(s));
  try {
    const host = new URL(dp.source_uri).hostname.replace(/^(www|m|music)\./, "");
    const k = Object.keys(SITES).find((x) => host.includes(x));
    return k ? SITES[k] : host;
  } catch (_) { return "Link"; }
}
// A finished moment's title is "<source title> 15:38-16:30" (moments.py); the range is shown on its own.
const noRange = (t) => (t || "").replace(/ \d+(?::\d\d)+-\d+(?::\d\d)+$/, "");
export function titleOf(dp) {
  return noRange(dp.title) || ctxOf(dp).title || (dp.source_uri ? siteOf(dp) : "") || `Deposit ${dp.id}`;
}
const titleArtist = (dp) => (artistOf(dp) ? `${titleOf(dp)} · ${artistOf(dp)}` : titleOf(dp));
export function rangeOf(x) {
  if (isNum(x.start_s) && isNum(x.end_s)) return `${mmss(x.start_s)}–${mmss(x.end_s)}`;
  const at = (x.context || {}).at_s;
  return isNum(at) ? `around ${mmss(at)}` : "";
}

// What the tooling found, from wherever this deposit keeps it: a moment's analysis of its slice
// (extracted.analysis), a fetched source's audio (extracted.audio), or the library track it points at.
export function foundOf(dp) {
  const x = dp.extracted || {}, a = x.analysis || {}, t = dp.track || {};
  const pick = (...v) => v.find((y) => y != null && y !== "");
  const bpm = pick(a.bpm, x.bpm, t.bpm), cam = pick(a.camelot, x.camelot, t.camelot), keyName = pick(a.key, x.key, t.key);
  const span = isNum(dp.start_s) && isNum(dp.end_s) ? dp.end_s - dp.start_s : null;
  const len = pick(a.duration, x.duration, (x.slice || {}).duration, (x.audio || {}).duration, t.duration, span);
  const bpb = pick(a.beats_per_bar, x.beats_per_bar);
  const bars = pick(x.bars, dp.size === "moment" && isNum(bpm) && isNum(len) && bpm > 0 ? Math.max(1, Math.round((len * bpm) / 60 / (bpb || 4))) : null);
  const stems = x.stems && Array.isArray(x.stems.names) && x.stems.names.length ? x.stems.names : null;
  const seen = new Set(), tags = [];
  for (const g of [...(dp.tags || []), ...(a.tags || [])]) {
    const tag = typeof g === "string" ? g : g.tag;
    if (!tag || seen.has(tag) || (isNum(g.score) && g.score < VIBE_MIN)) continue;
    seen.add(tag); tags.push(tag);
  }
  return { bpm, cam, keyName, keyTo: x.key_to, keyAt: x.key_at_bar, len, bars, meter: x.meter || `${bpb || 4}/4`, stems, tags,
    chords: Array.isArray(x.chords) ? x.chords : null, drums: x.drums || null };
}
const keyText = (f) => (f.cam && f.keyName ? `${f.cam} (${f.keyName})` : f.cam || f.keyName || "");
const stageAt = (dp) => Math.max(0, STAGES.findIndex(([k]) => k === (dp.extracted || {}).stage));
const captureSite = (dp) => (ctxOf(dp).via || "").split("·").map((s) => s.trim()).filter(Boolean).pop() || "the app";

// The list's one line on what the tooling found, or why it hasn't yet (with the colour class for that state).
export function foundLine(dp) {
  const x = dp.extracted || {};
  if (dp.status === "working") return { t: `Breaking down: ${STAGES[stageAt(dp)][2]}…`, cls: "bk-work" };
  if (dp.status === "needs-source") {
    if (dp.size === "track" && dp.source_kind === "track") return { t: "Needs sourcing: its track was deleted from your library.", cls: "bk-need" };
    if (dp.source_uri) return { t: "Needs sourcing: Tastr has the link, but hasn't fetched the audio yet.", cls: "bk-need" };
    return { t: "Needs sourcing: Tastr knows the track and the spot, but has no audio yet.", cls: "bk-need" };
  }
  if (dp.status === "failed") return { t: `Couldn't get the audio. ${x.error || ""}`.trim(), cls: "bk-fail" };
  const f = foundOf(dp);
  if (dp.size === "collection") {
    const n = dp.holds_count || 0;
    return { t: [isNum(f.len) ? `${mmss(f.len)} long` : null, `${n} moment${n === 1 ? "" : "s"} cut from it`].filter(Boolean).join(" · "), cls: "" };
  }
  const bits = [isNum(f.bpm) ? `${Math.round(f.bpm)} BPM` : null, f.cam || f.keyName ? (f.keyTo ? `${f.cam || f.keyName} → ${f.keyTo}` : f.cam || f.keyName) : null,
    isNum(f.bars) ? `${f.bars} bars` : isNum(f.len) ? mmss(f.len) : null, f.stems ? "stems" : null,
    dp.touches_count > 1 ? `${dp.touches_count} tracks named` : null, ...f.tags.slice(0, 2)];
  return { t: bits.filter(Boolean).join(" · "), cls: "" };
}

// ---- addresses: /bank and /bank/<id>, with Back and Forward ----

const idFromPath = (p = location.pathname) => { const m = /^\/bank\/(\d+)\/?$/.exec(p); return m ? +m[1] : null; };
function go(path) {
  if (location.pathname !== path) history.pushState(null, "", path);
  render();
  window.scrollTo(0, 0);
}
// A link that opens in this page, unless the person asked for a new tab or window.
const link = (path, ...kids) => h("a", { href: path, onclick: (e) => {
  if (e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault(); go(path);
} }, ...kids);
const badge = (size) => h("span", { class: `bk-size bk-${size}` }, SIZE_NAME[size] || size);

function sourceBits(dp, long) {
  const r = rangeOf(dp), t = titleOf(dp), a = artistOf(dp);
  if (dp.source_kind === "link") {
    const shown = noRange(dp.title) || ctxOf(dp).title || dp.source_uri;
    return [siteOf(dp), " · ", long ? h("a", { href: dp.source_uri, target: "_blank", rel: "noopener" }, `${shown} ↗`) : shown, r ? ` · ${r}` : ""];
  }
  if (dp.source_kind === "capture") return [ctxOf(dp).via || "Captured", " · ", a ? `${t} by ${a}` : t, r ? ` · ${r}` : ""];
  if (dp.source_kind === "track" || dp.track_id != null) {
    return ["Your library", " · ", long && dp.track_id != null ? h("a", { href: `/#track/${dp.track_id}` }, t) : t, a ? ` by ${a}` : "", r ? ` · ${r}` : ""];
  }
  return ["Added by hand", r ? ` · ${r}` : ""];
}

// ---- the list ----

const list = { size: "", rows: [], total: 0, tracks: null, loaded: false, error: "" };

let listAsk = 0;  // the newest list request: an older answer (a filter clicked twice quickly) is dropped
async function loadList(more = false) {
  const me = ++listAsk, q = new URLSearchParams({ limit: PAGE, offset: more ? list.rows.length : 0 });
  if (list.size) q.set("size", list.size);
  try {
    const [got, tr] = await Promise.all([api(`/api/deposits?${q}`),
      list.tracks == null ? api("/api/deposits?size=track&limit=1") : null]);
    if (me !== listAsk) return;
    list.rows = more ? [...list.rows, ...got.deposits] : got.deposits;
    list.total = got.total;
    if (tr) list.tracks = tr.total;
    list.error = "";
  } catch (e) { if (me === listAsk) list.error = e.message; }
  if (me === listAsk) list.loaded = true;
}

function listView() {
  const seg = h("span", { class: "adm-seg bk-seg", role: "group", "aria-label": "Size" },
    ...[["", "All"], ["moment", "Moments"], ["track", "Tracks"], ["collection", "Collections"]].map(([v, l]) =>
      h("button", { type: "button", "aria-pressed": String(list.size === v), onclick: async () => { list.size = v; await loadList(); render(false); } }, l)));
  const n = list.total, libLine = list.size !== "moment" && list.size !== "collection" && list.tracks;
  const rows = list.rows.map((dp) => {
    const fl = foundLine(dp);
    return h("li", { class: "bk-row" },
      h("a", { class: "bk-link", href: `/bank/${dp.id}`, onclick: (e) => {
        if (e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        e.preventDefault(); go(`/bank/${dp.id}`);
      } },
        badge(dp.size),
        h("span", { class: "bk-main" },
          dp.note ? h("span", { class: "bk-note" }, dp.note) : h("span", { class: "bk-note bk-nonote" }, titleArtist(dp)),
          h("span", { class: "bk-src muted" }, ...sourceBits(dp, false))),
        h("span", { class: `bk-found ${fl.cls}` }, fl.t),
        h("span", { class: "bk-when muted", title: fmtUpLong(whenOf(dp)) }, fmtUp(whenOf(dp)))));
  });
  return h("div", {},
    h("div", { class: "bk-head" }, h("h2", {}, "Bank"),
      h("p", { class: "muted" }, "Everything you've deposited, newest first: moments you marked, tracks and whole sets. Tastr finds the audio and breaks each one down in the background.")),
    h("div", { class: "bk-tools" }, seg, h("span", { class: "muted bk-n", role: "status" }, `${n} deposit${n === 1 ? "" : "s"}`),
      libLine ? h("span", { class: "muted bk-fine" }, `Your ${list.tracks} library track${list.tracks === 1 ? " is" : "s are"} in here too, as track deposits.`) : null),
    list.error ? h("p", { class: "warn-t", role: "alert" }, `Couldn't load your bank. ${list.error}`) : null,
    h("ul", { class: "bk-list", "aria-label": "Deposits" }, ...rows),
    !list.error && !rows.length ? h("p", { class: "muted" }, list.size ? "Nothing of this size yet." : "Nothing in your bank yet.") : null,
    list.rows.length < n ? h("p", {}, h("button", { type: "button", class: "bk-more", onclick: async (e) => {
      e.target.disabled = true; e.target.textContent = "Loading…"; await loadList(true); render(false);
    } }, `Show more (${n - list.rows.length} left)`)) : null);
}

// ---- hearing it ----

function barTicks(bars) {
  const per = bars > 32 ? 8 : 1;  // a long track: one tick per 8 bars
  return h("div", { class: "bk-bars", "aria-hidden": "true" }, ...Array.from({ length: Math.ceil(bars / per) }, (_, j) => {
    const i = j * per;
    return h("span", { class: i % 4 === 0 ? "bk-bar4" : "" }, i % (per > 1 ? 16 : 4) === 0 ? String(i + 1) : "");
  }));
}
function waveOf(buf, n = 96) {  // peak bars of one stem, for its lane
  const ch = buf.getChannelData(0), step = Math.max(1, Math.floor(ch.length / n)), peaks = [];
  for (let i = 0; i < n; i++) { let m = 0; for (let j = i * step, e = Math.min(ch.length, j + step); j < e; j += 4) m = Math.max(m, Math.abs(ch[j])); peaks.push(m); }
  const top = Math.max(1e-3, ...peaks);
  return svg("svg", { viewBox: `0 0 ${n} 20`, preserveAspectRatio: "none", class: "bk-wave", "aria-hidden": "true" },
    ...peaks.map((p, i) => { const v = Math.max(0.6, (p / top) * 18); return svg("rect", { x: i + 0.15, y: 10 - v / 2, width: 0.7, height: v }); }));
}

// Transport, bar ruler and (with stems) one lane per stem with mute and solo. `src`: { mix } or { stems: {name: url} }
// for a moment's slice, played with Web Audio so the stems stay in step and the loop is seamless; { element: url } for
// a whole track, streamed by an <audio> element.
function player(f, src, note) {
  let len = isNum(f.len) ? f.len : 0;
  const head = h("i", { class: "bk-head-i" }), time = h("span", { class: "bk-time" }, `0:00 / ${mmss(len)}`);
  const loop = h("button", { type: "button", class: "bk-loop", "aria-pressed": "true", title: "Play the slice over and over" }, "⟲ Loop");
  const play = h("button", { type: "button", class: "bk-play", "aria-label": "Play" }, "▶");
  const msg = h("p", { class: "muted bk-fine", role: "status" }, note || "");
  const paint = (pos) => { head.style.left = `${len ? Math.min(100, (pos / len) * 100) : 0}%`; time.textContent = `${mmss(pos)} / ${mmss(len)}`; };
  const showPlay = (on) => { play.textContent = on ? "❚❚" : "▶"; play.setAttribute("aria-label", on ? "Pause" : "Play"); };
  const looping = () => loop.getAttribute("aria-pressed") === "true";
  const order = Object.keys(STEM_NAME), rank = (n) => (order.includes(n) ? order.indexOf(n) : order.length);
  const names = src.stems ? Object.keys(src.stems).sort((a, b) => rank(a) - rank(b)) : [];  // Vocals, Drums, Bass, Other
  const lanes = {}, gains = {};
  const laneRows = names.map((name) => {
    const mute = h("button", { type: "button", class: "lane-btn mute", "aria-pressed": "false", title: `Mute ${(STEM_NAME[name] || name).toLowerCase()}` }, "M");
    const solo = h("button", { type: "button", class: "lane-btn solo", "aria-pressed": "false", title: `Hear only ${(STEM_NAME[name] || name).toLowerCase()}` }, "S");
    const wave = h("span", { class: "bk-wave-slot" });
    lanes[name] = { mute, solo, wave };
    for (const b of [mute, solo]) b.onclick = () => { b.setAttribute("aria-pressed", String(b.getAttribute("aria-pressed") !== "true")); mix(); };
    return h("div", { class: "bk-lane" }, h("span", { class: "bk-lane-n" }, STEM_NAME[name] || cap(name)), mute, solo, wave);
  });
  function mix() {  // solo wins over mute, as in the track card's stem lanes
    const soloed = names.filter((n) => lanes[n].solo.getAttribute("aria-pressed") === "true");
    for (const n of names) {
      const on = soloed.length ? soloed.includes(n) : lanes[n].mute.getAttribute("aria-pressed") !== "true";
      if (gains[n]) gains[n].gain.value = on ? 1 : 0;
    }
  }

  let stop, toggle, close;
  if (src.element) {
    const el = new Audio();
    el.preload = "none"; el.loop = true; el.src = src.element;
    el.addEventListener("loadedmetadata", () => { if (isFinite(el.duration)) { len = el.duration; paint(el.currentTime); } });
    el.addEventListener("timeupdate", () => paint(el.currentTime));
    el.addEventListener("ended", () => { showPlay(false); paint(0); });
    el.addEventListener("error", () => { showPlay(false); msg.textContent = "Couldn't play the audio."; });
    loop.onclick = () => { loop.setAttribute("aria-pressed", String(!looping())); el.loop = looping(); };
    stop = () => { el.pause(); showPlay(false); };
    close = () => { stop(); el.removeAttribute("src"); el.load(); };
    toggle = () => { if (el.paused) { el.play().catch(() => {}); showPlay(true); } else stop(); };
  } else {
    let ac = null, bufs = null, sources = [], t0 = 0, at = 0, raf = 0, on = false;
    const load = async () => {
      ac = new (window.AudioContext || window.webkitAudioContext)();
      msg.textContent = "Loading the audio…";
      const urls = src.stems ? names.map((n) => src.stems[n]) : [src.mix];
      const got = await Promise.all(urls.map(async (u) => {
        const r = await fetch(u);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return ac.decodeAudioData(await r.arrayBuffer());
      }));
      bufs = got;
      len = got[0].duration;
      if (src.stems) names.forEach((n, i) => { lanes[n].wave.replaceChildren(waveOf(got[i])); gains[n] = ac.createGain(); gains[n].connect(ac.destination); });
      mix();
      msg.textContent = note || "";
    };
    const pos = () => (on ? ac.currentTime - t0 : at);
    const tick = () => {
      let p = pos();
      if (p >= len) { if (looping()) p %= len; else { halt(); at = 0; paint(0); return; } }
      paint(p); raf = requestAnimationFrame(tick);
    };
    const halt = () => {
      for (const s of sources) { s.onended = null; try { s.stop(); } catch (_) {} }
      sources = []; cancelAnimationFrame(raf); on = false; showPlay(false);
    };
    const start = () => {
      const from = at % len;
      sources = bufs.map((b, i) => {
        const s = ac.createBufferSource();
        s.buffer = b; s.loop = looping();
        s.connect(src.stems ? gains[names[i]] : ac.destination);
        s.start(0, from);
        return s;
      });
      sources[0].onended = () => { if (on && !looping()) { halt(); at = 0; paint(0); } };
      t0 = ac.currentTime - from; on = true; showPlay(true); raf = requestAnimationFrame(tick);
    };
    loop.onclick = () => {
      loop.setAttribute("aria-pressed", String(!looping()));
      if (on) { at = pos() % len; halt(); start(); }  // a looping source can't be told where its loop ends mid-play
    };
    stop = () => { if (on) { at = pos() % len; halt(); } };
    close = () => { halt(); if (ac) ac.close().catch(() => {}); };
    // Loaded as the page opens, so the stems' waveforms show at once; the sound starts on ▶.
    play.disabled = true;
    const loading = load().then(() => { play.disabled = false; }, (e) => { msg.textContent = `Couldn't load the audio. ${e.message}`; });
    toggle = async () => {
      if (on) return stop();
      await loading;
      if (!bufs) return;
      if (ac.state === "suspended") await ac.resume();
      start();
    };
  }
  play.onclick = () => toggle();
  const bars = isNum(f.bars) ? f.bars : isNum(f.bpm) && isNum(len) && len > 0 ? Math.max(1, Math.round((len * f.bpm) / 240)) : 0;
  return {
    stop: () => close(),  // leaving the page: stop and let the audio go
    el: h("section", { class: "bk-sec", "aria-labelledby": "bk-hear" }, h("h3", { id: "bk-hear" }, "Hear it"),
      h("div", { class: "bk-transport" }, play, loop, time),
      h("div", { class: "bk-track" }, h("div", { class: "bk-full" }, head), bars ? barTicks(bars) : null),
      laneRows.length ? h("div", { class: "bk-lanes" }, ...laneRows) : null,
      msg),
  };
}

function hearIt(dp, f) {
  if (dp.size === "moment") {
    const base = `/api/deposits/${dp.id}`;
    const src = f.stems ? { stems: Object.fromEntries(f.stems.map((n) => [n, `${base}/stems/${n}`])) } : { mix: `${base}/audio` };
    return player(f, src, f.stems ? "" : "No stems yet.");
  }
  if (dp.size === "track") {
    const url = dp.track_id != null ? `/api/tracks/${dp.track_id}/audio` : `${base(dp)}/audio`;
    return player(f, { element: url }, dp.track_id != null ? "Its stems are on its track card." : "No stems yet.");
  }
  return null;
}
const base = (dp) => `/api/deposits/${dp.id}`;

// ---- what the tooling found ----

function foundView(dp, f) {
  const row = (k, ...v) => [h("dt", {}, k), h("dd", {}, ...v)];
  const named = dp.size === "moment" && (dp.touches || []).length ? dp.touches.map((t, i) => [i ? " → " : "",
    t.track_id != null ? h("a", { href: `/#track/${t.track_id}` }, t.title || `Track ${t.track_id}`) : link(`/bank/${t.id}`, t.title || `Deposit ${t.id}`)]) : null;
  const by = ctxOf(dp).tracks_by;
  const unnamed = dp.size === "moment" && !named && dp.source_kind === "capture";
  const key = keyText(f);
  return h("section", { class: "bk-sec", "aria-labelledby": "bk-found" }, h("h3", { id: "bk-found" }, "What Tastr found"),
    h("dl", { class: "bk-dl" },
      named ? row("Tracks", ...named, by ? h("span", { class: "muted" }, ` (a guess, from ${by})`) : "") : null,
      unnamed ? row("Tracks", h("span", { class: "muted" }, "Not named yet. Tastr will try to recognize the recording.")) : null,
      isNum(f.bpm) ? row("Tempo", `${Math.round(f.bpm)} BPM`) : null,
      key ? row("Key", f.keyTo ? `${key}, moving to ${f.keyTo}${isNum(f.keyAt) ? ` at bar ${f.keyAt}` : ""}` : key) : null,
      isNum(f.bars) ? row("Length", `${f.bars} bars of ${f.meter}`) : isNum(f.len) ? row("Length", mmss(f.len)) : null,
      f.drums ? row("Drums", f.drums) : null,
      f.chords ? row("Chords", h("span", { class: "bk-chords" }, ...f.chords.map((ch, i) => h("span", { title: `Bar ${i + 1}` }, ch)))) : null,
      f.tags.length ? row("Vibes", h("span", { class: "bk-tags" }, ...f.tags.map((t) => h("span", { class: "bk-tag" }, t)))) : null));
}

// ---- one deposit ----

let pollTimer = 0;

let queued = 0;  // a deposit sent to /fetch: its job may wait behind another one before it shows as working

async function fetchAgain(dp, btn, out) {  // POST /fetch: a moment from a link, saved without fetching or failed
  btn.disabled = true; btn.textContent = "Starting…";
  try { await api(`${base(dp)}/fetch`, { method: "POST" }); queued = dp.id; render(false); }
  catch (e) { out.textContent = e.message; btn.disabled = false; btn.textContent = "Try again"; }
}

function stateView(dp) {
  if (queued === dp.id && (dp.status === "needs-source" || dp.status === "failed")) {
    return h("section", { class: "bk-sec" }, h("h3", {}, "Breaking down"),
      h("p", {}, "Waiting its turn: Tastr breaks down one moment at a time."),
      h("p", { class: "muted" }, "This runs without you. Come back later; the page updates when it's done."));
  }
  if (queued === dp.id) queued = 0;  // it started (or finished): back to the deposit's own state
  const canFetch = dp.size === "moment" && dp.source_kind === "link" && isNum(dp.start_s);
  if (dp.status === "working") {
    const at = stageAt(dp);
    return h("section", { class: "bk-sec" }, h("h3", {}, "Breaking down"),
      h("ol", { class: "bk-steps" }, ...STAGES.map(([, t], i) => h("li", { class: i < at ? "done" : i === at ? "now" : "" },
        h("span", { "aria-hidden": "true" }, i < at ? "✓" : i === at ? "✱" : "○"), " ", t))),
      h("p", { class: "muted" }, "This runs without you. Come back later; the page updates when it's done."));
  }
  if (dp.status === "needs-source") {
    const out = h("p", { class: "warn-t", role: "status" });
    if (canFetch) {
      const btn = h("button", { type: "button", onclick: () => fetchAgain(dp, btn, out) }, "Fetch it now");
      return h("section", { class: "bk-sec" }, h("h3", {}, "Needs sourcing"),
        h("p", {}, `Tastr has the link but hasn't fetched the audio yet. It fetches the whole ${siteOf(dp)} source once, then cuts this moment from it.`),
        h("p", {}, btn), out);
    }
    if (dp.size === "track" && dp.source_kind === "track") {
      return h("section", { class: "bk-sec" }, h("h3", {}, "Needs sourcing"), h("p", {}, "Its track was deleted from your library, so Tastr has no audio for it. Your note and tags are kept."));
    }
    const what = artistOf(dp) ? `${titleOf(dp)} by ${artistOf(dp)}` : titleOf(dp), r = rangeOf(dp);
    return h("section", { class: "bk-sec" }, h("h3", {}, "Needs sourcing"),
      h("p", {}, `Tastr knows it's ${what}${r ? `, ${r}` : ""}, but ${captureSite(dp)} doesn't give out the audio.`),
      h("p", { class: "muted bk-fine" }, "Adding the audio from this page comes next."));
  }
  if (dp.status === "failed") {
    const out = h("p", { class: "warn-t", role: "status" });
    const btn = canFetch ? h("button", { type: "button", onclick: () => fetchAgain(dp, btn, out) }, "Try again") : null;
    return h("section", { class: "bk-sec" }, h("h3", {}, "Couldn't get the audio"),
      h("p", { class: "warn-t" }, (dp.extracted || {}).error || "Something went wrong."),
      btn ? h("p", {}, btn) : null, out);
  }
  return null;
}

function kidRow(k) {
  const r = rangeOf(k);
  return h("li", {}, badge(k.size), " ", link(`/bank/${k.id}`, noRange(k.title) || `Deposit ${k.id}`), r ? h("span", { class: "muted" }, ` · ${r}`) : null);
}
// A whole source fetched from a link holds the moments cut from it (moments.py place).
const cutFrom = (kids) => h("section", { class: "bk-sec" }, h("h3", {}, "Moments cut from it"), h("ul", { class: "bk-kids" }, ...kids.map(kidRow)));

function depositView(dp) {
  const f = foundOf(dp), parents = dp.held_by || [], kids = dp.holds || [];
  const go2 = (x) => link(`/bank/${x.id}`, noRange(x.title) || `Deposit ${x.id}`);
  let body = stateView(dp);
  if (!body) {
    if (dp.size === "collection") {
      body = h("section", { class: "bk-sec" }, h("h3", {}, "In it"),
        kids.length ? h("ul", { class: "bk-kids" }, ...kids.map(kidRow)) : h("p", { class: "muted" }, "Nothing cut from it yet."),
        h("p", { class: "muted" }, "Tastr doesn't split a set into its tracks yet."));
    } else {
      const p = hearIt(dp, f);
      pagePlayer = p;  // stopped when the page changes
      body = [p ? p.el : null, foundView(dp, f), kids.length ? cutFrom(kids) : null];
    }
  }
  return h("article", { class: "bk-dep" },
    Object.assign(link("/bank", "← All deposits"), { className: "bk-back" }),
    h("p", { class: "bk-meta" }, badge(dp.size), h("span", { class: "muted" }, ` Deposited ${fmtUpLong(whenOf(dp))}`)),
    dp.note ? h("h2", { class: "bk-title" }, `“${dp.note}”`) : h("h2", { class: "bk-title" }, titleArtist(dp), h("span", { class: "muted bk-nonote-t" }, " No note")),
    h("p", { class: "bk-srcline" }, h("span", { class: "muted" }, "Source: "), ...sourceBits(dp, true)),
    ...parents.map((x) => h("p", { class: "bk-srcline" }, h("span", { class: "muted" }, "Cut from: "), badge(x.size), " ", go2(x))),
    dp.size === "track" && dp.track_id != null ? h("p", { class: "bk-srcline" }, h("a", { href: `/#track/${dp.track_id}` }, "Open its track card")) : null,
    body);
}
let pagePlayer = null;  // the open deposit's player

// ---- drawing the page ----

let drawn = 0;  // which render is current, so a slow fetch can't draw over a newer page
export async function render(fresh = true) {
  const me = ++drawn, root = $("bk"), id = idFromPath();
  clearTimeout(pollTimer);
  if (pagePlayer) { pagePlayer.stop(); pagePlayer = null; }
  if (id == null) {
    if (fresh || !list.loaded) await loadList();
    if (me !== drawn) return;
    document.title = "Bank · Tastr";
    root.replaceChildren(listView());
    return;
  }
  let dp = null, err = "";
  try { dp = await api(`/api/deposits/${id}`); } catch (e) { err = e.status === 404 ? "There's no such deposit in your bank." : `Couldn't load it. ${e.message}`; }
  if (me !== drawn) return;
  if (!dp) {
    document.title = "Bank · Tastr";
    root.replaceChildren(h("article", { class: "bk-dep" }, Object.assign(link("/bank", "← All deposits"), { className: "bk-back" }), h("p", { class: "warn-t", role: "alert" }, err)));
    return;
  }
  document.title = `${dp.note || titleOf(dp)} · Bank · Tastr`;
  root.replaceChildren(depositView(dp));
  list.loaded = false;  // the list reads it again on the way back, in case it changed
  if (dp.status === "working" || queued === dp.id) pollTimer = setTimeout(() => { if (me === drawn) render(false); }, POLL_MS);
}

// The look switch, as in the app (help.js setLook): data-look on <html>, remembered per browser.
function setLook(v) {
  if (!["808", "technics"].includes(v)) v = "808";
  document.documentElement.dataset.look = v;
  document.querySelectorAll(".look-pick button").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.look === v)));
  try { localStorage.setItem("tastr-look", v); } catch (_) {}
}

if (typeof document !== "undefined" && $("bk")) {
  let saved = document.documentElement.dataset.look;
  try { saved = saved || localStorage.getItem("tastr-look"); } catch (_) {}
  setLook(saved);
  document.querySelectorAll(".look-pick button").forEach((b) => b.addEventListener("click", () => setLook(b.dataset.look)));
  window.addEventListener("popstate", () => render());
  render();
}
