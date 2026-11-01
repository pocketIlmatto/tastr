// The groove view's toolbar: Play and tempo, Export MIDI, saved versions of your edits, the reset buttons and the status line.
// g is the view's state (see groove.js).

import { api, cap, h } from "./core.js";
import { GM_NOTE, bassVoice, downloadBytes, dragNumber, midiMulti, padVoice, startLoop, stopAllPlayback, voice } from "./sequencer.js";
import { allEdits, editedKeys, editsFor, setAllEdits } from "./groove-edits.js";
import { bassMidi, chordMidis } from "./groove-music.js";

// A popover under its button: kept inside the viewport, focus on its first field, closed by a click outside or Esc.
function popover(btn, pop, wrapEl, paintFn) {
  const outside = (e) => { if (!wrapEl.contains(e.target)) close(false); };
  function close(refocus) { pop.hidden = true; btn.setAttribute("aria-expanded", "false"); document.removeEventListener("pointerdown", outside, true); if (refocus) btn.focus(); }
  function open() {
    pop.hidden = false; btn.setAttribute("aria-expanded", "true"); pop.style.left = "0px"; paintFn();
    // Keep the popover inside the viewport.
    const r = pop.getBoundingClientRect(), over = r.right - (document.documentElement.clientWidth - 8);
    if (over > 0) pop.style.left = `${-Math.min(over, r.left - 8)}px`;
    const f = pop.querySelector("input"); if (f) f.focus();
    document.addEventListener("pointerdown", outside, true);
  }
  btn.onclick = () => { if (!pop.hidden) close(true); else open(); };
  pop.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); close(true); } });
  return close;
}

// ---- Play: loops the section on show with your edits, solo and mute ----
function playBass(g, ctx, t, st, sd, m) {
  const { BAR } = g, r = m.rhythm, b = Math.floor(st / BAR) % Math.max(1, m.notes.length), i = st % BAR;
  if (!r || r[i] === "." || !m.notes[b] || bassMidi(m.notes[b]) == null) return;
  let len = 1; while (i + len < BAR && r[i + len] === "." && len < 4) len++;
  bassVoice(ctx, bassMidi(m.notes[b]), t, sd * len * 0.95, r[i] === "x" ? 1 : 0.6);
}
function playChords(g, ctx, t, st, sd, m) {
  const { BAR } = g;
  if (st % BAR) return;
  const c = m.chords[Math.floor(st / BAR) % Math.max(1, m.chords.length)];
  if (c) padVoice(ctx, chordMidis(c), t, sd * BAR);
}
// Reads the current section and edits on every step, so changes (and template swaps) are heard right away.
function playStep(g, ctx, t, st, sd) {
  const { BAR } = g;
  if (g.partOn("drums")) for (const [lane, p] of Object.entries(g.cur("drums"))) if (typeof p === "string" && g.audible(lane) && p[st % BAR] && p[st % BAR] !== ".") voice(ctx, lane, t, p[st % BAR] === "x" ? 1 : 0.55);
  if (g.partOn("bass")) playBass(g, ctx, t, st, sd, g.cur("bass"));
  if (g.partOn("chords")) playChords(g, ctx, t, st, sd, g.cur("chords"));
}
function playControls(g) {
  const playBtn = h("button", { type: "button", class: "seq-play", "aria-pressed": "false" }, "▶ Play");
  const bpmIn = h("input", { type: "number", min: "40", max: "240", step: "1", value: String(g.trackBpm), "aria-label": "Playback tempo in BPM", class: "seq-bpm" });
  dragNumber(bpmIn);
  g.bpmVal = () => Math.max(40, Math.min(240, +bpmIn.value || g.trackBpm));
  const ui = (playing) => { playBtn.textContent = playing ? "■ Stop" : "▶ Play"; playBtn.setAttribute("aria-pressed", String(playing)); if (!playing) g.setNow(-1); };
  playBtn.onclick = () => {
    if (playBtn.getAttribute("aria-pressed") === "true") { stopAllPlayback(); return; }
    if (!g.parts.some(g.partOn)) { g.say("Everything is muted. Unmute a part to play."); return; }
    try {
      startLoop((ctx, t, st, sd) => playStep(g, ctx, t, st, sd), g.bpmVal, 4 * g.BAR, (st) => g.setNow(st % (g.loopBars() * g.BAR)), () => ui(false));
      ui(true); g.say("");
    } catch (e) { g.say("Audio isn't available in this browser."); }
  };
  return [playBtn, h("label", { class: "seq-tempo" }, bpmIn, h("span", {}, "BPM"))];
}

