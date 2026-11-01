// Drums, bass & chords: each section's loops in one view, with one set of tabs and one transport, and their edits.
// The parts are in groove-parts.js, the toolbar (play, export, versions) in groove-tools.js, the edits store in groove-edits.js.

import { SEC_COLORS, cap, h, isNum } from "./core.js";
import { barSteps, sectionBus } from "./card.js";
import { diffText, legend } from "./sequencer.js";
import { allEdits, editedKeys, editsFor, putEdits, saveEdits, saveTimers } from "./groove-edits.js";
import { parseChord, pcOf } from "./groove-music.js";
import { partPanels } from "./groove-parts.js";
import { toolsRow } from "./groove-tools.js";
import { loadMarks } from "./groove-marks.js";

const bySec = (sug) => (sug && Array.isArray(sug.data.by_section) && sug.data.by_section.length ? sug.data.by_section : null);

// The view's state (g), shared by its parts, toolbar and marks.
function grooveState(card, drumsSug, bassSug, harmSug, templates) {
  const key = card.core && card.core.key && isNum(card.core.key.tonic) ? card.core.key : null;
  const BAR = barSteps(card); // steps per bar: 16 in 4/4, 12 in 3/4
  const parts = [(drumsSug || templates.length) && "drums", bassSug && key && "bass", harmSug && key && "chords"].filter(Boolean);
  const sugOf = { drums: drumsSug, bass: bassSug, chords: harmSug };
  const secs = bySec(drumsSug) || bySec(bassSug) || bySec(harmSug);
  const letters = secs ? [...new Set(secs.map((x) => x.letter || x.label))] : [];
  const g = {
    card, key, BAR, EMPTY: ".".repeat(BAR), parts, secs, drumsSug, templates,
    sel: secs ? secs.reduce((best, x, k) => ((x.energy || 0) > (secs[best].energy || 0) ? k : best), 0) : 0, showChg: false, nowStep: -1,
    on: new Set(parts), // parts that go into the MIDI file (picked in the Export MIDI popover)
    // Solo/mute per drum lane and per part, like a mixer: playback only (never saved or exported). Lasts while the card is open.
    mute: new Set(), solo: new Set(), pMute: new Set(), pSolo: new Set(),
    marks: [], // your pattern reviews (groove-marks.js)
    trackBpm: Math.round((drumsSug && drumsSug.data.bpm) || (card.core && card.core.bpm) || 120),
    wrap: h("div", { class: "sug groove" }),
  };
  // Older cards have no bass/chords by_section: those parts then use the global loop in every section.
  const fromData = {
    drums: (x) => (x ? x.pattern : drumsSug ? drumsSug.data.pattern : {}),
    bass: (x) => { const d = x || bassSug.data; return { notes: [...(d.loop || [])], rhythm: d.rhythm || null }; },
    chords: (x) => { const d = x || harmSug.data; return { chords: [...(d.progression || [])].map((c) => (parseChord(c) ? c : null)), roman: d.roman || null }; },
  };
  g.colorOf = (x) => SEC_COLORS[letters.indexOf(x.letter || x.label) % SEC_COLORS.length];
  g.ek = (k) => (secs ? secs[k].idx : "all");
  g.baseOf = (part, k) => {
    const list = bySec(sugOf[part]), x = list && secs ? list.find((y) => y.idx === secs[k].idx) : null;
    return fromData[part](x || null);
  };
  g.cur = (part, k = g.sel) => editsFor(card, part)[g.ek(k)] || g.baseOf(part, k);
  g.srcStore = () => editsFor(card, "drumsrc"); // a function: restoring a version replaces the edits object
  // What the user's edits are measured against: the picked template, or what was detected.
  g.drumsOrig = () => { const v = g.srcStore()[g.ek(g.sel)]; return v != null && templates[v] ? templates[v].data.pattern : g.baseOf("drums", g.sel); };
  g.origOf = (part) => (part === "drums" ? g.drumsOrig() : g.baseOf(part, g.sel));
  g.edit = (part, fn) => {
    const m = JSON.parse(JSON.stringify(g.cur(part)));
    fn(m);
    // Back to where it started (the detected part or the picked template): no longer an edit. A picked template stays picked.
    if (JSON.stringify(m) === JSON.stringify(g.origOf(part)) && !(part === "drums" && g.srcStore()[g.ek(g.sel)] != null)) delete editsFor(card, part)[g.ek(g.sel)];
    else editsFor(card, part)[g.ek(g.sel)] = m;
    g.paint(true);
  };
  g.edited = () => [...parts, "drumsrc"].some((p) => editsFor(card, p)[g.ek(g.sel)] != null);
  g.audible = (lane) => (g.solo.size ? g.solo.has(lane) : !g.mute.has(lane));
  g.partOn = (p) => (g.pSolo.size ? g.pSolo.has(p) : !g.pMute.has(p));
  g.secName = (k = g.sel) => (secs ? `${cap(secs[k].label)} ${secs[k].letter || ""}`.trim() : "");
  // Loop length of section k in bars (the longest of its bass and chord loops, 1 to 4).
  g.loopBars = (k = g.sel) => {
    let n = 1;
    if (parts.includes("bass")) n = Math.max(n, g.cur("bass", k).notes.length);
    if (parts.includes("chords")) n = Math.max(n, g.cur("chords", k).chords.length);
    return Math.max(1, Math.min(4, n));
  };
  return g;
}

