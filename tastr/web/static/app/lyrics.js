// Lyrics and karaoke (T87 in the design sandbox).
//
// Lyrics: GET /api/tracks/{id}/lyrics -> {state: none | found | not_found | no_tags | error, synced: [{t, line}], plain,
// instrumental, fetched_at, available}; POST looks them up on LRCLIB (again). Shown in the card's Lyrics section and the
// full-screen sing-along view, following whichever player has the track (the playlist bar, else the card's chart).
// Listening (T87 Whisper in the sandbox): POST /api/tracks/{id}/lyrics/listen has Whisper listen to the vocal stem,
// separating the stems first when needed. The answer's `listen` is the job ({state: none | queued | separating |
// listening | error}); once it's done the lyrics are LRCLIB's words with times (`timed_by: "whisper"`, lines it
// couldn't place carry `guess: true`) or what was heard (`source: "whisper"`).
// Your own lyrics (T92 in the sandbox): PUT /api/tracks/{id}/lyrics/edits {synced: [{t, line}]} or {plain} keeps the
// person's lines apart from LRCLIB's or Whisper's answer, so looking up or listening again never overwrites them; the
// answer then has edited, edited_at, base (lrclib | whisper | none) and newer (a different answer came in since).
// DELETE drops the edits; POST .../edits/keep keeps them over the newer answer.
// Karaoke: one switch for the card player, the playlist bar and the sing-along view, kept per browser (tastr-karaoke).
// It plays the track's karaoke mix (its stems without the vocals): POST /api/tracks/{id}/karaoke makes it from the
// stems the first time (a few seconds), GET .../karaoke/audio plays it.

import { api, h, mmss, svg } from "./core.js";
import { charts } from "./labels.js";
import { onStems, pollStems, setStems, startStems, stemStatus, stemsBusy, stemsEta } from "./menu.js";

export const karaBus = new EventTarget();
const changed = () => karaBus.dispatchEvent(new Event("change"));
const KEY = "tastr-karaoke";
let karaoke = false;
try { karaoke = localStorage.getItem(KEY) === "on"; } catch (_) { /* storage off */ }
export const karaokeOn = () => karaoke;
export function karaSet(on) {
  karaoke = on;
  try { localStorage.setItem(KEY, on ? "on" : "off"); } catch (_) { /* storage off */ }
  changed();
}

// ---- which stems a track has, and its karaoke mix ----
const stemsOf = (id) => stemStatus.get(id) || { state: "none" };
export const karaState = (id) => { const st = stemsOf(id); return st.state === "ready" ? "ready" : stemsBusy(st) ? "making" : "none"; };
const mixes = new Map(); // track id -> promise of the karaoke audio's URL, or null when the track has no stems
// The karaoke mix's URL once it's there, making it from the stems first if needed; null without stems.
export function karaokeUrl(id) {
  if (!mixes.has(id)) {
    const p = (async () => {
      let st = await api(`/api/tracks/${id}/karaoke`);
      if (st.stems !== "ready") { if (!stemStatus.has(id)) stemStatus.set(id, { state: st.stems }); return null; }
      if (st.state !== "ready") st = await api(`/api/tracks/${id}/karaoke`, { method: "POST" });
      for (let i = 0; st.state === "queued" || st.state === "running"; i++) {
        if (i > 120) throw new Error("The karaoke mix is taking too long");
        await new Promise((r) => setTimeout(r, 1000));
        st = await api(`/api/tracks/${id}/karaoke`);
      }
      if (st.state !== "ready") throw new Error(st.error || "Couldn't make the karaoke mix");
      return `/api/tracks/${id}/karaoke/audio`;
    })();
    mixes.set(id, p);
    p.catch(() => mixes.delete(id)); // ask again next time
  }
  return mixes.get(id);
}
// New stems make a new mix; forget the old answer (here and in whoever registered, e.g. the playlist bar).
const forgetHooks = new Set();
export const onForgetMix = (fn) => forgetHooks.add(fn);
export function forgetMix(id) { mixes.delete(id); forgetHooks.forEach((fn) => fn(id)); }

// ---- where the track is in playback: the playlist bar if it's playing this track, else the card's chart ----
let list = null; // playlist.js registers { id(), now(), playing(), seek(t), toggle() }
export const setListClock = (c) => { list = c; };
// Who has the track: a card player that's playing it, else the playlist bar on it (playing or paused), else the card.
function owner(id) {
  const k = charts.get(id), card = k && k.el.isConnected && k.isPlaying ? k : null;
  if (card && card.isPlaying()) return { card };
  if (list && list.id() === id) return { list };
  return { card };
}
export function lyrClock(id) {
  const o = owner(id);
  if (o.list) return { t: o.list.now(), playing: o.list.playing(), src: "pl" };
  return o.card ? { t: o.card.now(), playing: o.card.isPlaying(), src: "card" } : { t: 0, playing: false, src: null };
}
function lyrSeek(id, t) {
  const o = owner(id);
  if (o.list) { o.list.seek(t); if (!o.list.playing()) o.list.toggle(); }
  else if (o.card) o.card.seek(t, true);
}
function lyrToggle(id) {
  const o = owner(id);
  if (o.list) o.list.toggle(); else if (o.card) o.card.toggle();
}

