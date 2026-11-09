// Playing and exporting saved lists (TAS-79): a player bar fixed to the bottom of the page that streams each track's
// audio in turn (in list order or shuffled), fading into the next track when one ends on its own (crossfade, set in
// the bar and kept in this browser), and the Export menu that saves a list as an M3U8, XSPF or PLS file, or a zip
// with the audio. The queue and crossfade logic is in playlist-core.js.

import { $, h, httpError, isNum, mmss, tracks } from "./core.js";
import { openTrack, switchTo } from "./card.js";
import { player, setPlayer, stopAllPlayback } from "./sequencer.js";
import { pinPop } from "./pin.js";
import { XF_MAX, XF_MIN, markPlayed, newQueue, nextStep, prevStep, queueId, readXf, toggleShuffle, xfCurve, xfLen } from "./playlist-core.js";

let pl = null; // the queue (playlist-core.js) plus msg: a line that replaces the subline for now
// Two <audio> elements that take turns, so the next track can start while the last one fades out. They stream with
// Range requests, so a long list never decodes whole files. `el` is the one playing the queue's current track.
const els = [];
let el = null;
let fade = null; // while a crossfade runs: { from: the element fading out, prev: its track's name, timer }
let ctx = null; // Web Audio, made on a click (browsers start it muted otherwise); each element plays through a gain
const gains = new Map();
const XF_KEY = "tastr-crossfade";
let xf = readXf(null);
try { xf = readXf(localStorage.getItem(XF_KEY)); } catch (_) { /* storage off */ }
const hooks = new Set();
// Told when the playing track changes or the bar closes (the Library list marks the playing track).
export const onPlaying = (fn) => hooks.add(fn);
export const playingId = () => (pl ? queueId(pl) : null);
const told = () => hooks.forEach((fn) => fn(playingId()));
const byId = (id) => tracks.find((t) => t.id === id);
const nameOf = (t) => (t && t.title) || "Untitled";

// Registered as the shared `player` while playing, so starting a card's playback (or the drum player, or a key
// preview) pauses the list, and starting the list stops them.
const me = { stop() { finishFade(); if (el && !el.paused) el.pause(); } };

function makeEl() {
  const a = new Audio();
  a.preload = "auto";
  // Hosted audio redirects to the bucket, whose CORS rule allows any origin; without this Web Audio would get silence.
  a.crossOrigin = "anonymous";
  a.addEventListener("play", () => {
    if (a !== el) return;
    if (ctx && ctx.state !== "running" && ctx.state !== "closed") ctx.resume().catch(() => {});
    if (player !== me) { stopAllPlayback(); setPlayer(me); } paint(); });
  a.addEventListener("pause", () => { if (a !== el) return; finishFade(); if (player === me) setPlayer(null); paint(); });
  a.addEventListener("timeupdate", () => { if (a === el) { paintTime(); maybeFade(); } });
  a.addEventListener("loadedmetadata", () => { if (a === el) paintTime(); });
  a.addEventListener("ended", () => {
    if (a !== el) { if (fade && fade.from === a) finishFade(); return; }
    if (pl) { markPlayed(pl); const n = nextStep(pl); if (n == null) end(); else go(n, true); }
  });
  a.addEventListener("error", () => {
    if (a !== el || !pl || !a.getAttribute("src")) return;
    finishFade();
    // Its audio isn't on the server (expired or deleted): skip it.
    const t = byId(queueId(pl)), n = nextStep(pl);
    const msg = `Skipped “${nameOf(t)}”: its audio isn't on the server`;
    if (n == null) { end(msg); return; }
    go(n, true, msg);
  });
  return a;
}
function audioEl() {
  if (!els.length) els.push(makeEl(), makeEl());
  if (!el) el = els[0];
  return el;
}

