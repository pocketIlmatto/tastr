// Playing and exporting saved lists (TAS-79): a player bar fixed to the bottom of the page that streams each track's
// audio in turn (in list order or shuffled), and the Export menu that saves a list as an M3U8, XSPF or PLS file, or a
// zip with the audio. The queue logic is in playlist-core.js.

import { $, h, httpError, isNum, mmss, tracks } from "./core.js";
import { openTrack, switchTo } from "./card.js";
import { player, setPlayer, stopAllPlayback } from "./sequencer.js";
import { pinPop } from "./pin.js";
import { markPlayed, newQueue, nextStep, prevStep, queueId, toggleShuffle } from "./playlist-core.js";

let pl = null; // the queue (playlist-core.js) plus msg: a line that replaces the subline for now
let el = null; // one shared <audio>: it streams with Range requests, so a long list never decodes whole files
const hooks = new Set();
// Told when the playing track changes or the bar closes (the Library list marks the playing track).
export const onPlaying = (fn) => hooks.add(fn);
export const playingId = () => (pl ? queueId(pl) : null);
const told = () => hooks.forEach((fn) => fn(playingId()));
const byId = (id) => tracks.find((t) => t.id === id);
const nameOf = (t) => (t && t.title) || "Untitled";

// Registered as the shared `player` while playing, so starting a card's playback (or the drum player, or a key
// preview) pauses the list, and starting the list stops them.
const me = { stop() { if (el && !el.paused) el.pause(); } };

function audioEl() {
  if (el) return el;
  el = new Audio();
  el.preload = "auto";
  el.addEventListener("play", () => { if (player !== me) { stopAllPlayback(); setPlayer(me); } paint(); });
  el.addEventListener("pause", () => { if (player === me) setPlayer(null); paint(); });
  el.addEventListener("timeupdate", paintTime);
  el.addEventListener("loadedmetadata", paintTime);
  el.addEventListener("ended", () => { if (pl) { markPlayed(pl); const n = nextStep(pl); if (n == null) end(); else go(n, true); } });
  el.addEventListener("error", () => {
    if (!pl || !el.getAttribute("src")) return;
    // Its audio isn't on the server (expired or deleted): skip it.
    const t = byId(queueId(pl)), n = nextStep(pl);
    const msg = `Skipped “${nameOf(t)}”: its audio isn't on the server`;
    if (n == null) { end(msg); return; }
    go(n, true, msg);
  });
  return el;
}

// Start playing a list: from its first track, or a random one when shuffled.
export function playList(name, ids, { shuffle = false } = {}) {
  ids = ids.filter((id) => byId(id));
  if (!ids.length) return;
  pl = newQueue(name, ids, { shuffle });
  go(pl.i, true);
}

function go(i, autoplay, msg = "") {
  const a = audioEl();
  pl.i = i; pl.msg = msg; pl.ended = false;
  a.src = `/api/tracks/${queueId(pl)}/audio`;
  if (autoplay) a.play().catch(() => paint());
  paint(); told(); media();
}
function end(msg = "") {
  el.pause(); el.currentTime = 0;
  pl.ended = true; pl.msg = msg ? `${msg}. End of “${pl.name}”` : `End of “${pl.name}”`;
  paint();
}
function prev() {
  const s = prevStep(pl, el.currentTime);
  if (s === "restart") { el.currentTime = 0; if (el.paused) el.play().catch(() => {}); } else go(s, !el.paused);
}
function next() { const n = nextStep(pl); if (n != null) { markPlayed(pl); go(n, !el.paused); } }
function toggle() {
  if (!el.paused) { el.pause(); return; }
  if (pl.ended) { go(pl.i, true); return; }
  el.play().catch(() => {});
}
export function stopList() {
  if (!pl) return;
  el.pause(); el.removeAttribute("src"); el.load();
  pl = null;
  $("pl-bar").hidden = true; document.body.classList.remove("pl-on");
  if (navigator.mediaSession) navigator.mediaSession.metadata = null;
  told();
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
function paint() {
  if (!pl) return;
  const bar = $("pl-bar"), t = byId(queueId(pl)) || { id: queueId(pl) }, n = pl.order.length, playing = !el.paused;
  bar.hidden = false; document.body.classList.add("pl-on");
  seek = h("input", { type: "range", class: "pl-seek", min: "0", max: "0", step: "0.5", value: "0", "aria-label": "Seek in this track" });
  seek.addEventListener("input", () => { el.currentTime = +seek.value; paintTime(); });
  time = h("span", { class: "pl-time" });
  const b = (label, title, fn, o = {}) => h("button", { type: "button", class: "pl-btn", title, "aria-label": title, onclick: fn, ...o }, label);
  const sub = pl.msg || [`${pl.i + 1} of ${n} in “${pl.name}”${pl.shuffle ? " · shuffled" : ""}`, isNum(t.bpm) ? `${t.bpm.toFixed(0)} BPM` : null, t.camelot].filter(Boolean).join(" · ");
  bar.replaceChildren(
    h("div", { class: "pl-ctl" },
      b("⏮", "Previous track", prev, { class: "pl-btn pl-prev" }),
      b(playing ? "❚❚" : "▶", playing ? "Pause" : "Play", toggle, { class: "pl-btn pl-play" }),
      b("⏭", "Next track", next, { disabled: pl.i >= n - 1 ? "" : null }),
      b("⤮", pl.shuffle ? "Shuffle is on" : "Shuffle", () => { toggleShuffle(pl); pl.msg = ""; paint(); }, { class: "pl-btn pl-shuf", "aria-pressed": String(pl.shuffle) })),
    h("div", { class: "pl-now" },
      h("div", { class: "pl-title" }, h("button", { type: "button", class: "linklike pl-open", title: "Open this track's card",
        onclick: () => { switchTo("library"); openTrack(t.id); } }, nameOf(t)), t.artist ? h("span", { class: "muted" }, ` — ${t.artist}`) : null),
      h("div", { class: "pl-sub muted", role: "status" }, sub)),
    h("div", { class: "pl-pos" }, seek, time),
    b("✕", "Close the player", stopList, { class: "pl-btn pl-close" }));
  paintTime();
}

// The keyboard's media keys and the lock screen's controls.
function media() {
  const ms = navigator.mediaSession;
  if (!ms || !pl) return;
  const t = byId(queueId(pl)) || {};
  try {
    ms.metadata = new window.MediaMetadata({ title: nameOf(t), artist: t.artist || "", album: pl.name });
    ms.setActionHandler("play", () => el.play().catch(() => {}));
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