// One animation loop for every lyrics view on the page.
const watchers = new Set();
let raf = 0;
function watch(el, fn) { watchers.add({ el, fn }); if (!raf) raf = requestAnimationFrame(tick); }
function tick() {
  for (const w of watchers) { if (!w.el.isConnected) watchers.delete(w); else w.fn(); }
  raf = watchers.size ? requestAnimationFrame(tick) : 0;
}
// Runs fn on every karaoke change while el is on the page.
function onBus(el, fn) {
  const f = () => { if (!el.isConnected && el.dataset.mounted) { karaBus.removeEventListener("change", f); return; } el.dataset.mounted = "1"; fn(); };
  karaBus.addEventListener("change", f);
}

// ---- lyrics ----
const lyrics = new Map(); // track id -> the last answer
const synced = (L) => !!(L && L.state === "found" && !L.instrumental && L.synced && L.synced.length);
const hasLyrics = (L) => !!(L && L.state === "found" && !L.instrumental && ((L.synced && L.synced.length) || L.plain));
// The line being sung: guessed lines (timed by listening, not placed) never light up.
const lineAt = (L, t) => { let i = -1; for (let k = 0; k < L.synced.length; k++) { const x = L.synced[k]; if (x.t == null || x.guess) continue; if (x.t <= t + 0.15) i = k; else break; } return i; };
const heard = (L) => !!(L && L.source === "whisper");
const LISTENING = ["queued", "separating", "listening"];
export const lyricsOf = (id) => lyrics.get(id);
export const lyricsShown = (id) => hasLyrics(lyrics.get(id));
// From the card's own answer (c.lyrics, c.lyrics_available); a fresh card knows best.
const applied = new WeakSet(); // card answers already taken in: drawing the same card again mustn't undo a newer listen
export function cardLyrics(c) {
  const was = lyrics.get(c.id);
  if (c.lyrics && !applied.has(c.lyrics) && was?.state !== "pending" && !was?.relook) {
    applied.add(c.lyrics);
    lyrics.set(c.id, { ...c.lyrics, available: !!c.lyrics_available, listen_available: !!c.listen_available,
      ...(was?.listen?.code === 429 && !LISTENING.includes(c.lyrics.listen?.state) ? { listen: was.listen } : {}) });
    if (LISTENING.includes(c.lyrics.listen?.state)) followListen(c.id);
  }
  return lyrics.get(c.id);
}
async function lookUp(id) {
  const was = lyrics.get(id) || {};
  if (heard(was)) { // heard lyrics stay unless LRCLIB has the words now
    lyrics.set(id, { ...was, relook: true }); changed();
    try {
      const got = await api(`/api/tracks/${id}/lyrics`, { method: "POST" });
      lyrics.set(id, heard(got) ? { ...got, relooked: new Date().toISOString() } : got);
    } catch (_) { lyrics.set(id, { ...was, relook: false }); }
    changed();
    return;
  }
  lyrics.set(id, { ...was, state: "pending" }); changed();
  try { lyrics.set(id, await api(`/api/tracks/${id}/lyrics`, { method: "POST" })); }
  catch (_) { lyrics.set(id, { ...was, state: "error", available: was.available !== false }); }
  changed();
}
// Listen for lyrics (or for when LRCLIB's lines are sung). One job at a time on the server; later ones say "queued".
async function listen(id) {
  const was = lyrics.get(id) || {};
  lyrics.set(id, { ...was, listen: { state: "queued" } }); changed();
  try { lyrics.set(id, { ...was, listen: await api(`/api/tracks/${id}/lyrics/listen`, { method: "POST" }) }); }
  catch (e) { lyrics.set(id, { ...was, listen: { state: "error", code: e.status, error: e.message } }); changed(); return; }
  changed();
  followListen(id);
}
const following = new Set();
async function followListen(id) {
  if (following.has(id)) return;
  following.add(id);
  try {
    for (let fails = 0; ;) {
      await new Promise((r) => setTimeout(r, 2000));
      let got;
      try { got = await api(`/api/tracks/${id}/lyrics`); fails = 0; } catch (_) { if (++fails > 5) break; continue; } // offline a moment: keep trying
      const was = lyrics.get(id) || {};
      lyrics.set(id, { ...got, listen_available: got.listen_available ?? was.listen_available });
      if (got.listen?.state === "separating" && !stemsBusy(stemStatus.get(id))) { // show the stems' progress too
        try { setStems(id, await api(`/api/tracks/${id}/stems`)); pollStems(); } catch (_) { /* next round */ }
      }
      changed();
      if (!LISTENING.includes(got.listen?.state)) break;
    }
  } finally { following.delete(id); }
}
// For a playlist track that has no card open: ask once what's stored.
export async function loadLyrics(id) {
  if (lyrics.has(id)) return lyrics.get(id);
  try { lyrics.set(id, await api(`/api/tracks/${id}/lyrics`)); changed(); } catch (_) { /* no lyrics then */ }
  return lyrics.get(id);
}
// Save, undo or keep the person's edits; the answer is the lyrics as shown. Errors go to the caller.
async function editCall(id, method, path, body) {
  const was = lyrics.get(id) || {};
  const got = await api(`/api/tracks/${id}/lyrics/edits${path}`, { method, ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}) });
  lyrics.set(id, { ...got, listen_available: got.listen_available ?? was.listen_available });
  changed();
  return got;
}
const saveEdits = (id, body) => editCall(id, "PUT", "", body);
const undoEdits = (id) => editCall(id, "DELETE", "");
const keepEdits = (id) => editCall(id, "POST", "/keep");
const mineLabel = (L) => (L.base && L.base !== "none" ? "Edited by you" : "Typed by you");
const fmtDay = (iso) => {
  const d = new Date(String(iso || "").replace(" ", "T").replace(/(T\d\d:\d\d(:\d\d)?)$/, "$1Z"));
  return isNaN(d) ? "" : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
};