// Route both elements through Web Audio gains, when crossfade is on. Called from a click (playing a list, the play
// button, turning crossfade on) so the context starts unmuted. Without Web Audio, tracks just cut.
function ensureGraph() {
  if (ctx && ctx.state !== "running" && ctx.state !== "closed") ctx.resume().catch(() => {});
  if (!xf.on) return;
  audioEl();
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!ctx && AC) ctx = new AC();
    if (!ctx) return;
    for (const a of els) {
      if (gains.has(a)) continue;
      const g = ctx.createGain();
      ctx.createMediaElementSource(a).connect(g);
      g.connect(ctx.destination);
      gains.set(a, g);
    }
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
  } catch (_) { /* no Web Audio here: no crossfade */ }
}
const canFade = () => !!ctx && ctx.state === "running" && els.every((a) => gains.has(a));
function setGain(a, v) {
  const g = gains.get(a);
  if (!g) return;
  g.gain.cancelScheduledValues(ctx.currentTime);
  g.gain.setValueAtTime(v, ctx.currentTime);
}
function ramp(a, rising, secs) {
  const g = gains.get(a);
  g.gain.cancelScheduledValues(ctx.currentTime);
  g.gain.setValueCurveAtTime(xfCurve(rising), ctx.currentTime, secs);
}

// Near the end of a track that's playing on its own, load the next one on the spare element (so a slow server can't
// eat the fade), then start it and fade between them.
const PRELOAD = 15; // seconds before the fade
const audioUrl = (id) => `/api/tracks/${id}/audio`;
const spare = () => els.find((a) => a !== el);
function load(a, id) { if (a.getAttribute("src") !== audioUrl(id)) { a.src = audioUrl(id); a.load(); } }
function unload(a) { if (a && a.getAttribute("src")) { a.removeAttribute("src"); a.load(); } }
function maybeFade() {
  if (!pl || fade || !xf.on || el.paused || !canFade()) return;
  const n = nextStep(pl), d = el.duration;
  if (n == null || !isFinite(d)) return;
  const id = pl.ids[pl.order[n]], len = xfLen(xf.sec, d, (byId(id) || {}).duration), left = d - el.currentTime;
  if (len <= 0) return;
  if (left <= len + PRELOAD) load(spare(), id);
  if (left <= len) startFade(n, len);
}
function startFade(n, len) {
  const from = el, to = spare();
  const f = fade = { from, prev: nameOf(byId(queueId(pl))), timer: 0 };
  markPlayed(pl);
  pl.i = n; pl.msg = ""; pl.ended = false;
  el = to;
  setGain(to, 0);
  // The ramps start once the new track really plays. If it's still loading when the old one ends, it comes in at
  // full volume (finishFade), as without crossfade.
  const begin = () => {
    if (fade !== f) return;
    ramp(from, false, len); ramp(to, true, len);
    f.timer = setTimeout(finishFade, len * 1000 + 300);
  };
  to.addEventListener("playing", begin, { once: true });
  if (to.error) unload(to); // a preload that failed: load it again, so its error event fires and skips it
  load(to, queueId(pl));
  to.play().catch(() => {});
  paint(); told(); media();
}
// End a crossfade now: stop the track fading out and play the new one at full volume.
function finishFade() {
  if (!fade) return;
  const f = fade;
  fade = null;
  clearTimeout(f.timer);
  f.from.pause(); f.from.removeAttribute("src"); f.from.load();
  setGain(f.from, 1);
  setGain(el, 1);
  // Just the line under the title: rebuilding the bar would drop a drag on its seek slider.
  const sub = pl && $("pl-bar").querySelector(".pl-sub");
  if (sub) sub.textContent = subText();
}

// Start playing a list: from its first track, or a random one when shuffled.
export function playList(name, ids, { shuffle = false } = {}) {
  ids = ids.filter((id) => byId(id));
  if (!ids.length) return;
  pl = newQueue(name, ids, { shuffle });
  ensureGraph();
  go(pl.i, true);
}

function go(i, autoplay, msg = "") {
  finishFade();
  const a = audioEl();
  unload(spare());
  pl.i = i; pl.msg = msg; pl.ended = false;
  a.src = audioUrl(queueId(pl));
  if (autoplay) a.play().catch(() => paint());
  paint(); told(); media();
}
function end(msg = "") {
  finishFade();
  el.pause(); el.currentTime = 0;
  pl.ended = true; pl.msg = msg ? `${msg}. End of “${pl.name}”` : `End of “${pl.name}”`;
  paint();
}
function prev() {
  const s = prevStep(pl, el.currentTime);
  if (s === "restart") { finishFade(); el.currentTime = 0; if (el.paused) el.play().catch(() => {}); } else go(s, !el.paused);
}
function next() { const n = nextStep(pl); if (n != null) { markPlayed(pl); go(n, !el.paused); } }
function play() { ensureGraph(); return el.play().catch(() => {}); }
function toggle() {
  if (!el.paused) { el.pause(); return; }
  ensureGraph();
  if (pl.ended) { go(pl.i, true); return; }
  play();
}
export function stopList() {
  if (!pl) return;
  finishFade();
  el.pause(); el.removeAttribute("src"); el.load();
  unload(spare());
  pl = null;
  closeXf(false);
  $("pl-bar").hidden = true; document.body.classList.remove("pl-on");
  if (navigator.mediaSession) navigator.mediaSession.metadata = null;
  told();
}

