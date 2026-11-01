// The groove view's three parts for the section on show: drums (with the pattern picker), bass and chords, each with
// solo/mute, an edit count with Revert, and your verdict on what was detected. g is the view's state (see groove.js).

import { NOTES, cap, h } from "./core.js";
import { styleTip } from "./sequencer.js";
import { editsFor } from "./groove-edits.js";
import { laneName, laneRows } from "./groove-lanes.js";
import { BASS_HELP, MAJ_SCALE, MIN_SCALE, bassRoll, chordTones, flipStep, miniKeys, noteOptions, parseChord, pcOf, romanOf } from "./groove-music.js";
import { markRow } from "./groove-marks.js";

const PART_TIPS = {
  drums: "Measured from the audio. For each bar in the section, the app takes the jump in loudness at each of 16 steps (12 in 3/4) within each lane's frequency band, then averages across the section's bars.",
  bass: "Measured from the audio. For each bar, the app takes the strongest pitch below about 150 Hz, then finds the 4-bar loop that repeats most often in the section. The rhythm row shows where notes start.",
  chords: "Measured from the audio. For each bar, the app matches the notes it hears against major and minor chords, then finds the 4-bar loop that repeats most often in the section. Bars without a confident match show as a dash.",
};
const tplName = (t) => t.title.replace(/^Pattern idea: /, "");
const chgTag = (g) => h("span", { class: "chg-tag", title: `Δ = different from the previous section (${g.secName(g.sel - 1)}). Not an edit.` }, ` · Δ ${g.secName(g.sel - 1)}`);

// Solo/mute per drum lane and per part, like a mixer: playback only (never saved or exported). Lasts while the card is open.
export function smBtns(g, soloSet, muteSet, k, name) {
  return [[soloSet, "S", "Solo", "solo"], [muteSet, "M", "Mute", "mute"]].map(([st, label, tip, cls]) => {
    const id = `${soloSet === g.solo ? "lane" : "part"}-${cls}-${k}`;
    const b = h("button", { type: "button", class: `lane-btn ${cls}`, "aria-pressed": String(st.has(k)), title: `${tip} ${name}`, "aria-label": `${tip}: ${name}`, "data-sm": id }, label);
    // Repainting replaces the button, so focus goes to its replacement.
    b.onclick = () => { if (st.has(k)) st.delete(k); else st.add(k); g.paint(true); const nb = [...g.wrap.querySelectorAll("[data-sm]")].find((x) => x.dataset.sm === id); if (nb) nb.focus(); };
    return b;
  });
}

// a part with edits in this section gets a "N edits · Revert" pill (Revert resets only that part)
function revert(g, part) {
  delete editsFor(g.card, part)[g.ek(g.sel)];
  if (part === "drums") delete g.srcStore()[g.ek(g.sel)];
  g.paint(true);
  g.say(`${cap(part)} back to ${part === "drums" && !g.drumsSug ? "no drums" : "detected"} for ${g.secName() || "this track"}.`);
}
const editPill = (g, part, nEdits) => h("span", { class: "edit-pill" }, nEdits === true ? "Edited" : `${nEdits} edit${nEdits > 1 ? "s" : ""}`, " · ",
  h("button", { type: "button", class: "linklike", "aria-label": `Revert the ${part} in ${g.secName() || "this track"}`, onclick: () => revert(g, part) }, "Revert"));

function panel(g, part, title, kids, foot, nEdits) {
  return h("div", { class: "part" + (nEdits ? " has-edits" : "") + (g.partOn(part) ? "" : " quiet"), "data-part": part },
    h("div", { class: "part-head" }, h("span", { class: "part-sm" }, smBtns(g, g.pSolo, g.pMute, part, part)), h("b", { class: "tip-title", tabindex: "0", "data-tip": PART_TIPS[part] }, title), foot ? h("span", { class: "muted part-foot" }, foot) : null,
      nEdits ? editPill(g, part, nEdits) : null), markRow(g, part, nEdits), ...kids);
}