// ---- the karaoke switch ----
const micIcon = () => svg("svg", { viewBox: "0 0 16 16", width: "15", height: "15", "aria-hidden": "true", fill: "none", stroke: "currentColor", "stroke-width": "1.6", "stroke-linecap": "round" },
  svg("rect", { x: "5.5", y: "1.5", width: "5", height: "8", rx: "2.5" }), svg("path", { d: "M3 7.5a5 5 0 0 0 10 0M8 12.5V15M5.5 15h5" }));
// A pressed button when the stems are ready; otherwise disabled, with why and (no stems) "Separate stems".
// o: { icon: just the mic (the playlist bar), btnCls, cls, card: the card, for starting stems }
export function karaCtl(id, o = {}) {
  const wrap = h("span", { class: "kara " + (o.cls || "") });
  const paint = () => {
    const st = karaState(id), on = karaoke && st === "ready";
    const tip = st === "ready" ? (on ? "Karaoke is on: the music without the vocals. Click to hear the full mix." : "Karaoke: play the music without the vocals, from the same spot")
      : st === "making" ? "Stems are being made. Karaoke works once they're ready." : "Karaoke needs this track's stems (vocals, bass, drums, other)";
    const btn = h("button", { type: "button", class: o.btnCls || "tp-btn kara-btn", "aria-pressed": String(on), title: tip, "aria-label": o.icon ? `Karaoke${on ? ", on" : ""}` : null,
      disabled: st === "ready" ? null : "", onclick: () => karaSet(!karaoke) }, micIcon(), o.icon ? null : ` Karaoke${on ? ": on" : ""}`);
    const sst = stemsOf(id);
    const say = o.icon ? null
      : st === "making" ? h("span", { class: "muted kara-say" }, h("span", { class: "spin", "aria-hidden": "true" }), ` Stems ${Math.round(stemsEta(sst.state === "running" ? sst : { progress: 0 }).frac * 100)}%`)
      : st === "none" && o.card ? h("button", { type: "button", class: "linklike kara-say", title: "Split this track into vocals, bass, drums and other (about 3 minutes, once). Karaoke plays everything but the vocals.",
        onclick: () => startStems(o.card).then(changed, () => {}) }, "Separate stems") : null;
    wrap.replaceChildren(...[btn, say].filter(Boolean));
  };
  onBus(wrap, paint);
  onStems(wrap, id, (st, was) => {
    const made = (x) => JSON.stringify((x && x.made_with) || null);
    if (st.state === "ready" && (!was || was.state !== "ready" || made(st) !== made(was))) forgetMix(id); // new stems, new mix
    paint(); changed();
  });
  paint();
  if (stemsBusy(stemsOf(id))) pollStems();
  return wrap;
}

// ---- the editor: timed rows (time, nudges, Set to now, text) or one textarea for plain lyrics ----
const fmtLT = (t) => (t == null ? "" : `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, "0")}`);
// "1:02.5", "62.5" or "" (no time) -> seconds or null; anything else NaN
const parseLT = (v) => { v = String(v).trim(); if (!v) return null; const m = /^(\d+):(\d{1,2}(?:\.\d*)?)$/.exec(v); if (m) return +m[1] * 60 + +m[2]; return /^\d+(\.\d*)?$/.test(v) ? +v : NaN; };
export const parseLRC = (text) => String(text).split(/\r?\n/).map((l) => /^\s*\[(\d+):(\d+(?:\.\d+)?)\](.*)$/.exec(l)).filter(Boolean)
  .map((m) => ({ t: Math.round((+m[1] * 60 + +m[2]) * 10) / 10, line: m[3].trim() }));