// What changed against the previous section, per part ("" when nothing did).
function partDiff(g, part, m, p) {
  if (part === "drums") { if (g.srcStore()[g.ek(g.sel)] != null) return ""; const t = diffText(m, p); return t === "same as previous section" ? "" : t; }
  if (part === "bass") {
    if (!m.notes.some(Boolean)) return p.notes.some(Boolean) ? "bass drops out" : "";
    if (!p.notes.some(Boolean)) return "bass comes in";
    const nb = m.notes.filter((n, b) => pcOf(n) !== pcOf(p.notes[b])).length;
    return [nb ? `${nb} bass note${nb > 1 ? "s" : ""} change` : "", (m.rhythm || "") !== (p.rhythm || "") ? "new bass rhythm" : ""].filter(Boolean).join(", ");
  }
  if (!m.chords.some(Boolean)) return p.chords.some(Boolean) ? "chords drop out" : "";
  if (!p.chords.some(Boolean)) return "chords come in";
  const nc = m.chords.filter((c, b) => c !== p.chords[b]).length;
  return nc ? `${nc} chord${nc > 1 ? "s" : ""} change` : "";
}

// The playhead lights the drum step, the bass roll column and the current bass note / chord bar.
function setNow(g, st) {
  const { wrap, BAR } = g;
  g.nowStep = st;
  wrap.querySelectorAll(".now").forEach((el) => el.classList.remove("now"));
  if (st < 0) return;
  wrap.querySelectorAll(`.drum-grid i[data-step="${st % BAR}"]`).forEach((el) => el.classList.add("now"));
  wrap.querySelectorAll(`[data-bar="${Math.floor(st / BAR)}"]`).forEach((el) => el.classList.add("now"));
  const ph = wrap.querySelector(".roll-head"); if (ph) { ph.setAttribute("x", String(st * 10)); ph.classList.add("now"); }
}

// The note under the section tabs: which section, its bars, and what changed from the one before.
function paintSecNote(g, note) {
  const { secs, sel, parts } = g;
  const sec = secs[sel], pv = sel > 0 ? secs[sel - 1] : null;
  const diff = pv ? parts.map((p) => partDiff(g, p, g.cur(p), g.baseOf(p, sel - 1))).filter(Boolean).join("; ") || "same as previous section" : "";
  note.replaceChildren(h("b", {}, g.secName()), h("span", { class: "muted" }, ` · bars ${sec.start_bar}-${sec.end_bar}`),
    h("div", { class: "muted sec-diff" }, pv
      ? ["Compared with ", h("button", { type: "button", class: "sec-link", title: "Go to this section", style: `--c:${g.colorOf(pv)}`, onclick: () => { g.sel -= 1; g.paint(); } }, g.secName(sel - 1)), `: ${diff}`]
      : "First section, nothing before it to compare with"));
}

export function grooveView(card, drumsSug, bassSug, harmSug, templates = []) {
  const g = grooveState(card, drumsSug, bassSug, harmSug, templates);
  const { secs, wrap } = g, bus = sectionBus;
  g.setNow = (st) => setNow(g, st);
  const tools = toolsRow(g);
  loadMarks(g);
  const body = h("div", { class: "sec-body groove-parts" });
  const tabs = h("div", { class: "sec-tabs", role: "tablist", "aria-label": "Section" });
  const note = h("div", { class: "sec-note" });
  const toggle = h("input", { type: "checkbox" }); // Δ outlines start off
  toggle.addEventListener("change", () => { g.showChg = toggle.checked; g.paint(true); });
  g.paint = (quiet) => {
    saveEdits(card);
    bus.dispatchEvent(new CustomEvent("edits")); // the Strudel sketch regenerates from these
    g.resetBtn.hidden = !g.edited();
    g.resetBtn.textContent = secs ? "Reset this section" : "Reset edits";
    const editedNow = new Set(editedKeys(allEdits(card)));
    g.resetAllBtn.hidden = !editedNow.size;
    [...tabs.children].forEach((b, k) => b.classList.toggle("edited", editedNow.has(String(g.ek(k)))));
    g.paintVersions();
    body.replaceChildren(...partPanels(g, (part) => (secs && g.sel > 0 ? g.baseOf(part, g.sel - 1) : null)));
    setNow(g, g.nowStep);
    if (!secs) return;
    [...tabs.children].forEach((b, k) => b.setAttribute("aria-selected", String(k === g.sel)));
    paintSecNote(g, note);
    if (quiet !== true) bus.dispatchEvent(new CustomEvent("section", { detail: { idx: secs[g.sel].idx, from: "groove" } }));
  };
  const mineKey = h("span", { tabindex: "0", "data-tip": "A blue ring marks a step, note or chord you changed from what was detected (or from the picked pattern idea). The fill still shows how loud the step is now, so a ring around an empty step is a hit you removed. Hover one to see what it was." }, h("i", { class: "sw mine" }), "Your edit");
  // One legend above all parts: its states apply to the drums, the bass rhythm and edits everywhere.
  const foot = h("div", { class: "grid-foot" }, h("div", { class: "grid-legend" }, legend(true, g.BAR, !!secs), mineKey), secs ? h("label", { class: "chg-toggle" }, toggle, "Outline Δ from the previous section") : null);
  if (secs) {
    bus.addEventListener("section", (e) => {
      if (e.detail.from !== "chart") return;
      const k = secs.findIndex((x) => x.idx === e.detail.idx);
      if (k >= 0) { g.sel = k; g.paint(true); }
    });
    secs.forEach((x, k) => tabs.append(h("button", { type: "button", role: "tab", class: "sec-tab", style: `--c:${g.colorOf(x)}`, onclick: () => { g.sel = k; g.paint(); } }, g.secName(k))));
    wrap.append(tabs, note);
  }
  wrap.append(tools, foot, body);
  g.paint(true);
  return wrap;
}

// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  window.addEventListener("pagehide", () => [...saveTimers.keys()].forEach((id) => putEdits(id, true)));
}