// ---- the crossfade settings: a popover from the bar's XF button ----
// Two crossing ramps: one track fading out as the next fades in.
function xfIcon() {
  const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg");
  for (const [k, v] of Object.entries({ viewBox: "0 0 20 14", width: "20", height: "14", fill: "none", stroke: "currentColor",
    "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) svg.setAttribute(k, v);
  for (const d of ["M2 3 H6 L14 11 H18", "M2 11 H6 L14 3 H18"]) { const p = document.createElementNS(ns, "path"); p.setAttribute("d", d); svg.append(p); }
  return svg;
}
const xfTitle = () => (xf.on ? `Crossfade: on, ${xf.sec} s` : "Crossfade: off");
function saveXf() { try { localStorage.setItem(XF_KEY, JSON.stringify(xf)); } catch (_) { /* storage off */ } }
let xfPop = null, xfUnpin = () => {};
const xfOutside = (e) => {
  const btn = $("pl-bar").querySelector(".pl-xf");
  if (xfPop && !xfPop.contains(e.target) && !(btn && btn.contains(e.target))) closeXf(false);
};
function closeXf(refocus) {
  if (!xfPop) return;
  xfPop.remove(); xfPop = null; xfUnpin(); xfUnpin = () => {};
  document.removeEventListener("pointerdown", xfOutside, true);
  const btn = $("pl-bar").querySelector(".pl-xf");
  if (btn) { btn.setAttribute("aria-expanded", "false"); if (refocus) btn.focus(); }
}
function syncXfBtn() {
  const btn = $("pl-bar").querySelector(".pl-xf");
  if (!btn) return;
  btn.setAttribute("aria-pressed", String(xf.on)); btn.title = xfTitle(); btn.setAttribute("aria-label", xfTitle());
}
function xfMenu() {
  if (xfPop) { closeXf(true); return; }
  const btn = $("pl-bar").querySelector(".pl-xf");
  const out = h("output", { class: "pl-xf-val" }, `${xf.sec} s`);
  const on = h("input", { type: "checkbox", checked: xf.on ? "" : null });
  const len = h("input", { type: "range", class: "pl-xf-len", min: String(XF_MIN), max: String(XF_MAX), step: "1", value: String(xf.sec),
    "aria-label": "Crossfade length in seconds", disabled: xf.on ? null : "" });
  on.addEventListener("change", () => {
    xf.on = on.checked; len.disabled = !xf.on; saveXf(); syncXfBtn();
    if (xf.on) ensureGraph(); else finishFade();
  });
  len.addEventListener("input", () => { xf.sec = +len.value; out.textContent = `${xf.sec} s`; saveXf(); syncXfBtn(); });
  xfPop = h("div", { class: "pl-xf-pop menu-pop", role: "dialog", "aria-label": "Crossfade" },
    h("b", { class: "pl-xf-head" }, "Crossfade"),
    h("label", { class: "pl-xf-on" }, on, " Fade into the next track"),
    h("div", { class: "pl-xf-row" }, len, out),
    h("p", { class: "muted pl-xf-note" }, "Only when a track ends on its own. ⏭ and ⏮ still cut straight away. Kept in this browser."));
  xfPop.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); closeXf(true); } });
  document.addEventListener("pointerdown", xfOutside, true);
  document.body.append(xfPop);
  btn.setAttribute("aria-expanded", "true");
  xfUnpin = pinPop(xfPop, btn);
  on.focus();
}