// ---- drums: picking a template copies it into the section's drum edits, so playback picks it up on the next step ----
const stepDiffs = (BAR, a, b) => { let n = 0; for (const lane of new Set([...Object.keys(a), ...Object.keys(b)])) for (let i = 0; i < BAR; i++) if (((a[lane] || "")[i] || ".") !== ((b[lane] || "")[i] || ".")) n++; return n; };
function useTemplate(g, v, all) {
  const { card, secs, templates } = g;
  const keys = all && secs ? secs.map((_, k) => g.ek(k)) : [g.ek(g.sel)];
  for (const kk of keys) {
    if (v === "") { delete g.srcStore()[kk]; delete editsFor(card, "drums")[kk]; }
    else { g.srcStore()[kk] = v; editsFor(card, "drums")[kk] = JSON.parse(JSON.stringify(templates[v].data.pattern)); }
  }
  g.paint(true);
  const name = v === "" ? (g.drumsSug ? "the detected drums" : "no drums") : tplName(templates[v]);
  g.say(all ? `Every section now uses ${name}.` : `${g.secName() || "The track"} now uses ${name}.`);
}
function drumsPicker(g) {
  const { templates, drumsSug } = g;
  if (!templates.length) return null;
  const v = g.srcStore()[g.ek(g.sel)], t = v != null ? templates[v] || null : null;
  const pickEl = h("select", { class: "drum-src", "aria-label": "Drum pattern for this section", onchange: (e) => useTemplate(g, e.target.value, false) },
    h("option", { value: "", ...(t ? {} : { selected: "" }) }, drumsSug ? "Detected groove" : "None detected"),
    h("optgroup", { label: "Pattern ideas (by tempo)" }, templates.map((x, i) => h("option", { value: String(i), ...(String(i) === String(v) ? { selected: "" } : {}) }, tplName(x)))));
  const isEdited = t && JSON.stringify(g.cur("drums")) !== JSON.stringify(t.data.pattern);
  return h("div", { class: "drum-src-row" }, h("label", { class: "seq-tempo" }, h("span", {}, "Pattern"), pickEl),
    t ? h("span", { class: "origin tpl" }, "Template for ", h("span", { class: "term", tabindex: "0", "data-tip": styleTip(t.data) }, t.data.style || "this tempo"), isEdited ? ", edited" : "") : null,
    g.secs ? h("button", { type: "button", class: "linklike", onclick: () => useTemplate(g, v != null ? String(v) : "", true) }, t ? "Use in every section" : drumsSug ? "Use detected in every section" : "Use no drums in every section") : null);
}
function drumsPanel(g, prev) {
  const { BAR, drumsSug } = g;
  const tpl = g.srcStore()[g.ek(g.sel)] != null, pat = g.cur("drums"), orig = g.drumsOrig();
  const laneCtl = { quiet: (lane) => !g.audible(lane), el: (lane) => h("span", { class: "lane-sm" }, smBtns(g, g.solo, g.mute, lane, laneName(lane))) };
  const lanes = laneRows(pat, g.showChg && !tpl ? prev : null, !tpl && !!drumsSug, (lane, i) => {
    g.edit("drums", (m) => { m[lane] = flipStep(m[lane], i); });
    const el = g.wrap.querySelector(`.drum-grid i[data-lane="${lane}"][data-step="${i}"]`); if (el) el.focus();
  }, drumsSug && drumsSug.data.lane_help, orig, laneCtl);
  return panel(g, "drums", "Drums", [drumsPicker(g), ...(lanes.length ? lanes : [h("p", { class: "muted part-empty" }, "No drums detected. Pick a pattern idea to start from one.")])],
    `1 bar, ${BAR} steps; click any step to edit`, stepDiffs(BAR, pat, orig) || (tpl ? true : 0));
}

// ---- bass: one note per bar, and a rhythm that repeats every bar ----
function bassNotePick(g, n, b, o, prev) {
  const { BAR, key } = g, mine = pcOf(o.notes[b]) !== pcOf(n);
  return h("label", { class: "bar-pick" + (mine ? " mine" : ""), "data-bar": b, title: mine ? `Your edit (detected: ${o.notes[b] || "none"})` : null },
    h("span", { class: "muted" }, `Bar ${b + 1}`, mine ? h("span", { class: "mine-tag" }, " · edited")
      : prev && prev.notes.some(Boolean) && pcOf(prev.notes[b]) !== pcOf(n) ? chgTag(g) : null),
    h("select", { "aria-label": `Bass note, bar ${b + 1}`, onchange: (e) => g.edit("bass", (x) => {
      if (!x.notes.length) x.notes = [null, null, null, null];
      x.notes[b] = e.target.value || null;
      if (!x.rhythm && e.target.value) x.rhythm = BAR === 16 ? "x.......x......." : "x" + ".".repeat(BAR - 1); // beats 1 and 3, or 1 in 3/4
    }) }, noteOptions(n, key)));
}
function bassPanel(g, prev) {
  const { BAR, EMPTY, key } = g;
  const m = g.cur("bass"), has = m.notes.some(Boolean) && m.rhythm, slots = m.notes.length ? m.notes.slice(0, 4) : [null, null, null, null];
  const o = g.baseOf("bass", g.sel), oRhythm = o.rhythm || EMPTY;
  const nEdits = slots.filter((n, b) => pcOf(o.notes[b]) !== pcOf(n)).length + [...(m.rhythm || EMPTY)].filter((ch, i) => ch !== oRhythm[i]).length;
  const noteRow = h("div", { class: "bar-picks" }, slots.map((n, b) => bassNotePick(g, n, b, o, prev)));
  return panel(g, "bass", "Bass", [
    has ? bassRoll(m, key, o, BAR) : h("p", { class: "muted part-empty" }, "No bass in this section. Pick a note for a bar to add one."),
    noteRow,
    ...laneRows({ rhythm: m.rhythm || EMPTY }, g.showChg && prev && prev.rhythm ? { rhythm: prev.rhythm } : null, true, (lane, i) => g.edit("bass", (x) => {
      x.rhythm = flipStep(x.rhythm || EMPTY, i);
      if (!x.notes.some(Boolean)) x.notes = [0, 1, 2, 3].map(() => NOTES[key.tonic]);
    }), BASS_HELP, { rhythm: oRhythm }),
  ], "One note per bar; the rhythm repeats every bar", nEdits);
}

