// Lyrics and karaoke (T87 in the design sandbox).
//
// Lyrics: GET /api/tracks/{id}/lyrics -> {state: none | found | not_found | no_tags | error, synced: [{t, line}], plain,
// instrumental, fetched_at, available}; POST looks them up on LRCLIB (again). Shown in the card's Lyrics section and the
// full-screen sing-along view, following whichever player has the track (the playlist bar, else the card's chart).
// Karaoke: one switch for the card player, the playlist bar and the sing-along view, kept per browser (tastr-karaoke).
// It plays the track's karaoke mix (its stems without the vocals): POST /api/tracks/{id}/karaoke makes it from the
// stems the first time (a few seconds), GET .../karaoke/audio plays it.

import { api, h, mmss, svg } from "./core.js";
import { charts } from "./labels.js";
import { onStems, pollStems, startStems, stemStatus, stemsBusy, stemsEta } from "./menu.js";

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
const lineAt = (L, t) => { let i = -1; for (let k = 0; k < L.synced.length; k++) { if (L.synced[k].t <= t + 0.15) i = k; else break; } return i; };
export const lyricsOf = (id) => lyrics.get(id);
export const lyricsShown = (id) => hasLyrics(lyrics.get(id));
// From the card's own answer (c.lyrics, c.lyrics_available); a fresh card knows best.
export function cardLyrics(c) {
  if (c.lyrics && lyrics.get(c.id)?.state !== "pending") lyrics.set(c.id, { ...c.lyrics, available: !!c.lyrics_available });
  return lyrics.get(c.id);
}
async function lookUp(id) {
  const was = lyrics.get(id) || {};
  lyrics.set(id, { ...was, state: "pending" }); changed();
  try { lyrics.set(id, await api(`/api/tracks/${id}/lyrics`, { method: "POST" })); }
  catch (_) { lyrics.set(id, { ...was, state: "error", available: was.available !== false }); }
  changed();
}
// For a playlist track that has no card open: ask once what's stored.
export async function loadLyrics(id) {
  if (lyrics.has(id)) return lyrics.get(id);
  try { lyrics.set(id, await api(`/api/tracks/${id}/lyrics`)); changed(); } catch (_) { /* no lyrics then */ }
  return lyrics.get(id);
}
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

// ---- the card's Lyrics section ----
export function lyricsBody(c) {
  const box = h("div", { class: "ly" });
  const credit = (L) => h("p", { class: "muted ly-credit" }, "Lyrics from ", h("a", { href: "https://lrclib.net", target: "_blank", rel: "noopener" }, "LRCLIB"),
    L && L.fetched_at ? ` · checked ${fmtDay(L.fetched_at)}` : "");
  const again = (label = "Try again") => h("button", { type: "button", class: "linklike", onclick: () => lookUp(c.id) }, label);
  let lines = null, lastIdx = -2, userScrolled = 0;
  const paint = () => {
    const L = lyrics.get(c.id); lines = null; lastIdx = -2;
    const top = h("div", { class: "ly-top" }, karaCtl(c.id, { card: c }), hasLyrics(L)
      ? h("button", { type: "button", class: "tp-btn ly-big", title: "Big lyrics for singing along, sized for a phone (Esc to close)", onclick: () => openSing(c.id, c.title || c.source?.title, c.source?.artist) }, "⤢ Sing along") : null);
    const st = L ? L.state : "none";
    let body;
    if (L && L.available === false && st !== "found") body = [h("p", { class: "ly-state muted" }, "Lyrics lookups aren't set up on this server.")];
    else if (st === "pending") body = [h("p", { class: "muted ly-state" }, h("span", { class: "spin", "aria-hidden": "true" }), " Looking for lyrics on LRCLIB…")];
    else if (st === "none") body = [h("p", { class: "ly-state" }, "Not looked up yet. ", again("Find lyrics"), h("span", { class: "muted" }, " Looks up the artist and title on LRCLIB."))];
    else if (st === "found" && L.instrumental) body = [h("p", { class: "ly-state" }, "♪ Instrumental. LRCLIB lists this track as having no vocals."), credit(L)];
    else if (synced(L)) {
      lines = h("ol", { class: "ly-lines", "aria-label": "Lyrics, timed to the track. Click a line to play from there." });
      let prevT = 0;
      L.synced.forEach((x, k) => {
        if (x.t - prevT > 12 && k) lines.append(h("li", { class: "ly-gap", "aria-hidden": "true" }, "♪"));
        lines.append(h("li", { "data-k": String(k) }, h("button", { type: "button", class: "ly-line", title: `Play from ${mmss(x.t)}`, onclick: () => lyrSeek(c.id, x.t) }, x.line || "♪")));
        prevT = x.t;
      });
      lines.addEventListener("wheel", () => (userScrolled = Date.now()), { passive: true });
      lines.addEventListener("touchmove", () => (userScrolled = Date.now()), { passive: true });
      body = [lines, h("p", { class: "muted ly-hint" }, "The line being sung lights up as the track plays. Click a line to play from there."), credit(L)];
    } else if (st === "found" && L.plain) body = [h("p", { class: "muted ly-hint" }, "These lyrics aren't timed, so they don't follow the track."), h("div", { class: "ly-plain" }, L.plain), credit(L)];
    else if (st === "not_found") body = [h("p", { class: "ly-state" }, "LRCLIB doesn't have lyrics for this track yet. ", again("Look again")), credit(L)];
    else if (st === "no_tags") body = [h("p", { class: "ly-state" }, "Can't look up lyrics: the file has no artist or title tags. ", h("span", { class: "muted" }, "Add them in Metadata, then look again."))];
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
  const key = () => { const L = lyrics.get(c.id); return `${L ? L.state : "none"}|${L ? L.fetched_at : ""}`; };
  onBus(box, () => { const k = key(); if (box.dataset.st !== k) { box.dataset.st = k; paint(); } });
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
    h("div", { class: "sing-foot" }, play, karaCtl(id, { cls: "sing-kara" }), h("span", { class: "muted sing-credit" }, "Lyrics from LRCLIB")));
  let last = "";
  const paint = () => {
    const ck = lyrClock(id), lab = ck.playing ? "❚❚" : "▶";
    if (play.textContent !== lab) { play.textContent = lab; play.setAttribute("aria-label", ck.playing ? "Pause" : "Play"); }
    if (!timed) return;
    const i = lineAt(L, ck.t), n = L.synced[i + 1], gap = !!n && (i < 0 || (n.t - ck.t > 6 && ck.t - L.synced[i].t > 6));
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