function lyricsEditor(c, L, close) {
  const startTimed = synced(L);
  const ed = { mode: startTimed ? "timed" : "plain", rows: startTimed ? L.synced.map((x) => ({ t: x.guess ? null : x.t, line: x.line })) : [], text: startTimed ? "" : L?.plain || "", cur: 0 };
  const snap = () => JSON.stringify([ed.mode, ed.rows, ed.text]), start = snap();
  const wrap = h("div", { class: "lye", role: "group", "aria-label": "Edit lyrics" });
  let list = null, warn = null, cancelSure = false, busy = false;
  const nowT = () => Math.round(lyrClock(c.id).t * 10) / 10;
  const outOfOrder = () => { let prev = -1, n = 0; ed.rows.forEach((r) => { if (r.t == null) return; if (r.t < prev) n++; prev = Math.max(prev, r.t); }); return n; };
  const focusRow = (k) => { const el = list && list.querySelector(`li[data-k="${k}"] .lye-text`); if (el) { el.focus({ preventScroll: true }); el.closest("li").scrollIntoView({ block: "nearest" }); } };
  const stamp = (k) => { if (!ed.rows[k]) return; ed.rows[k].t = nowT(); ed.cur = Math.min(k + 1, ed.rows.length - 1); paint(); focusRow(ed.cur); };
  function rowEl(r, k) {
    const time = h("input", { type: "text", class: "lye-time", inputmode: "decimal", value: fmtLT(r.t), placeholder: "–:––", "aria-label": `Time of line ${k + 1}`, title: "m:ss.0" });
    time.addEventListener("change", () => { const v = parseLT(time.value); if (Number.isNaN(v)) { time.classList.add("bad-in"); return; } r.t = v == null ? null : Math.round(v * 10) / 10; paintFlags(); });
    const nudge = (d, label) => h("button", { type: "button", class: "lye-b", title: `${d < 0 ? "Earlier" : "Later"} by 0.1 s`, "aria-label": `Line ${k + 1} ${label}`,
      onclick: () => { r.t = Math.max(0, Math.round(((r.t ?? nowT()) + d) * 10) / 10); time.value = fmtLT(r.t); paintFlags(); } }, d < 0 ? "−.1" : "+.1");
    const text = h("input", { type: "text", class: "lye-text", value: r.line, placeholder: "(blank line = a ♪ break)", "aria-label": `Line ${k + 1}`, maxlength: "300" });
    text.addEventListener("input", () => { r.line = text.value; });
    text.addEventListener("focus", () => { ed.cur = k; list.querySelectorAll("li.cur").forEach((x) => x.classList.remove("cur")); text.closest("li").classList.add("cur"); });
    return h("li", { "data-k": String(k), class: k === ed.cur ? "cur" : "" },
      h("span", { class: "lye-tools" }, time, nudge(-0.1, "earlier"), nudge(0.1, "later"),
        h("button", { type: "button", class: "lye-b lye-stamp", title: "Set this line's time to where the track is playing (Ctrl+Enter, ⌘+Enter on a Mac)", onclick: () => stamp(k) },
          h("span", { class: "lye-long" }, "Set to now"), h("span", { class: "lye-short", "aria-hidden": "true" }, "Now"))),
      text,
      h("span", { class: "lye-rowacts" },
        h("button", { type: "button", class: "lye-b", title: "Add a line after this one (a missing repeat, say)", "aria-label": `Add a line after line ${k + 1}`, onclick: () => { ed.rows.splice(k + 1, 0, { t: null, line: "" }); ed.cur = k + 1; paint(); focusRow(k + 1); } }, "＋"),
        h("button", { type: "button", class: "lye-b", title: "Remove this line", "aria-label": `Remove line ${k + 1}`, onclick: () => { ed.rows.splice(k, 1); ed.cur = Math.max(0, Math.min(ed.cur, ed.rows.length - 1)); paint(); } }, "×")));
  }
  function paintFlags() { // lines earlier than the line above in the warn colour, with a note
    if (!list) return;
    let prev = -1;
    list.querySelectorAll("li[data-k]").forEach((li) => {
      const r = ed.rows[+li.dataset.k], bad = r.t != null && r.t < prev;
      if (r.t != null) prev = Math.max(prev, r.t);
      li.classList.toggle("bad", bad); li.querySelector(".lye-time").classList.remove("bad-in");
    });
    const n = outOfOrder();
    warn.hidden = !n;
    warn.textContent = n ? `${n} line${n === 1 ? " is" : "s are"} earlier than the line above. Saving puts them in time order.` : "";
  }
  const lrcBox = () => { // Paste LRC: [mm:ss.xx] line
    const ta = h("textarea", { class: "lye-lrc", rows: "5", placeholder: "[00:12.40] First line\n[00:16.10] Second line", "aria-label": "LRC text" });
    const say = h("span", { class: "muted" });
    return h("div", { class: "lye-paste" }, ta, h("div", { class: "lye-acts" },
      h("button", { type: "button", onclick: () => {
        const rows = parseLRC(ta.value);
        if (!rows.length) { say.textContent = "No [mm:ss.xx] lines found."; return; }
        ed.mode = "timed"; ed.rows = rows; ed.cur = 0; paint();
      } }, "Use these lines"), h("button", { type: "button", class: "linklike", onclick: () => paint() }, "Cancel"), say));
  };
  const says = h("span", { class: "muted lye-say", role: "status" });
  async function save() {
    if (busy) return;
    const body = ed.mode === "timed" ? { synced: ed.rows.map((r) => ({ t: r.t, line: r.line })) } : { plain: ed.text };
    busy = true; says.classList.remove("warn-t"); says.textContent = "Saving…";
    try { await saveEdits(c.id, body); close(); }
    catch (e) { says.classList.add("warn-t"); says.textContent = `Couldn't save: ${e.message}`; }
    finally { busy = false; }
  }
  function paint() {
    cancelSure = false;
    const kids = [];
    if (ed.mode === "timed") {
      kids.push(h("p", { class: "muted lye-hint" }, "Play the track and press ", h("b", {}, "Set to now"), " as each line starts, or ", h("kbd", {}, "Ctrl"), "+", h("kbd", {}, "Enter"), " (", h("kbd", {}, "⌘"), "+", h("kbd", {}, "Enter"),
        " on a Mac) to stamp the line you're on and move to the next. The line being sung still lights up, so you can check."));
      list = h("ol", { class: "lye-rows" }, ...ed.rows.map(rowEl));
      warn = h("p", { class: "warn-t lye-warn", role: "status", hidden: "" });
      kids.push(list, warn, h("p", { class: "lye-more" },
        h("button", { type: "button", class: "linklike", onclick: () => { ed.rows.push({ t: null, line: "" }); ed.cur = ed.rows.length - 1; paint(); focusRow(ed.cur); } }, "＋ Add line"),
        h("button", { type: "button", class: "linklike", onclick: (e) => e.currentTarget.closest(".lye-more").after(lrcBox()) }, "Paste LRC")));
    } else {
      list = null;
      const ta = h("textarea", { class: "lye-plain", rows: "12", "aria-label": "Lyrics" });
      ta.value = ed.text;
      ta.addEventListener("input", () => { ed.text = ta.value; });
      kids.push(ta, h("p", { class: "muted lye-hint" }, "One line per line; a blank line between verses. After saving you can time them with ", h("b", {}, "Time these lines"), ", or ",
        h("button", { type: "button", class: "linklike", onclick: () => {
          ed.rows = ed.text.split("\n").map((line) => ({ t: null, line: line.trim() })).filter((r, k, a) => r.line || (k && a[k - 1].line));
          ed.mode = "timed"; ed.cur = 0; paint(); focusRow(0);
        } }, "add times by hand"), ". ", h("button", { type: "button", class: "linklike", onclick: (e) => e.currentTarget.closest("p").after(lrcBox()) }, "Paste LRC")));
      setTimeout(() => ta.isConnected && ta.focus({ preventScroll: true }));
    }
    const cancel = h("button", { type: "button", onclick: () => {
      if (snap() === start || cancelSure) return close();
      cancelSure = true; cancel.textContent = "Discard your changes? Click again";
      setTimeout(() => { if (cancel.isConnected) { cancelSure = false; cancel.textContent = "Cancel"; } }, 4000);
    } }, "Cancel");
    says.textContent = L?.edited ? "Saves over your earlier edits." : "Your edits are yours: looking up or listening again won't overwrite them.";
    says.classList.remove("warn-t");
    wrap.replaceChildren(...kids, h("div", { class: "lye-acts lye-foot" }, h("button", { type: "button", class: "primary", onclick: save }, "Save"), cancel, says));
    paintFlags();
  }
  wrap.addEventListener("keydown", (e) => { if (ed.mode === "timed" && e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); stamp(ed.cur); } });
  watch(wrap, () => { // the line being sung lights up in the editor too
    if (!list) return;
    const t = lyrClock(c.id).t;
    let i = -1;
    ed.rows.forEach((r, k) => { if (r.t != null && r.t <= t + 0.15 && r.line) i = k; });
    if (wrap.dataset.now !== String(i)) { wrap.dataset.now = String(i); list.querySelectorAll("li[data-k]").forEach((li) => li.classList.toggle("now", +li.dataset.k === i)); }
  });
  paint();
  return wrap;
}