// The bar: controls, the track (a link to its card) with "i of n in “Name”", a seek range, and ✕.
let seek = null, time = null;
function paintTime() {
  if (!pl || !seek) return;
  const d = isFinite(el.duration) ? el.duration : (byId(queueId(pl)) || {}).duration || 0, t = el.currentTime || 0;
  seek.max = String(d || 0); if (!seek.matches(":active")) seek.value = String(t);
  time.textContent = `${mmss(t)} / ${mmss(d)}`;
  const p = $("pl-bar").querySelector(".pl-prev");
  if (p) p.disabled = pl.i === 0 && t <= 3;
}
// The line under the title: a message for now, the crossfade, or "i of n in “List”" with the track's tempo and key.
function subText() {
  const t = byId(queueId(pl)) || {};
  return pl.msg || (fade ? `Fading in from “${fade.prev}”` : null) || [`${pl.i + 1} of ${pl.order.length} in “${pl.name}”${pl.shuffle ? " · shuffled" : ""}`,
    isNum(t.bpm) ? `${t.bpm.toFixed(0)} BPM` : null, t.camelot].filter(Boolean).join(" · ");
}
function paint() {
  if (!pl) return;
  const bar = $("pl-bar"), t = byId(queueId(pl)) || { id: queueId(pl) }, n = pl.order.length, playing = !el.paused;
  bar.hidden = false; document.body.classList.add("pl-on");
  seek = h("input", { type: "range", class: "pl-seek", min: "0", max: "0", step: "0.5", value: "0", "aria-label": "Seek in this track" });
  seek.addEventListener("input", (e) => { const v = +e.target.value; finishFade(); el.currentTime = v; paintTime(); });
  time = h("span", { class: "pl-time" });
  const b = (label, title, fn, o = {}) => h("button", { type: "button", class: "pl-btn", title, "aria-label": title, onclick: fn, ...o }, label);
  const xfBtn = b(xfIcon(), xfTitle(), xfMenu, { class: "pl-btn pl-xf", "aria-pressed": String(xf.on), "aria-haspopup": "dialog", "aria-expanded": String(!!xfPop) });
  bar.replaceChildren(
    h("div", { class: "pl-ctl" },
      b("⏮", "Previous track", prev, { class: "pl-btn pl-prev" }),
      b(playing ? "❚❚" : "▶", playing ? "Pause" : "Play", toggle, { class: "pl-btn pl-play" }),
      b("⏭", "Next track", next, { disabled: pl.i >= n - 1 ? "" : null }),
      b("⤮", pl.shuffle ? "Shuffle is on" : "Shuffle", () => { toggleShuffle(pl); pl.msg = ""; paint(); }, { class: "pl-btn pl-shuf", "aria-pressed": String(pl.shuffle) }),
      xfBtn),
    h("div", { class: "pl-now" },
      h("div", { class: "pl-title" }, h("button", { type: "button", class: "linklike pl-open", title: "Open this track's card",
        onclick: () => { switchTo("library"); openTrack(t.id); } }, nameOf(t)), t.artist ? h("span", { class: "muted" }, ` — ${t.artist}`) : null),
      h("div", { class: "pl-sub muted", role: "status" }, subText())),
    h("div", { class: "pl-pos" }, seek, time),
    b("✕", "Close the player", stopList, { class: "pl-btn pl-close" }));
  // The bar is rebuilt on every change: keep an open crossfade popover under the new button.
  if (xfPop) { xfUnpin(); xfUnpin = pinPop(xfPop, xfBtn); }
  paintTime();
}

// The keyboard's media keys and the lock screen's controls.
function media() {
  const ms = navigator.mediaSession;
  if (!ms || !pl) return;
  const t = byId(queueId(pl)) || {};
  try {
    ms.metadata = new window.MediaMetadata({ title: nameOf(t), artist: t.artist || "", album: pl.name });
    ms.setActionHandler("play", () => play());
    ms.setActionHandler("pause", () => el.pause());
    ms.setActionHandler("previoustrack", () => pl && prev());
    ms.setActionHandler("nexttrack", () => pl && next());
  } catch (_) { /* an older browser without some of these */ }
}