// ---- chords: one per bar, picked from the key's major and minor triads ----
function diatonic(key) {
  const sc = key.mode === "minor" ? MIN_SCALE : MAJ_SCALE, q = key.mode === "minor" ? [1, 0, 0, 1, 1, 0, 0] : [0, 1, 1, 0, 0, 1, 0];
  // Diatonic major and minor triads, leaving out the diminished one.
  return sc.map((d, i) => NOTES[(key.tonic + d) % 12] + (q[i] ? "m" : "")).filter((n, i) => !(key.mode === "minor" ? i === 1 : i === 6));
}
function chordCell(g, c, b, { m, o, prev, diat, isEdited }) {
  const { key } = g;
  const p = parseChord(c), changed = prev && prev.chords.some(Boolean) && prev.chords[b] !== c, mine = (o.chords[b] || null) !== (c || null);
  return h("div", { class: "chord-cell" + (p ? "" : " none") + (mine ? " mine" : ""), "data-bar": b, title: mine ? `Your edit (detected: ${o.chords[b] || "no clear chord"})` : null },
    h("span", { class: "muted chord-bar" }, `Bar ${b + 1}`, mine ? h("span", { class: "mine-tag" }, " · edited") : changed ? chgTag(g) : null),
    h("select", { class: "chord-pick", "aria-label": `Chord, bar ${b + 1}`, onchange: (e) => g.edit("chords", (x) => { if (!x.chords.length) x.chords = [null, null, null, null]; x.chords[b] = e.target.value || null; }) },
      h("option", { value: "", ...(c ? {} : { selected: "" }) }, "—"),
      [...new Set([...(c ? [c] : []), ...diat])].map((n) => h("option", { value: n, ...(n === c ? { selected: "" } : {}) }, n))),
    h("span", { class: "chord-roman" }, p ? (!isEdited && m.roman && m.roman[b]) || romanOf(c, key.tonic, key.mode) : "no clear chord"),
    p ? miniKeys(chordTones(c), p.root) : null,
    h("span", { class: "muted chord-tones" }, p ? chordTones(c).map((t) => NOTES[t]).join(" · ") : ""));
}
function chordsPanel(g, prev) {
  const m = g.cur("chords"), isEdited = !!editsFor(g.card, "chords")[g.ek(g.sel)];
  const slots = m.chords.length ? m.chords.slice(0, 4) : [null, null, null, null];
  const o = g.baseOf("chords", g.sel), ctx = { m, o, prev, diat: diatonic(g.key), isEdited };
  const nEdits = slots.filter((c, b) => (o.chords[b] || null) !== (c || null)).length;
  const cells = slots.map((c, b) => chordCell(g, c, b, ctx));
  return panel(g, "chords", "Chords", [m.chords.some(Boolean) ? null : h("p", { class: "muted part-empty" }, "No clear chords in this section. Pick one for a bar to add it."), h("div", { class: "chord-row" }, cells)].filter(Boolean),
    "One chord per bar, root in the darker key", nEdits);
}

// The panels for the parts this card has, for the section on show; prevOf(part) is the previous section's detected part.
export function partPanels(g, prevOf) {
  return g.parts.map((p) => (p === "drums" ? drumsPanel(g, prevOf(p)) : p === "bass" ? bassPanel(g, prevOf(p)) : chordsPanel(g, prevOf(p))));
}