// ---- Export MIDI: the parts you pick, for this section's loop or the whole track ----
const EXP_WHAT = { drums: "General MIDI drum notes", bass: "the bass line", chords: "the chords as triads" };
const barsOf = (k) => `${k} bar${k > 1 ? "s" : ""}`;
// How many bars section k lasts in the track.
const secLen = (g, k) => Math.max(1, (g.secs[k].end_bar ?? g.secs[k].start_bar ?? 1) - (g.secs[k].start_bar ?? 1) + 1);
const trackBars = (g) => (g.secs ? g.secs.reduce((a, _, k) => a + secLen(g, k), 0) : g.loopBars());

// One bar of section k, playing bar lb of its loop, starting at tick t0, into the event lists.
function addBar(g, ev, k, lb, t0) {
  const { BAR, on } = g, TICKS = BAR * 120;
  if (on.has("drums")) for (const [lane, p] of Object.entries(g.cur("drums", k))) if (typeof p === "string") [...p.slice(0, BAR)].forEach((ch, i) => { if (ch !== ".") ev.drums.push({ t: t0 + i * 120, d: 60, note: GM_NOTE(lane), vel: ch === "x" ? 110 : 70 }); });
  if (on.has("bass")) {
    const m = g.cur("bass", k), nn = m.notes.length ? m.notes[lb % m.notes.length] : null;
    if (nn && m.rhythm && bassMidi(nn) != null) [...m.rhythm].forEach((ch, i) => { if (ch !== ".") ev.bass.push({ t: t0 + i * 120, d: 110, note: bassMidi(nn), vel: ch === "x" ? 110 : 75 }); });
  }
  if (on.has("chords")) {
    const cs = g.cur("chords", k).chords, c = cs.length ? cs[lb % cs.length] : null;
    if (c) chordMidis(c).forEach((note) => ev.chords.push({ t: t0, d: TICKS - 20, note, vel: 90 }));
  }
}
function doExport(g, whole) {
  const { BAR, secs, on, card } = g, TICKS = BAR * 120, ev = { drums: [], bass: [], chords: [] };
  let n = 0;
  if (whole) secs.forEach((_, k) => { const L = g.loopBars(k); for (let b = 0; b < secLen(g, k); b++) addBar(g, ev, k, b % L, (n + b) * TICKS); n += secLen(g, k); });
  else { n = g.loopBars(); for (let b = 0; b < n; b++) addBar(g, ev, g.sel, b, b * TICKS); }
  const tracks = [["Drums", 9, "drums"], ["Bass", 0, "bass"], ["Chords", 1, "chords"]].filter(([, , p]) => on.has(p)).map(([name, ch, p]) => [name, ch, ev[p]]);
  if (!tracks.length) { g.say("Pick at least one part to export."); return; }
  const file = `${(card.source.title || "Pattern").replace(/[^\w\- ().]+/g, "").trim() || "Pattern"}${secs && !whole ? " - " + g.secName() : ""}.mid`;
  downloadBytes(midiMulti(tracks, g.bpmVal(), n * TICKS, BAR / 4), file, "audio/midi");
  g.say(`Saved "${file}" (${tracks.map((x) => x[0].toLowerCase()).join(" + ")}, ${g.bpmVal()} BPM, ${n} bar${n > 1 ? "s" : ""} of ${BAR / 4}/4${whole ? `, ${secs.length} sections` : ""}).`);
}
function exportMenu(g) {
  const { secs, card } = g;
  let expScope = "section"; // "section" (the loop shown) or "track" (every section in order, each looping over its bars)
  const wholeTrack = () => !!secs && expScope === "track";
  const exportBtn = h("button", { type: "button", class: "ver-btn", "aria-haspopup": "true", "aria-expanded": "false", title: "Save the parts as one MIDI file, with a track each" }, "Export MIDI ▾");
  const expPop = h("div", { class: "ver-pop exp-pop", role: "dialog", "aria-label": "Export MIDI", hidden: "" });
  const expWrap = h("span", { class: "ver-wrap" }, exportBtn, expPop);
  // With sections: this section's loop, or the whole track (each section's loop repeated for as long as it lasts)
  const scopePick = (n) => h("div", { class: "exp-scope", role: "radiogroup", "aria-label": "What to export" }, [["section", `This section (${g.secName()})`, `${n}-bar loop`], ["track", "Whole track", `all ${secs.length} sections in order, ${barsOf(trackBars(g))}`]].map(([v, label, note]) => {
    const rb = h("input", { type: "radio", name: `exp-scope-${card.id}`, value: v, ...(expScope === v ? { checked: "" } : {}) });
    rb.onchange = () => { expScope = v; paintExport(); expPop.querySelector(`input[value="${v}"]`).focus(); };
    return h("label", { class: "exp-part" }, rb, h("b", {}, label), h("span", { class: "muted" }, ` · ${note}`));
  }));
  function paintExport() {
    const { on } = g;
    const go = h("button", { type: "button", class: "exp-go", ...(on.size ? {} : { disabled: "" }) }, "Export");
    const n = g.loopBars();
    expPop.replaceChildren(
      secs ? scopePick(n) : null,
      h("p", { class: "muted ver-note" }, `${secs ? "" : `This track · ${barsOf(n)} · `}${g.bpmVal()} BPM, ${g.BAR / 4}/4, with your edits. Each part becomes its own track, so it loads into any DAW.${wholeTrack() ? " Each section repeats its loop for as many bars as it lasts." : ""}`),
      h("div", { class: "exp-parts" }, g.parts.map((p) => {
        const cb = h("input", { type: "checkbox", ...(on.has(p) ? { checked: "" } : {}) });
        cb.onchange = () => { if (cb.checked) on.add(p); else on.delete(p); go.disabled = !on.size; };
        return h("label", { class: "exp-part" }, cb, h("b", {}, cap(p)), h("span", { class: "muted" }, ` · ${EXP_WHAT[p]}`));
      })),
      h("div", { class: "exp-actions" }, go, h("button", { type: "button", class: "linklike", onclick: () => closeExp(true) }, "Cancel")));
    go.onclick = () => { doExport(g, wholeTrack()); closeExp(true); };
  }
  const closeExp = popover(exportBtn, expPop, expWrap, paintExport);
  return expWrap;
}