// ---- the card's Lyrics section ----
export function lyricsBody(c) {
  const box = h("div", { class: "ly" });
  let editing = false;
  const editBtn = (label) => h("button", { type: "button", class: "tp-btn ly-edit", title: label === "Type lyrics" ? "Type or paste this track's lyrics yourself" : "Fix words, add a missing line, or fix when lines light up",
    onclick: () => { editing = true; paint(); } }, "✎ " + label);
  const failed = (p, e) => p.append(h("span", { class: "warn-t" }, ` Couldn't do that: ${e.message}`));
  // "Edited by you · based on LRCLIB · date. Undo my edits", the undo asking first
  const editedCredit = (L) => {
    const p = h("p", { class: "muted ly-credit ly-mine" });
    const from = L.base === "whisper" ? "what Whisper heard" : L.base === "lrclib" ? "LRCLIB" : null;
    const fill = (sure) => p.replaceChildren(h("b", { class: "mine-tag" }, mineLabel(L)), from ? ` · based on ${from}` : "", L.timed_by === "whisper" ? " · timed by listening" : "",
      L.edited_at ? ` · ${fmtDay(L.edited_at)}. ` : ". ",
      sure ? h("span", { class: "ly-sure" }, `Drop your edits and go back to ${L.base === "whisper" ? "what Whisper heard" : L.base === "lrclib" ? "LRCLIB's lyrics" : "no lyrics"}? This can't be undone. `,
        h("button", { type: "button", class: "linklike danger-link", onclick: () => undoEdits(c.id).catch((e) => failed(p, e)) }, "Yes, undo my edits"), " ",
        h("button", { type: "button", class: "linklike", onclick: () => fill(false) }, "Keep them"))
        : h("button", { type: "button", class: "linklike", onclick: () => fill(true) }, "Undo my edits"));
    fill(false);
    return p;
  };
  // A newer LRCLIB or Whisper answer under the edits: show it (dropping the edits, asked first) or keep mine
  const newerNote = (L) => {
    if (!L.edited || !L.newer) return null;
    const p = h("p", { class: "ly-newer", role: "status" }), who = L.source === "whisper" ? "Listening found" : "LRCLIB has";
    const fill = (sure) => p.replaceChildren(h("span", { class: "muted" }, sure ? `Show the new lyrics? Your edits will be dropped. ` : `${who} new lyrics for this track${L.fetched_at ? ` (${fmtDay(L.fetched_at)})` : ""}. `),
      sure ? h("button", { type: "button", class: "linklike danger-link", onclick: () => undoEdits(c.id).catch((e) => failed(p, e)) }, "Yes, show them")
        : h("button", { type: "button", class: "linklike", onclick: () => fill(true) }, "Show them"), " ",
      h("button", { type: "button", class: "linklike", onclick: () => (sure ? fill(false) : keepEdits(c.id).catch((e) => failed(p, e))) }, sure ? "Cancel" : "Keep mine"));
    fill(false);
    return p;
  };
  const credit = (L) => L && L.edited ? editedCredit(L) : heard(L)
    ? h("p", { class: "muted ly-credit ly-heard" }, h("b", {}, "Heard by Whisper from the vocals. Words may be wrong."), " ",
      L.relook ? h("span", {}, h("span", { class: "spin", "aria-hidden": "true" }), " Looking on LRCLIB…")
        : [L.relooked ? `Still not on LRCLIB (checked ${fmtDay(L.relooked)}). ` : "", h("button", { type: "button", class: "linklike", onclick: () => lookUp(c.id) }, "Look on LRCLIB again")])
    : h("p", { class: "muted ly-credit" }, "Lyrics from ", h("a", { href: "https://lrclib.net", target: "_blank", rel: "noopener" }, "LRCLIB"),
      L && L.fetched_at ? ` · checked ${fmtDay(L.fetched_at)}` : "", L && L.timed_by === "whisper" ? ". Timed by listening to the vocals." : "");
  // The listen job's own line (working, error), or the action that starts it. kind: "hear" (no lyrics) or "time".
  const listenLine = (L, kind) => {
    const ls = L && L.listen && L.listen.state, what = kind === "time" ? "when each line is sung" : "lyrics";
    if (ls === "separating") {
      const sst = stemsOf(c.id), pct = sst.state === "running" ? ` ${Math.round(stemsEta(sst).frac * 100)}%` : "";
      return h("p", { class: "muted ly-state ly-work" }, h("span", { class: "spin", "aria-hidden": "true" }), ` Separating stems${pct}… then listening for ${what}.`);
    }
    if (ls === "queued") return h("p", { class: "muted ly-state ly-work" }, h("span", { class: "spin idle", "aria-hidden": "true" }), " Waiting for another listen to finish…");
    if (ls === "listening") return h("p", { class: "muted ly-state ly-work" }, h("span", { class: "spin", "aria-hidden": "true" }), ` Listening for ${what}…`);
    if (ls === "error") return L.listen.code === 429 ? h("p", { class: "ly-state" }, h("span", { class: "warn-t" }, L.listen.error))
      : h("p", { class: "ly-state" }, h("span", { class: "warn-t" }, "Couldn't listen to this track. "), h("button", { type: "button", class: "linklike", title: L.listen.error || null, onclick: () => listen(c.id) }, "Try again"));
    if (!L || !L.listen_available) return null;
    const noStems = karaState(c.id) !== "ready", stemsNote = noStems ? " Separates the stems first (about 3 minutes)." : "";
    return kind === "time"
      ? h("button", { type: "button", class: "linklike", title: "Listens to the vocals to find when each line is sung" + (noStems ? "." + stemsNote : ""), onclick: () => listen(c.id) }, "Time these lines")
      : h("p", { class: "ly-listen" }, h("button", { type: "button", class: "tp-btn", onclick: () => listen(c.id) }, "Listen for lyrics"),
        h("span", { class: "muted" }, "Whisper listens to the vocals and writes down what it hears. It gets clear singing mostly right and misses words in busy or repeated parts." + stemsNote));
  };
  const again = (label = "Try again") => h("button", { type: "button", class: "linklike", onclick: () => lookUp(c.id) }, label);
  let lines = null, lastIdx = -2, userScrolled = 0;
  const paint = () => {
    const L = lyrics.get(c.id); lines = null; lastIdx = -2;
    if (editing) { box.replaceChildren(h("div", { class: "ly-top" }, karaCtl(c.id, { card: c })), lyricsEditor(c, L, () => { editing = false; box.dataset.st = key(); paint(); })); return; }
    const st = L ? L.state : "none";
    const canType = L && (st === "not_found" || st === "no_tags" || (L.available === false && st !== "found"));
    const top = h("div", { class: "ly-top" }, karaCtl(c.id, { card: c }), hasLyrics(L)
      ? h("button", { type: "button", class: "tp-btn ly-big", title: "Big lyrics for singing along, sized for a phone (Esc to close)", onclick: () => openSing(c.id, c.title || c.source?.title, c.source?.artist) }, "⤢ Sing along") : null,
      hasLyrics(L) ? editBtn("Edit lyrics") : canType ? editBtn("Type lyrics") : null);
    let body;
    if (L && L.available === false && st !== "found" && !(st === "not_found" || st === "no_tags")) body = [h("p", { class: "ly-state muted" }, "Lyrics lookups aren't set up on this server."), listenLine(L, "hear")].filter(Boolean); // listening works without LRCLIB
    else if (st === "pending") body = [h("p", { class: "muted ly-state" }, h("span", { class: "spin", "aria-hidden": "true" }), " Looking for lyrics on LRCLIB…")];
    else if (st === "none") body = [h("p", { class: "ly-state" }, "Not looked up yet. ", again("Find lyrics"), h("span", { class: "muted" }, " Looks up the artist and title on LRCLIB."))];
    else if (st === "found" && L.instrumental) body = [h("p", { class: "ly-state" }, "♪ Instrumental. LRCLIB lists this track as having no vocals."), credit(L)];
    else if (synced(L)) {
      lines = h("ol", { class: "ly-lines", "aria-label": "Lyrics, timed to the track. Click a line to play from there." });
      let prevT = 0;
      // ♪ only for a real break: more than 12 s, and more than 2.2x the usual gap (heard lines come evenly spaced)
      const steps = L.synced.filter((x) => x.t != null && !x.guess).map((x) => x.t).map((t, k, a) => (k ? t - a[k - 1] : 0)).slice(1).sort((a, b) => a - b);
      const gapMin = Math.max(12, 2.2 * (steps[steps.length >> 1] || 0));
      L.synced.forEach((x, k) => {
        if (!x.line) { lines.append(h("li", { class: "ly-gap", "aria-hidden": "true" }, "♪")); return; } // a blank line is a break
        if (x.guess || x.t == null) { // timed by listening, but this line couldn't be placed: never lights up
          lines.append(h("li", { "data-k": String(k), class: "untimed" }, h("span", { class: "ly-line", title: "Not timed: listening couldn't place this line" }, x.line)));
          return;
        }
        if (x.t - prevT > gapMin && k) lines.append(h("li", { class: "ly-gap", "aria-hidden": "true" }, "♪"));
        lines.append(h("li", { "data-k": String(k) }, h("button", { type: "button", class: "ly-line", title: `Play from ${mmss(x.t)}`, onclick: () => lyrSeek(c.id, x.t) }, x.line)));
        prevT = x.t;
      });
      lines.addEventListener("wheel", () => (userScrolled = Date.now()), { passive: true });
      lines.addEventListener("touchmove", () => (userScrolled = Date.now()), { passive: true });
      if (L.edited) lines.classList.add("ly-edited");
      body = [newerNote(L), lines, h("p", { class: "muted ly-hint" }, "The line being sung lights up as the track plays. Click a line to play from there."), credit(L)].filter(Boolean);
    } else if (st === "found" && L.plain) {
      const busy = LISTENING.includes(L.listen?.state) || L.listen?.state === "error";
      body = [newerNote(L), h("p", { class: "muted ly-hint" }, "These lyrics aren't timed, so they don't follow the track. ", busy ? null : listenLine(L, "time")),
        busy ? listenLine(L, "time") : null, h("div", { class: "ly-plain" + (L.edited ? " ly-edited" : "") }, L.plain), credit(L)].filter(Boolean);
    } else if (st === "not_found") body = [h("p", { class: "ly-state" }, "LRCLIB doesn't have lyrics for this track yet. ", L.available === false ? null : again("Look again")), listenLine(L, "hear"), credit(L)].filter(Boolean);
    else if (st === "no_tags") body = [h("p", { class: "ly-state" }, "Can't look up lyrics: the file has no artist or title tags. ", h("span", { class: "muted" }, "Add them in Metadata, then look again.")), listenLine(L, "hear")].filter(Boolean);
    else body = [h("p", { class: "ly-state" }, h("span", { class: "warn-t" }, "Couldn't reach LRCLIB. "), again())];
    box.replaceChildren(top, ...body);
  };
  const follow = () => {
    if (!lines) return;
    const L = lyrics.get(c.id), ck = lyrClock(c.id), i = synced(L) ? lineAt(L, ck.t) : -1;
    if (i === lastIdx) return;
    lastIdx = i;
    lines.querySelectorAll("li[data-k]").forEach((li) => { const k = +li.dataset.k; li.classList.toggle("now", k === i); li.classList.toggle("past", k < i); });
    const cur = lines.querySelector("li.now");
    // keep it in view inside the list, never scroll the page; leave it alone for 3 s after the reader scrolls it
    if (cur && ck.playing && Date.now() - userScrolled > 3000) {
      const at = cur.getBoundingClientRect().top - lines.getBoundingClientRect().top + lines.scrollTop; // the line's place inside the list
      lines.scrollTo({ top: at - lines.clientHeight / 2 + cur.offsetHeight / 2, behavior: "smooth" });
    }
  };
  const key = () => { const L = lyrics.get(c.id); return L ? [L.state, L.fetched_at, L.source, L.timed_by, L.listen?.state, L.relook, L.relooked, L.edited_at, L.newer].join("|") : "none"; };
  onBus(box, () => { if (editing) return; const k = key(); if (box.dataset.st !== k || /\|separating\|/.test(k)) { box.dataset.st = k; paint(); } }); // separating: the stems' % moves
  cardLyrics(c);
  paint(); box.dataset.st = key();
  watch(box, follow);
  return box;
}

