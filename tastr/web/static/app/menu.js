// The track menu, with on-demand drum model re-analysis and stem separation.

import { $, api, currentId, fmtLeft, h, isNum, jobTitle, mmss, setCurrentId } from "./core.js";
import { loadTracks, refresh, renderList } from "./library.js";
import { inReview, setInReview } from "./labels.js";
import { beatsPerBar, renderCard } from "./card.js";
import { keyName } from "./daw.js";
import { setViewsCache } from "./audio.js";
import { dropEdits, flushEdits, loadEdits } from "./groove-edits.js";

function trackSummary(c) {
  const core = c.core || {}, k = core.key, src = c.source || {};
  const facts = [isNum(core.bpm) ? `${core.bpm.toFixed(1)} BPM` : null, beatsPerBar(c) === 3 ? "3/4" : null, k ? `${keyName(k)}${k.camelot ? ` (${k.camelot})` : ""}` : null, mmss(c.duration)];
  const vibes = [...(c.tags || [])].sort((a, b) => b.score - a.score).map((t) => t.tag);
  return [`${src.title || src.uri || `Track ${c.id}`}${src.artist ? " by " + src.artist : ""}`, facts.filter(Boolean).join(" · "), vibes.length ? `Vibe: ${vibes.join(", ")}` : null].filter(Boolean).join("\n");
}
// A status the menu shows once its card is drawn again (reloading metadata re-renders the card).
const drumsBusy = (st) => st.state === "running" || st.state === "queued";
const drumsLabel = (st) => st.state === "queued" ? "Waiting for another drum model run…" : "Detecting drums with the model…";
// Starts the run (or joins one already going), waits for it and resolves to the track's fresh card.
export async function runDrumModel(id, say) {
  let st = await api(`/api/tracks/${id}/drum-model`, { method: "POST" });
  while (drumsBusy(st)) {
    if (say) say(drumsLabel(st));
    await new Promise((r) => setTimeout(r, 3000));
    st = await api(`/api/tracks/${id}/drum-model`);
  }
  if (st.state !== "done") throw new Error(st.error || "The drum model run failed");
  return api(`/api/tracks/${id}`);
}

export const stemStatus = new Map(); // track id -> latest {state: none|queued|running|ready|error, progress?, error?}
const stemListeners = new Set();
let stemTimer = null;
export function stemsOf(c) {
  if (!stemStatus.has(c.id)) { stemStatus.set(c.id, { state: c.stems_state || "none" }); pollStems(); }
  return stemStatus.get(c.id);
}
export const stemsBusy = (st) => st.state === "running" || st.state === "queued";
const STEM_TYPICAL = 180; // seconds for one track's separation, until the job's own progress rate is known
// How far along a running separation is, and the seconds left. Demucs on this machine reports real progress; a Modal
// job reports none until it ends, so that one goes by its running time against STEM_TYPICAL.
export function stemsEta(st) {
  const p = isNum(st.progress) ? st.progress : 0, el = isNum(st.elapsed) ? st.elapsed : null;
  if (el == null) return { frac: p, left: (1 - p) * STEM_TYPICAL };
  if (p > 0.05 && el >= 10) return { frac: p, left: (el * (1 - p)) / p };
  return { frac: Math.min(0.95, Math.max(p, el / STEM_TYPICAL)), left: Math.max(0, STEM_TYPICAL - el) };
}
export const stemsLabel = (st) => {
  if (st.state === "queued") return `Waiting for another separation${st.position ? ` · ${st.position} ahead` : "…"}`;
  const { frac, left } = stemsEta(st);
  return `Separating stems ${Math.round(frac * 100)}% · ${fmtLeft(left)}`;
};
export function setStems(id, st) {
  const was = stemStatus.get(id);
  stemStatus.set(id, st);
  const running = [...stemStatus.values()].find((x) => x.state === "running");
  jobTitle("stems", running ? stemsEta(running).frac : null);
  for (const l of [...stemListeners]) {
    if (!l.el.isConnected) stemListeners.delete(l);
    else if (l.id === id) l.fn(st, was);
  }
}
// Calls fn(status, previous) whenever this track's stems status changes, for as long as el is on the page.
export function onStems(el, id, fn) { stemListeners.add({ el, id, fn }); }
export function pollStems() {
  if (stemTimer || ![...stemStatus.values()].some(stemsBusy)) return;
  stemTimer = setTimeout(async () => {
    for (const [id, st] of [...stemStatus]) {
      if (!stemsBusy(st)) continue;
      // A failed check (offline, the server waking up) keeps the last state and tries again on the next round.
      try { setStems(id, await api(`/api/tracks/${id}/stems`)); } catch (_) { /* keep polling */ }
    }
    stemTimer = null;
    pollStems();
  }, 2500);
}
export async function startStems(c) {
  setStems(c.id, await api(`/api/tracks/${c.id}/stems`, { method: "POST" }));
  pollStems();
}