// ---- Versions: saved copies of this track's edits (the current edits autosave; a version is a named copy to come back to) ----
const fmtDate = (t) => new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const secWord = (n) => `${n} section${n === 1 ? "" : "s"}`;
function versionsMenu(g) {
  const { card } = g;
  const verBtn = h("button", { type: "button", class: "ver-btn", "aria-haspopup": "true", "aria-expanded": "false" }, "Versions ▾");
  const verPop = h("div", { class: "ver-pop", role: "dialog", "aria-label": "Versions of your edits", hidden: "" });
  const verWrap = h("span", { class: "ver-wrap" }, verBtn, verPop);
  const verUrl = `/api/tracks/${card.id}/edit-versions`;
  let versions = null, verErr = "", undoSnap = null;
  const snap = () => JSON.parse(JSON.stringify(allEdits(card)));
  const restore = (edits) => { setAllEdits(card, JSON.parse(JSON.stringify(edits || {}))); g.paint(true); };
  const sayUndo = (msg) => g.status.replaceChildren(msg + " ", h("button", { type: "button", class: "linklike", onclick: () => { if (undoSnap) restore(undoSnap); undoSnap = null; g.say("Undone."); } }, "Undo"));
  // Goes through the autosave like any edit, so the server copy is cleared too.
  g.discardAll = (msg) => { undoSnap = snap(); restore({}); sayUndo(msg); };
  const loadVersions = async () => {
    try { versions = await api(verUrl); verErr = ""; } catch (e) { verErr = e.message; }
    paintVersions();
  };
  const restoreV = async (v) => {
    try {
      const full = await api(`${verUrl}/${v.id}`);
      undoSnap = snap(); restore(full.edits); closeVer(true); sayUndo(`Restored “${v.name}”.`);
    } catch (e) { g.say(`Couldn't restore “${v.name}”: ${e.message}`); }
  };
  const deleteV = async (v) => {
    try { await api(`${verUrl}/${v.id}`, { method: "DELETE" }); g.say(`Deleted “${v.name}”.`); } catch (e) { g.say(`Couldn't delete “${v.name}”: ${e.message}`); }
    await loadVersions();
    verBtn.focus();
  };
  // The name box and Save, for a new version of the edits as they are now.
  function saveRow(n, list) {
    const name = h("input", { type: "text", class: "ver-name", placeholder: `Version ${list.length + 1}`, "aria-label": "Name for this version", maxlength: "60" });
    const saveV = h("button", { type: "button", ...(n ? {} : { disabled: "" }) }, "Save");
    const doSave = async () => {
      const nm = name.value.trim() || `Version ${list.length + 1}`;
      saveV.disabled = true;
      try { await api(verUrl, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: nm, edits: snap() }) }); g.say(`Saved “${nm}”.`); }
      catch (e) { g.say(`Couldn't save the version: ${e.message}`); }
      await loadVersions();
      const f = verPop.querySelector("input"); if (f) f.focus();
    };
    saveV.onclick = doSave;
    name.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); if (n) doSave(); } });
    return h("div", { class: "ver-save" }, name, saveV);
  }
  function versionList(list) {
    if (verErr) return h("p", { class: "error ver-note" }, `Couldn't load the saved versions: ${verErr}`);
    if (!versions) return h("p", { class: "muted ver-note" }, "Loading saved versions…");
    if (!list.length) return h("p", { class: "muted ver-note" }, "No saved versions yet.");
    return h("ul", { class: "ver-list", "aria-label": "Saved versions" }, list.map((v) => h("li", {},
      h("span", { class: "ver-meta", title: v.name }, h("b", {}, v.name), h("span", { class: "muted" }, ` · ${fmtDate(v.created_at)} · ${secWord(v.sections)}`)),
      h("button", { type: "button", "aria-label": `Restore ${v.name}`, onclick: () => restoreV(v) }, "Restore"),
      h("button", { type: "button", class: "ver-del", "aria-label": `Delete ${v.name}`, onclick: () => deleteV(v) }, "Delete"))));
  }
  function paintVersions() {
    if (verPop.hidden) return;
    const n = editedKeys(allEdits(card)).length, list = versions || [];
    verPop.replaceChildren(
      h("p", { class: "muted ver-note" }, n ? `Your edits (${secWord(n)}) save automatically with this track. Save a version to come back to it later.`
        : "No edits yet. Edits save automatically with this track; save a version to keep a copy you can come back to."),
      saveRow(n, list),
      versionList(list),
      n ? h("button", { type: "button", class: "linklike ver-discard", onclick: () => { undoSnap = snap(); restore({}); closeVer(true); sayUndo("Back to the detected patterns everywhere."); } }, "Discard all edits (back to detected)") : null);
  }
  g.paintVersions = paintVersions;
  const closeVer = popover(verBtn, verPop, verWrap, () => { paintVersions(); loadVersions(); });
  return verWrap;
}

export function toolsRow(g) {
  g.status = h("span", { class: "seq-status muted", role: "status", "aria-live": "polite" });
  g.say = (m) => { g.status.textContent = m; };
  const [playBtn, tempo] = playControls(g);
  g.resetBtn = h("button", { type: "button", class: "seq-reset", hidden: "" }, "Reset edits");
  g.resetBtn.onclick = () => { [...g.parts, "drumsrc"].forEach((p) => delete editsFor(g.card, p)[g.ek(g.sel)]); g.paint(true); g.say("Edits reset for this section."); };
  g.resetAllBtn = h("button", { type: "button", class: "seq-reset", hidden: "", title: "Discard every edit in every section of this track (you can undo)" }, "Reset whole track");
  const expWrap = exportMenu(g), verWrap = versionsMenu(g);
  g.resetAllBtn.onclick = () => g.discardAll("Edits reset in every section.");
  return h("div", { class: "seq-tools" }, playBtn, tempo, expWrap, verWrap, g.resetBtn, g.resetAllBtn, g.status);
}