// ---- sing-along: the whole screen, the current line big and the next one under it ----
export function openSing(id, title, artist) {
  document.querySelector(".sing")?.remove();
  const L = lyrics.get(id);
  if (!hasLyrics(L)) return;
  const back = document.activeElement;
  const cur = h("p", { class: "sing-now", "aria-live": "polite" }), nxt = h("p", { class: "sing-next" }), prev = h("p", { class: "sing-prev" }), when = h("p", { class: "sing-when muted" });
  const play = h("button", { type: "button", class: "sing-play", onclick: () => lyrToggle(id) });
  const close = () => { view.remove(); document.body.classList.remove("sing-open"); document.removeEventListener("keydown", key, true); back?.focus?.(); };
  const key = (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
    else if (e.key === " " && e.target.tagName !== "BUTTON") { e.preventDefault(); e.stopPropagation(); lyrToggle(id); }
  };
  const timed = synced(L);
  const stage = timed ? h("div", { class: "sing-stage" }, prev, cur, nxt, when)
    : h("div", { class: "sing-stage sing-plain" }, h("p", { class: "muted sing-note" }, "These lyrics aren't timed, so they don't follow the track."), h("div", { class: "sing-text" }, L.plain));
  const view = h("div", { class: "sing", role: "dialog", "aria-modal": "true", "aria-label": `Sing along: ${title || "this track"}` },
    h("div", { class: "sing-head" }, h("div", { class: "sing-title" }, h("b", {}, title || "Untitled"), artist ? h("span", { class: "muted" }, ` — ${artist}`) : null),
      h("button", { type: "button", class: "sing-x", title: "Close (Esc)", "aria-label": "Close sing-along", onclick: close }, "✕")),
    stage,
    h("div", { class: "sing-foot" }, play, karaCtl(id, { cls: "sing-kara" }), h("span", { class: "muted sing-credit" },
      L.edited ? mineLabel(L) : heard(L) ? "Heard by Whisper from the vocals. Words may be wrong." : L.timed_by === "whisper" ? "Lyrics from LRCLIB · timed by listening" : "Lyrics from LRCLIB")));
  let last = "";
  const paint = () => {
    const ck = lyrClock(id), lab = ck.playing ? "❚❚" : "▶";
    if (play.textContent !== lab) { play.textContent = lab; play.setAttribute("aria-label", ck.playing ? "Pause" : "Play"); }
    if (!timed) return;
    // the next line that will light up: guessed lines (never lit) are skipped by the preview and the countdown
    const i = lineAt(L, ck.t), n = L.synced.slice(i + 1).find((x) => x.t != null && !x.guess);
    const gap = !!n && (i < 0 || (n.t - ck.t > 6 && ck.t - L.synced[i].t > 6));
    const k = `${i}|${gap ? Math.ceil(n.t - ck.t) : ""}`;
    if (k === last) return;
    last = k;
    prev.textContent = i > 0 && !gap ? L.synced[i - 1].line : "";
    cur.textContent = gap || i < 0 || !L.synced[i].line ? "♪" : L.synced[i].line;
    nxt.textContent = n ? n.line : "";
    when.textContent = gap ? `Next line in ${Math.ceil(n.t - ck.t)} s` : !n && i >= 0 ? "Last line" : "";
    cur.classList.toggle("rest", cur.textContent === "♪");
  };
  document.body.append(view); document.body.classList.add("sing-open"); document.addEventListener("keydown", key, true);
  watch(view, paint); paint(); play.focus();
}