// ---- Export a list as a playlist file ----
export const PL_FORMATS = [
  ["m3u8", "M3U8", "Most players and DJ apps: VLC, Music, rekordbox, Serato, Traktor"],
  ["xspf", "XSPF", "XML playlist: VLC, Strawberry, Clementine"],
  ["pls", "PLS", "Older players and internet radio apps"],
  ["zip", "Zip with the audio", "Audio files + an .m3u8 that points at them, plays anywhere offline"],
];
const safeName = (name) => name.replace(/[\\/:*?"<>|]/g, "_").trim().slice(0, 60) || "Playlist";
const saveBlob = (blob, name) => {
  const href = URL.createObjectURL(blob), a = h("a", { href, download: name, hidden: "" });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 60_000);
};

// The menu, pinned to the window under `anchor`, for list `l` ({ id, name, track_ids }).
export function exportMenu(l, anchor) {
  const was = document.querySelector(".pl-exp");
  if (was) { was.remove(); if (was.dataset.for === String(l.id)) return; }
  const n = l.track_ids.length;
  const say = h("p", { class: "muted pl-exp-say", role: "status", "aria-live": "polite" });
  const pre = h("pre", { class: "st-code pl-exp-pre", hidden: "" });
  const sayIt = (text, spin = false, error = false) => {
    say.classList.toggle("error", error);
    say.replaceChildren(...[spin ? h("span", { class: "spin", "aria-hidden": "true" }) : null, text].filter(Boolean));
  };
  const get = async (fmt) => {
    pre.hidden = true;
    if (fmt === "zip") {
      // files.js imports library.js, which imports this module: load it when needed, not at startup
      const { download } = await import("./files.js");
      await download(`/api/lists/${l.id}/playlist.zip`, `${safeName(l.name)}.zip`, 0, undefined, sayIt);
      if (!say.classList.contains("error")) sayIt(`Downloaded “${safeName(l.name)}.zip”: the audio in audio/, numbered in list order, plus “${safeName(l.name)}.m3u8”. Unzip it and open the .m3u8 in any player.`);
      return;
    }
    sayIt("Making the playlist…", true);
    try {
      const r = await fetch(`/api/lists/${l.id}/playlist.${fmt}`);
      if (!r.ok) { let d = null; try { d = (await r.json()).detail; } catch (_) {} throw new Error(httpError(r.status, d, r.statusText)); }
      const text = await r.text(), file = `${safeName(l.name)}.${fmt}`, days = +r.headers.get("X-Link-Days");
      saveBlob(new Blob([text], { type: r.headers.get("Content-Type") || "text/plain" }), file);
      sayIt(`Downloaded “${file}”. ` + (days
        ? `Each entry is a link to the track's audio that works for ${days} days, without signing in. To keep the list, use the zip.`
        : "Each entry points at the track's audio on your Tastr server, so keep Tastr running. For a copy that plays anywhere, use the zip."));
      const lines = text.split("\n");
      pre.textContent = lines.slice(0, 14).join("\n") + (lines.length > 15 ? "\n…" : "");
      pre.hidden = false;
    } catch (e) { sayIt(`Couldn't export “${l.name}”: ${e.message}`, false, true); }
  };
  const pop = h("div", { class: "pl-exp menu-pop", role: "menu", "aria-label": `Export ${l.name}`, "data-for": String(l.id) },
    h("div", { class: "pl-exp-head" }, h("b", {}, `Export “${l.name}”`), h("span", { class: "muted" }, ` · ${n} track${n === 1 ? "" : "s"}`)),
    ...PL_FORMATS.map(([k, label, tip]) => h("button", { type: "button", role: "menuitem", class: "menu-item", onclick: () => get(k) },
      h("span", {}, h("b", {}, label), h("span", { class: "muted pl-exp-tip" }, tip)))),
    say, pre);
  let unpin = () => {};
  const close = (refocus) => { pop.remove(); unpin(); document.removeEventListener("pointerdown", outside, true); anchor.setAttribute("aria-expanded", "false"); if (refocus) anchor.focus(); };
  const outside = (e) => { if (!pop.contains(e.target) && !anchor.contains(e.target)) close(false); };
  pop.addEventListener("keydown", (e) => {
    const items = [...pop.querySelectorAll("[role=menuitem]")], i = items.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); close(true); }
    else if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
    else if (e.key === "Tab") close(false);
  });
  document.addEventListener("pointerdown", outside, true);
  document.body.append(pop);
  anchor.setAttribute("aria-expanded", "true");
  unpin = pinPop(pop, anchor);
  pop.querySelector("[role=menuitem]").focus();
}