let menuNote = null;
export function trackMenu(c) {
  const btn = h("button", { type: "button", class: "menu-btn", "aria-haspopup": "menu", "aria-expanded": "false", "aria-label": "Track actions" }, "⋯");
  const pop = h("div", { class: "menu-pop", role: "menu", hidden: "" });
  const say = h("span", { class: "menu-say muted", role: "status", "aria-live": "polite" });
  const wrap = h("div", { class: "menu-wrap" }, say, btn, pop);
  let sayTimer;
  const tell = (msg, isErr, busy, ms) => {
    clearTimeout(sayTimer);
    say.className = "menu-say " + (isErr ? "error" : "muted");
    say.replaceChildren(...(busy ? [h("span", { class: "spin", "aria-hidden": "true" })] : []), msg);
    if (!busy) sayTimer = setTimeout(() => say.replaceChildren(), ms || (isErr ? 6000 : 3000));
  };
  if (menuNote && menuNote.id === c.id) { const n = menuNote; menuNote = null; tell(n.msg, n.isErr, false, n.ms); setTimeout(() => btn.focus()); }
  const close = (refocus) => { pop.hidden = true; btn.setAttribute("aria-expanded", "false"); document.removeEventListener("pointerdown", outside, true); if (refocus) btn.focus(); };
  const outside = (e) => { if (!wrap.contains(e.target)) close(false); };
  const item = (label, o = {}) => h("button", { type: "button", role: "menuitem", class: "menu-item" + (o.danger ? " danger" : ""), onclick: o.onclick,
    ...(o.disabled ? { disabled: "", title: o.title || "" } : o.title ? { title: o.title } : {}) }, label);
  const items = () => [...pop.querySelectorAll("[role=menuitem]:not([disabled]), .menu-confirm button")];
  const copy = async () => {
    close(true);
    try { await navigator.clipboard.writeText(trackSummary(c)); tell("Copied"); } catch (e) { tell("Couldn't copy here"); }
  };
  const reload = async () => {
    close(true);
    tell("Reloading metadata…", false, true);
    try {
      const r = await api(`/api/tracks/${c.id}/retag`, { method: "POST" });
      menuNote = { id: c.id, msg: !r.tags_found ? "No tags found in the file" : r.changed ? "Metadata reloaded" : "Metadata already up to date" };
      if (currentId === c.id) renderCard(r.card); else menuNote = null;
      try { await loadTracks(); renderList(); } catch (_) {}
    } catch (e) { tell(e.message, true); }
  };
  // meter: "3/4", "4/4" or "auto" re-analyzes with that time signature (bars, grooves and sections all follow it).
  const reanalyze = async (meter) => {
    close(true);
    tell("Re-analyzing… this can take a minute", false, true);
    try {
      await flushEdits(c.id); // the server moves saved edits to the new sections, so it needs the latest
      const r = await api(`/api/tracks/${c.id}/reanalyze${typeof meter === "string" ? `?meter=${encodeURIComponent(meter)}` : ""}`, { method: "POST" });
      setViewsCache(null); // the new card may carry its own waveform and spectrogram
      dropEdits(c.id);
      await loadEdits(c.id);
      const sig = `${(r.card.core && r.card.core.beats_per_bar) || 4}/4`;
      menuNote = r.edits_dropped ? { id: c.id, msg: `Re-analyzed${typeof meter === "string" ? ` in ${sig}` : ""}. Some edits no longer match the new sections, so they were dropped.`, ms: 10000 }
        : { id: c.id, msg: typeof meter === "string" ? `Re-analyzed in ${sig}` : "Re-analyzed with the current engine" };
      if (currentId === c.id) renderCard(r.card); else menuNote = null;
      try { await loadTracks(); renderList(); } catch (_) {}
    } catch (e) { tell(e.message, true); }
  };
  const detectDrums = async () => {
    close(true);
    tell("Detecting drums with the model… this can take a minute or two", false, true);
    try {
      await flushEdits(c.id); // the server moves saved edits to the new sections, as with Re-analyze
      const card = await runDrumModel(c.id, (msg) => tell(`${msg} this can take a minute or two`, false, true));
      setViewsCache(null);
      dropEdits(c.id);
      await loadEdits(c.id);
      menuNote = { id: c.id, msg: "Drums detected with the model" };
      if (currentId === c.id) renderCard(card); else menuNote = null;
    } catch (e) { tell(e.message, true); }
  };
  const drumsItem = () => {
    const label = "Detect drums with the model";
    if (c.extras && c.extras.drum_model) return item("Drums detected with the model", { disabled: true, title: "Re-analyze keeps using the drum model on this track" });
    if (drumsBusy({ state: c.drum_model_state })) return item(drumsLabel({ state: c.drum_model_state }), { disabled: true });
    if (!c.has_audio) return null;
    if (!c.drum_model_available) return item(label, { disabled: true, title: "Needs PyTorch, an optional install: pip install 'tastr[drums]'" });
    return item(label, { onclick: detectDrums, title: "Re-analyze with the pretrained drum model (ADTOF, CC BY-NC-SA 4.0): finds toms and cymbals the built-in lanes miss. Slower, so it runs only when asked." });
  };
  const separate = async () => {
    close(true);
    try { await startStems(c); } catch (e) { tell(e.message, true); }
  };
  const lookUp = async () => {
    close(true);
    tell("Looking up BPM and key…", false, true);
    try {
      const r = await api(`/api/tracks/${c.id}/lookup`, { method: "POST" });
      const found = (r.card.lookups || []).filter((x) => x.status === "found").length;
      const notes = r.notes || [];
      const held = notes.find((n) => n.status === "limited" || n.status === "error");
      menuNote = { id: c.id, ms: held ? 10000 : undefined, isErr: !!held,
        msg: held ? held.error : notes.length && notes.every((n) => n.status === "recent") ? "Already looked up in the last few minutes"
          : found ? `Found in ${found} catalog${found > 1 ? "s" : ""}` : "Not found in the catalogs" };
      if (currentId === c.id) renderCard(r.card); else menuNote = null;
    } catch (e) { tell(e.message, true); }
  };
  const stemsButton = () => {
    const st = stemsOf(c);
    if (st.state === "ready") return item("Stems ready", { disabled: true, title: "Pick stems under Try this in → Strudel" });
    if (stemsBusy(st)) return item(stemsLabel(st), { disabled: true, title: "A few minutes per track" });
    if (!c.has_audio) return item("Separate stems", { disabled: true, title: "The original audio file is not on disk" });
    if (!c.stems_available) return item("Separate stems", { disabled: true, title: "Needs Demucs, an optional install: pip install 'tastr[stems]'" });
    return item(st.state === "error" ? "Separate stems (try again)" : "Separate stems", { onclick: separate });
  };
  const stemsItem = () => { const b = stemsButton(); b.dataset.stems = ""; return b; };
  onStems(wrap, c.id, (st, was) => {
    if (stemsBusy(st)) tell(stemsLabel(st), false, true);
    else if (was && stemsBusy(was)) tell(st.state === "ready" ? "Stems ready" : st.state === "error" ? `Stem separation failed${st.error ? `: ${st.error}` : ""}` : "", st.state === "error");
    const old = pop.querySelector("[data-stems]");
    if (old) { const fresh = stemsItem(), had = document.activeElement === old; old.replaceWith(fresh); if (had) (fresh.disabled ? items()[0] : fresh).focus(); }
  });
  if (stemsBusy(stemsOf(c))) tell(stemsLabel(stemsOf(c)), false, true);
  const remove = async () => {
    try {
      await api(`/api/tracks/${c.id}`, { method: "DELETE" });
    } catch (e) { close(true); tell(e.message, true); return; }
    setCurrentId(null);
    stemStatus.delete(c.id);
    dropEdits(c.id);
    $("card").replaceChildren(h("p", { class: "muted" }, "Select a track."));
    await refresh();
  };
  const showMain = () => {
    const isFile = !c.source || c.source.kind !== "url"; // links have no file to re-read
    pop.replaceChildren(...[
      item("Copy track summary", { onclick: copy }),
      isFile ? item("Reload metadata", { onclick: reload }) : null,
      c.has_audio ? item("Re-analyze", { onclick: () => reanalyze() }) : null, // needs the original audio on disk
      c.has_audio && c.core && c.core.bpm ? item(`Change to ${beatsPerBar(c) === 3 ? "4/4" : "3/4"} time`, { onclick: () => reanalyze(beatsPerBar(c) === 3 ? "4/4" : "3/4") }) : null,
      c.has_audio && c.core && c.core.meter_set ? item("Detect the time signature again", { onclick: () => reanalyze("auto") }) : null,
      (c.lookup_providers || []).length ? item("Look up BPM & key online", { onclick: lookUp }) : null,
      drumsItem(),
      stemsItem(),
      inReview(c.id) ? item("Remove from review", { onclick: () => { close(true); setInReview([c.id], false); } }) : item("Add to review", { onclick: () => { close(true); setInReview([c.id], true); } }),
      h("hr", { class: "menu-sep" }),
      item("Delete track…", { danger: true, onclick: showConfirm })].filter(Boolean));
  };
  const showConfirm = () => {
    const name = (c.source && (c.source.title || c.source.uri)) || `Track ${c.id}`;
    pop.replaceChildren(h("div", { class: "menu-confirm", role: "group", "aria-label": "Confirm delete" },
      h("p", {}, h("b", {}, `Delete “${name}”?`), h("br"), h("span", { class: "muted" }, "This removes the track and its analysis from your library.")),
      h("div", { class: "menu-confirm-btns" },
        h("button", { type: "button", onclick: () => { showMain(); items()[0].focus(); } }, "Cancel"),
        h("button", { type: "button", class: "danger-solid", onclick: remove }, "Delete"))));
    pop.querySelector("button").focus();
  };
  btn.onclick = () => {
    if (!pop.hidden) { close(true); return; }
    showMain(); pop.hidden = false; btn.setAttribute("aria-expanded", "true"); items()[0].focus();
    document.addEventListener("pointerdown", outside, true);
  };
  pop.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(true); return; }
    const list = items(), i = list.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); list[(i + 1) % list.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); list[(i - 1 + list.length) % list.length].focus(); }
    else if (e.key === "Tab") close(false);
  });
  return wrap;
}

// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  // Background tabs barely run timers, so check again as soon as the tab is back.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden || !stemTimer) return;
    clearTimeout(stemTimer); stemTimer = null;
    for (const [id, st] of [...stemStatus]) if (stemsBusy(st)) api(`/api/tracks/${id}/stems`).then((x) => setStems(id, x), () => {});
    pollStems();
  });
}
