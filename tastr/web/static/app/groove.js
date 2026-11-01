// Drums, bass & chords: each section's loops in one view, with one set of tabs and one transport, and their edits.

import { NOTES, SEC_COLORS, api, cap, h, isNum, svg } from "./core.js";
import { JSON_PUT, loadReview } from "./labels.js";
import { barSteps, sectionBus } from "./card.js";
import { GM_NOTE, bassVoice, diffText, downloadBytes, dragNumber, legend, midiMulti, padVoice, startLoop, stopAllPlayback, styleTip, voice } from "./sequencer.js";

// The engine sends what each measured lane means (data.lane_help); this covers cards analyzed before it did.
const LANE_HELP = {
  kick: "Kick: low hits whose energy sits below about 120 Hz.",
  snare: "Snare: noisy mid hits (400 Hz to 5 kHz), usually with some body around 200 Hz.",
  hat: "Hi-hat: short hits above 6 kHz.",
  cymbal: "Cymbal: crash and ride hits, found by the optional drum model.",
};
export const laneName = (lane) => lane.replace(/_/g, " ");
const laneHelp = (lane, measured, help) => {
  if (!measured) return "Part of a template pattern, not measured from the audio.";
  const text = help && help[lane];
  return text ? `${cap(laneName(lane))}: ${text.charAt(0).toLowerCase()}${text.slice(1)}` : LANE_HELP[lane] || "Measured from the audio.";
};

// One lane per instrument; the steps (16 in 4/4, 12 in 3/4) sit in tinted beat groups of 4, with beat numbers above the first lane.
// `orig` is what the user's edits are measured against (the detected pattern or the picked template): steps that differ are "mine".
// `laneCtl` ({ quiet(lane), el(lane) }) adds solo/mute buttons after each lane name and fades lanes that won't play.
const STEP_NAMES = { x: "strong", o: "softer", ".": "empty" };
export function laneRows(pattern, prev, measured, onToggle, help, orig, laneCtl) {
  const rows = [];
  let nBeats = 4;
  for (const [lane, pat] of Object.entries(pattern)) {
    if (typeof pat !== "string") continue;
    nBeats = pat.length === 12 ? 3 : 4;
    const cells = [...pat.slice(0, nBeats * 4)].map((ch, i) => {
      const changed = prev && prev[lane] && prev[lane][i] !== ch;
      const was = orig ? (orig[lane] || "")[i] || "." : ch, mine = was !== ch;
      return h("i", { class: [ch === "x" ? "full" : ch === "o" ? "half" : "", changed ? "chg" : "", mine ? "mine" : ""].join(" ").trim(),
        title: mine ? `Your edit (was ${STEP_NAMES[was] || "empty"})` : null,
        "data-step": i, "data-lane": lane, tabindex: onToggle ? "0" : null, role: onToggle ? "button" : null,
        "aria-label": onToggle ? `${laneName(lane)}, step ${i + 1}: ${STEP_NAMES[ch] || "empty"}. Press to change.` : null,
        onclick: onToggle ? () => onToggle(lane, i) : null,
        onkeydown: onToggle ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onToggle(lane, i); } } : null });
    });
    const beats = [...Array(nBeats).keys()].map((b) => h("div", { class: "beat-group" }, cells.slice(b * 4, b * 4 + 4)));
    rows.push(h("div", { class: "drum-lane" + (laneCtl && laneCtl.quiet(lane) ? " quiet" : "") }, h("span", { class: "name", tabindex: "0", "data-tip": laneHelp(lane, measured, help) }, laneName(lane)),
      laneCtl ? laneCtl.el(lane) : null, h("div", { class: "drum-grid" }, beats)));
  }
  if (rows.length) rows.unshift(h("div", { class: "drum-lane beat-nums", "aria-hidden": "true" }, h("span", { class: "name" }), laneCtl ? h("span", { class: "lane-sm" }) : null, h("div", { class: "drum-grid" }, [...Array(nBeats).keys()].map((n) => h("span", {}, n + 1)))));
  return rows;
}

// Edits per card, part ("drums", "bass", "chords", "drumsrc" = picked template) and section idx ("all" without sections).
// They autosave with the track on the server (PUT /edits, about a second after the last change); playback reads them on every step.
const partEdits = new Map(), EDIT_PARTS = ["drums", "bass", "chords", "drumsrc"];
export const savedEdits = new Map(), saveTimers = new Map(); // track id -> JSON last loaded or saved; pending autosaves
export const allEdits = (card) => { if (!partEdits.has(card.id)) partEdits.set(card.id, {}); return partEdits.get(card.id); };
const editsFor = (card, part) => (allEdits(card)[part] ||= {});
// Section keys (section idx, or "all") that have any edit.
// JSON of the parts that hold edits, so empty parts don't count as a change.
const editsJson = (e) => JSON.stringify(Object.fromEntries(Object.entries(e || {}).filter(([, v]) => v && Object.keys(v).length)));
export const editedKeys = (edits) => { const ks = new Set(); for (const p of EDIT_PARTS) Object.keys((edits || {})[p] || {}).forEach((k) => ks.add(k)); return [...ks]; };
export async function loadEdits(id) {
  if (savedEdits.has(id)) return;
  const e = await api(`/api/tracks/${id}/edits`).catch(() => null);
  if (!e || savedEdits.has(id)) return; // unloaded edits never autosave, so a failed load can't overwrite them
  partEdits.set(id, e);
  savedEdits.set(id, editsJson(e));
}
function putEdits(id, keepalive) {
  clearTimeout(saveTimers.get(id));
  saveTimers.delete(id);
  const body = editsJson(partEdits.get(id));
  const prev = savedEdits.get(id);
  if (body === prev) return Promise.resolve();
  savedEdits.set(id, body);
  return fetch(`/api/tracks/${id}/edits`, { method: "PUT", headers: { "Content-Type": "application/json" }, body, keepalive })
    .then((r) => { if (!r.ok) throw new Error(r.statusText); })
    .catch(() => { if (savedEdits.get(id) === body) savedEdits.set(id, prev); }); // the next change tries again
}
function saveEdits(card) {
  if (!savedEdits.has(card.id) || editsJson(allEdits(card)) === savedEdits.get(card.id)) return;
  clearTimeout(saveTimers.get(card.id));
  saveTimers.set(card.id, setTimeout(() => putEdits(card.id), 1000));
}
// Send pending autosaves now: before re-analyzing (which remaps the saved edits) and when the page goes away.
export const flushEdits = (id) => (saveTimers.has(id) ? putEdits(id) : Promise.resolve());
// Forget a track's edits so the next render loads them again (after re-analyzing or deleting it).
export const dropEdits = (id) => { clearTimeout(saveTimers.get(id)); saveTimers.delete(id); partEdits.delete(id); savedEdits.delete(id); };
const MAJ_SCALE = [0, 2, 4, 5, 7, 9, 11], MIN_SCALE = [0, 2, 3, 5, 7, 8, 10];
// Numerals count from the key's own scale, like the engine (music.roman_numeral): in A minor, F is VI.
// ♭/♯ only mark chords from outside the key.
const ROMAN = { major: ["I", "♭II", "II", "♭III", "III", "IV", "♯IV", "V", "♭VI", "VI", "♭VII", "VII"],
  minor: ["I", "♭II", "II", "III", "♯III", "IV", "♯IV", "V", "VI", "♯VI", "VII", "♯VII"] };
// Note names can use sharps or flats (the engine writes bass notes like "Ab").
const pcOf = (n) => { const m = /^([A-G])([#b]?)/.exec(n || ""); return m ? (NOTES.indexOf(m[1]) + (m[2] === "#" ? 1 : m[2] === "b" ? 11 : 0)) % 12 : -1; };
const parseChord = (name) => { const m = /^([A-G][#b]?)(m(?!aj))?/.exec(name || ""); return m ? { root: pcOf(m[1]), minor: !!m[2] } : null; };
const chordTones = (name) => { const p = parseChord(name); return p ? [0, p.minor ? 3 : 4, 7].map((d) => (p.root + d) % 12) : []; };
const romanOf = (name, tonic, mode) => { const p = parseChord(name); if (!p) return ""; const r = ROMAN[mode === "minor" ? "minor" : "major"][(p.root - tonic + 12) % 12]; return p.minor ? r.toLowerCase() : r; };
const bassMidi = (n) => { const i = pcOf(n); return i < 0 ? null : 12 * (i <= 4 ? 3 : 2) + i; }; // C2..E2, F1..B1
const chordMidis = (name) => { const p = parseChord(name); if (!p) return []; const r = 48 + p.root; return [r, r + (p.minor ? 3 : 4), r + 7]; };
const degreeOf = (pc, key) => { const sc = key.mode === "minor" ? MIN_SCALE : MAJ_SCALE; const d = sc.indexOf((pc - key.tonic + 12) % 12); return d < 0 ? null : d + 1; };
const BASS_HELP = { rhythm: "Where bass notes start in the bar: measured from jumps in low-frequency energy (below about 150 Hz), averaged over the section's bars." };
function noteOptions(val, key) {
  const sc = (key.mode === "minor" ? MIN_SCALE : MAJ_SCALE).map((d) => NOTES[(key.tonic + d) % 12]);
  const want = val ? NOTES[pcOf(val)] : "";
  const opt = (n, label) => h("option", { value: n, ...(n === want ? { selected: "" } : {}) }, label);
  return [opt("", "—"), h("optgroup", { label: `In ${NOTES[key.tonic]} ${key.mode}` }, sc.map((n) => opt(n, `${n} · ${degreeOf(NOTES.indexOf(n), key)}`))),
    h("optgroup", { label: "Other notes" }, NOTES.filter((n) => !sc.includes(n)).map((n) => opt(n, n)))];
}
// Piano roll: 4 bars x `bar` steps (16 in 4/4, 12 in 3/4); rows from F1 to E2, widened to fit the notes used. Each bar's note plays on the rhythm's hits.
function bassRoll(m, key, orig, bar = 16) {
  const used = m.notes.filter(Boolean).map((n) => bassMidi(n)).filter((x) => x != null);
  const lo = Math.min(...used, 12 * 2 + 5), hi = Math.max(...used, 12 * 3 + 4);
  const rows = []; for (let p = hi; p >= lo; p--) rows.push(p);
  const RH = 9, W = 4 * bar * 10, H = rows.length * RH, kids = [];
  rows.forEach((p, r) => {
    kids.push(svg("rect", { x: 0, y: r * RH, width: W, height: RH, class: degreeOf(p % 12, key) != null ? "roll-row" : "roll-row off" }));
    if (p % 12 === key.tonic) kids.push(svg("rect", { x: 0, y: r * RH, width: W, height: RH, class: "roll-tonic" }));
  });
  for (let st = 0; st <= 4 * bar; st += 4) kids.push(svg("line", { x1: st * 10, x2: st * 10, y1: 0, y2: H, class: st % bar ? "roll-beat" : "roll-bar" }));
  const r = m.rhythm;
  m.notes.slice(0, 4).forEach((n, b) => {
    const row = rows.indexOf(bassMidi(n));
    if (!n || !r || row < 0) return;
    [...r].forEach((ch, i) => {
      if (ch === ".") return;
      let len = 1; while (i + len < bar && r[i + len] === "." && len < 4) len++;
      const mine = orig && (pcOf(orig.notes[b]) !== pcOf(n) || (orig.rhythm || "")[i] !== ch);
      kids.push(svg("rect", { x: (b * bar + i) * 10 + 1, y: row * RH + 1, width: len * 10 - 2, height: RH - 2, rx: 2, class: "roll-note" + (ch === "o" ? " soft" : "") + (mine ? " mine" : "") }));
    });
  });
  kids.push(svg("rect", { x: -20, y: 0, width: 10, height: H, class: "roll-head" }));
  const labels = h("div", { class: "roll-keys", "aria-hidden": "true", style: `grid-template-rows:repeat(${rows.length},1fr)` },
    rows.map((p) => h("span", { class: p % 12 === key.tonic ? "tonic" : "" }, degreeOf(p % 12, key) ? NOTES[p % 12] : "")));
  return h("div", { class: "roll" }, labels, svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", class: "roll-svg", role: "img", "aria-label": `Bass notes: ${m.notes.map((n) => n || "rest").join(", ")}` }, kids));
}
// One octave, C to B: the chord's root in the dark accent, its other tones in the light accent.
function miniKeys(tones, root) {
  const W = [0, 2, 4, 5, 7, 9, 11], B = [[1, 0], [3, 1], [6, 3], [8, 4], [10, 5]], kids = [];
  W.forEach((pc, i) => kids.push(svg("rect", { x: i * 12, y: 0, width: 12, height: 34, class: "kw" + (tones.includes(pc) ? (pc === root ? " on root" : " on") : "") })));
  B.forEach(([pc, i]) => kids.push(svg("rect", { x: i * 12 + 8, y: 0, width: 8, height: 21, class: "kb" + (tones.includes(pc) ? (pc === root ? " on root" : " on") : "") })));
  return svg("svg", { viewBox: "0 0 84 34", class: "mini-keys", "aria-hidden": "true" }, kids);
}
const PART_TIPS = {
  drums: "Measured from the audio. For each bar in the section, the app takes the jump in loudness at each of 16 steps (12 in 3/4) within each lane's frequency band, then averages across the section's bars.",
  bass: "Measured from the audio. For each bar, the app takes the strongest pitch below about 150 Hz, then finds the 4-bar loop that repeats most often in the section. The rhythm row shows where notes start.",
  chords: "Measured from the audio. For each bar, the app matches the notes it hears against major and minor chords, then finds the 4-bar loop that repeats most often in the section. Bars without a confident match show as a dash.",
};
const flipStep = (s, i) => s.slice(0, i) + (s[i] === "." ? "x" : s[i] === "x" ? "o" : ".") + s.slice(i + 1);

export function grooveView(card, drumsSug, bassSug, harmSug, templates = []) {
  const key = card.core && card.core.key && isNum(card.core.key.tonic) ? card.core.key : null;
  const BAR = barSteps(card), EMPTY = ".".repeat(BAR); // steps per bar: 16 in 4/4, 12 in 3/4
  const parts = [(drumsSug || templates.length) && "drums", bassSug && key && "bass", harmSug && key && "chords"].filter(Boolean);
  const sugOf = { drums: drumsSug, bass: bassSug, chords: harmSug };
  const bySec = (sug) => (sug && Array.isArray(sug.data.by_section) && sug.data.by_section.length ? sug.data.by_section : null);
  const secs = bySec(drumsSug) || bySec(bassSug) || bySec(harmSug);
  const letters = secs ? [...new Set(secs.map((x) => x.letter || x.label))] : [];
  const colorOf = (x) => SEC_COLORS[letters.indexOf(x.letter || x.label) % SEC_COLORS.length];
  // Older cards have no bass/chords by_section: those parts then use the global loop in every section.
  const fromData = {
    drums: (x) => (x ? x.pattern : drumsSug ? drumsSug.data.pattern : {}),
    bass: (x) => { const d = x || bassSug.data; return { notes: [...(d.loop || [])], rhythm: d.rhythm || null }; },
    chords: (x) => { const d = x || harmSug.data; return { chords: [...(d.progression || [])].map((c) => (parseChord(c) ? c : null)), roman: d.roman || null }; },
  };
  const ek = (k) => (secs ? secs[k].idx : "all");
  const baseOf = (part, k) => {
    const list = bySec(sugOf[part]), x = list && secs ? list.find((y) => y.idx === secs[k].idx) : null;
    return fromData[part](x || null);
  };
  const cur = (part, k = sel) => editsFor(card, part)[ek(k)] || baseOf(part, k);
  const edit = (part, fn) => {
    const m = JSON.parse(JSON.stringify(cur(part)));
    fn(m);
    // Back to where it started (the detected part or the picked template): no longer an edit. A picked template stays picked.
    if (JSON.stringify(m) === JSON.stringify(origOf(part)) && !(part === "drums" && srcStore()[ek(sel)] != null)) delete editsFor(card, part)[ek(sel)];
    else editsFor(card, part)[ek(sel)] = m;
    paint(true);
  };
  const edited = () => [...parts, "drumsrc"].some((p) => editsFor(card, p)[ek(sel)] != null);
  const bus = sectionBus;
  let sel = secs ? secs.reduce((best, x, k) => ((x.energy || 0) > (secs[best].energy || 0) ? k : best), 0) : 0, showChg = false, nowStep = -1;
  const on = new Set(parts); // parts that go into the MIDI file (picked in the Export MIDI popover)
  // Solo/mute per drum lane and per part, like a mixer: playback only (never saved or exported). Lasts while the card is open.
  const mute = new Set(), solo = new Set(), pMute = new Set(), pSolo = new Set();
  const audible = (lane) => (solo.size ? solo.has(lane) : !mute.has(lane));
  const partOn = (p) => (pSolo.size ? pSolo.has(p) : !pMute.has(p));
  const smBtns = (soloSet, muteSet, k, name) => [[soloSet, "S", "Solo", "solo"], [muteSet, "M", "Mute", "mute"]].map(([st, label, tip, cls]) => {
    const id = `${soloSet === solo ? "lane" : "part"}-${cls}-${k}`;
    const b = h("button", { type: "button", class: `lane-btn ${cls}`, "aria-pressed": String(st.has(k)), title: `${tip} ${name}`, "aria-label": `${tip}: ${name}`, "data-sm": id }, label);
    // Repainting replaces the button, so focus goes to its replacement.
    b.onclick = () => { if (st.has(k)) st.delete(k); else st.add(k); paint(true); const nb = [...wrap.querySelectorAll("[data-sm]")].find((x) => x.dataset.sm === id); if (nb) nb.focus(); };
    return b;
  });
  const laneCtl = { quiet: (lane) => !audible(lane), el: (lane) => h("span", { class: "lane-sm" }, smBtns(solo, mute, lane, laneName(lane))) };
  const secName = (k = sel) => (secs ? `${cap(secs[k].label)} ${secs[k].letter || ""}`.trim() : "");
  const bars = () => {
    let n = 1;
    if (parts.includes("bass")) n = Math.max(n, cur("bass").notes.length);
    if (parts.includes("chords")) n = Math.max(n, cur("chords").chords.length);
    return Math.max(1, Math.min(4, n));
  };
  const trackBpm = Math.round((drumsSug && drumsSug.data.bpm) || (card.core && card.core.bpm) || 120);
  const wrap = h("div", { class: "sug groove" });

  // transport
  const status = h("span", { class: "seq-status muted", role: "status", "aria-live": "polite" });
  const say = (m) => { status.textContent = m; };
  const playBtn = h("button", { type: "button", class: "seq-play", "aria-pressed": "false" }, "▶ Play");
  const bpmIn = h("input", { type: "number", min: "40", max: "240", step: "1", value: String(trackBpm), "aria-label": "Playback tempo in BPM", class: "seq-bpm" });
  dragNumber(bpmIn);
  const bpmVal = () => Math.max(40, Math.min(240, +bpmIn.value || trackBpm));
  const resetBtn = h("button", { type: "button", class: "seq-reset", hidden: "" }, "Reset edits");
  resetBtn.onclick = () => { [...parts, "drumsrc"].forEach((p) => delete editsFor(card, p)[ek(sel)]); paint(true); say("Edits reset for this section."); };
  const resetAllBtn = h("button", { type: "button", class: "seq-reset", hidden: "", title: "Discard every edit in every section of this track (you can undo)" }, "Reset whole track");
  const ui = (playing) => { playBtn.textContent = playing ? "■ Stop" : "▶ Play"; playBtn.setAttribute("aria-pressed", String(playing)); if (!playing) setNow(-1); };
  const playBass = (ctx, t, st, sd, m) => {
    const r = m.rhythm, b = Math.floor(st / BAR) % Math.max(1, m.notes.length), i = st % BAR;
    if (!r || r[i] === "." || !m.notes[b] || bassMidi(m.notes[b]) == null) return;
    let len = 1; while (i + len < BAR && r[i + len] === "." && len < 4) len++;
    bassVoice(ctx, bassMidi(m.notes[b]), t, sd * len * 0.95, r[i] === "x" ? 1 : 0.6);
  };
  const playChords = (ctx, t, st, sd, m) => {
    if (st % BAR) return;
    const c = m.chords[Math.floor(st / BAR) % Math.max(1, m.chords.length)];
    if (c) padVoice(ctx, chordMidis(c), t, sd * BAR);
  };
  playBtn.onclick = () => {
    if (playBtn.getAttribute("aria-pressed") === "true") { stopAllPlayback(); return; }
    if (!parts.some(partOn)) { say("Everything is muted. Unmute a part to play."); return; }
    try {
      // Reads the current section and edits on every step, so changes (and template swaps) are heard right away.
      startLoop((ctx, t, st, sd) => {
        if (partOn("drums")) for (const [lane, p] of Object.entries(cur("drums"))) if (typeof p === "string" && audible(lane) && p[st % BAR] && p[st % BAR] !== ".") voice(ctx, lane, t, p[st % BAR] === "x" ? 1 : 0.55);
        if (partOn("bass")) playBass(ctx, t, st, sd, cur("bass"));
        if (partOn("chords")) playChords(ctx, t, st, sd, cur("chords"));
      }, bpmVal, 4 * BAR, (st) => setNow(st % (bars() * BAR)), () => ui(false));
      ui(true); say("");
    } catch (e) { say("Audio isn't available in this browser."); }
  };
  let expScope = "section"; // Export MIDI: "section" (the loop shown) or "track" (every section in order, each looping over its bars)
  const wholeTrack = () => !!secs && expScope === "track";
  const fileName = () => `${(card.source.title || "Pattern").replace(/[^\w\- ().]+/g, "").trim() || "Pattern"}${secs && !wholeTrack() ? " - " + secName() : ""}.mid`;
  const exportBtn = h("button", { type: "button", class: "ver-btn", "aria-haspopup": "true", "aria-expanded": "false", title: "Save the parts as one MIDI file, with a track each" }, "Export MIDI ▾");
  // Loop length of section k (like bars(), for any section) and how many bars the section lasts in the track.
  const loopBars = (k) => {
    let n = 1;
    if (parts.includes("bass")) n = Math.max(n, cur("bass", k).notes.length);
    if (parts.includes("chords")) n = Math.max(n, cur("chords", k).chords.length);
    return Math.max(1, Math.min(4, n));
  };
  const secLen = (k) => Math.max(1, (secs[k].end_bar ?? secs[k].start_bar ?? 1) - (secs[k].start_bar ?? 1) + 1);
  const trackBars = () => (secs ? secs.reduce((a, _, k) => a + secLen(k), 0) : bars());
  const doExport = () => {
    const TICKS = BAR * 120, ev = { drums: [], bass: [], chords: [] };
    // One bar of section k, playing bar lb of its loop, starting at tick t0.
    const addBar = (k, lb, t0) => {
      if (on.has("drums")) for (const [lane, p] of Object.entries(cur("drums", k))) if (typeof p === "string") [...p.slice(0, BAR)].forEach((ch, i) => { if (ch !== ".") ev.drums.push({ t: t0 + i * 120, d: 60, note: GM_NOTE(lane), vel: ch === "x" ? 110 : 70 }); });
      if (on.has("bass")) {
        const m = cur("bass", k), nn = m.notes.length ? m.notes[lb % m.notes.length] : null;
        if (nn && m.rhythm && bassMidi(nn) != null) [...m.rhythm].forEach((ch, i) => { if (ch !== ".") ev.bass.push({ t: t0 + i * 120, d: 110, note: bassMidi(nn), vel: ch === "x" ? 110 : 75 }); });
      }
      if (on.has("chords")) {
        const cs = cur("chords", k).chords, c = cs.length ? cs[lb % cs.length] : null;
        if (c) chordMidis(c).forEach((note) => ev.chords.push({ t: t0, d: TICKS - 20, note, vel: 90 }));
      }
    };
    let n = 0;
    if (wholeTrack()) secs.forEach((_, k) => { const L = loopBars(k); for (let b = 0; b < secLen(k); b++) addBar(k, b % L, (n + b) * TICKS); n += secLen(k); });
    else { n = bars(); for (let b = 0; b < n; b++) addBar(sel, b, b * TICKS); }
    const tracks = [["Drums", 9, "drums"], ["Bass", 0, "bass"], ["Chords", 1, "chords"]].filter(([, , p]) => on.has(p)).map(([name, ch, p]) => [name, ch, ev[p]]);
    if (!tracks.length) { say("Pick at least one part to export."); return; }
    const file = fileName();
    downloadBytes(midiMulti(tracks, bpmVal(), n * TICKS, BAR / 4), file, "audio/midi");
    say(`Saved "${file}" (${tracks.map((x) => x[0].toLowerCase()).join(" + ")}, ${bpmVal()} BPM, ${n} bar${n > 1 ? "s" : ""} of ${BAR / 4}/4${wholeTrack() ? `, ${secs.length} sections` : ""}).`);
  };
  // Export picker: choose which parts go into the file (all by default), then save.
  const expPop = h("div", { class: "ver-pop exp-pop", role: "dialog", "aria-label": "Export MIDI", hidden: "" });
  const expWrap = h("span", { class: "ver-wrap" }, exportBtn, expPop);
  const closeExp = (refocus) => { expPop.hidden = true; exportBtn.setAttribute("aria-expanded", "false"); document.removeEventListener("pointerdown", outsideExp, true); if (refocus) exportBtn.focus(); };
  const outsideExp = (e) => { if (!expWrap.contains(e.target)) closeExp(false); };
  const EXP_WHAT = { drums: "General MIDI drum notes", bass: "the bass line", chords: "the chords as triads" };
  function paintExport() {
    const go = h("button", { type: "button", class: "exp-go", ...(on.size ? {} : { disabled: "" }) }, "Export");
    const n = bars(), barsOf = (k) => `${k} bar${k > 1 ? "s" : ""}`;
    expPop.replaceChildren(
      // With sections: this section's loop, or the whole track (each section's loop repeated for as long as it lasts)
      secs ? h("div", { class: "exp-scope", role: "radiogroup", "aria-label": "What to export" }, [["section", `This section (${secName()})`, `${n}-bar loop`], ["track", "Whole track", `all ${secs.length} sections in order, ${barsOf(trackBars())}`]].map(([v, label, note]) => {
        const rb = h("input", { type: "radio", name: `exp-scope-${card.id}`, value: v, ...(expScope === v ? { checked: "" } : {}) });
        rb.onchange = () => { expScope = v; paintExport(); expPop.querySelector(`input[value="${v}"]`).focus(); };
        return h("label", { class: "exp-part" }, rb, h("b", {}, label), h("span", { class: "muted" }, ` · ${note}`));
      })) : null,
      h("p", { class: "muted ver-note" }, `${secs ? "" : `This track · ${barsOf(n)} · `}${bpmVal()} BPM, ${BAR / 4}/4, with your edits. Each part becomes its own track, so it loads into any DAW.${wholeTrack() ? " Each section repeats its loop for as many bars as it lasts." : ""}`),
      h("div", { class: "exp-parts" }, parts.map((p) => {
        const cb = h("input", { type: "checkbox", ...(on.has(p) ? { checked: "" } : {}) });
        cb.onchange = () => { if (cb.checked) on.add(p); else on.delete(p); go.disabled = !on.size; };
        return h("label", { class: "exp-part" }, cb, h("b", {}, cap(p)), h("span", { class: "muted" }, ` · ${EXP_WHAT[p]}`));
      })),
      h("div", { class: "exp-actions" }, go, h("button", { type: "button", class: "linklike", onclick: () => closeExp(true) }, "Cancel")));
    go.onclick = () => { doExport(); closeExp(true); };
  }
  exportBtn.onclick = () => {
    if (!expPop.hidden) { closeExp(true); return; }
    expPop.hidden = false; exportBtn.setAttribute("aria-expanded", "true"); expPop.style.left = "0px"; paintExport();
    // Keep the popover inside the viewport.
    const r = expPop.getBoundingClientRect(), over = r.right - (document.documentElement.clientWidth - 8);
    if (over > 0) expPop.style.left = `${-Math.min(over, r.left - 8)}px`;
    const f = expPop.querySelector("input"); if (f) f.focus();
    document.addEventListener("pointerdown", outsideExp, true);
  };
  expPop.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); closeExp(true); } });

  // Saved versions of this track's edits (the current edits autosave; a version is a named copy to come back to).
  const verBtn = h("button", { type: "button", class: "ver-btn", "aria-haspopup": "true", "aria-expanded": "false" }, "Versions ▾");
  const verPop = h("div", { class: "ver-pop", role: "dialog", "aria-label": "Versions of your edits", hidden: "" });
  const verWrap = h("span", { class: "ver-wrap" }, verBtn, verPop);
  const verUrl = `/api/tracks/${card.id}/edit-versions`;
  let versions = null, verErr = "", undoSnap = null;
  const snap = () => JSON.parse(JSON.stringify(allEdits(card)));
  const restore = (edits) => { partEdits.set(card.id, JSON.parse(JSON.stringify(edits || {}))); paint(true); };
  const fmtDate = (t) => new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const secWord = (n) => `${n} section${n === 1 ? "" : "s"}`;
  const closeVer = (refocus) => { verPop.hidden = true; verBtn.setAttribute("aria-expanded", "false"); document.removeEventListener("pointerdown", outsideVer, true); if (refocus) verBtn.focus(); };
  const outsideVer = (e) => { if (!verWrap.contains(e.target)) closeVer(false); };
  const sayUndo = (msg) => status.replaceChildren(msg + " ", h("button", { type: "button", class: "linklike", onclick: () => { if (undoSnap) restore(undoSnap); undoSnap = null; say("Undone."); } }, "Undo"));
  const loadVersions = async () => {
    try { versions = await api(verUrl); verErr = ""; } catch (e) { verErr = e.message; }
    paintVersions();
  };
  function paintVersions() {
    if (verPop.hidden) return;
    const n = editedKeys(allEdits(card)).length, list = versions || [];
    const name = h("input", { type: "text", class: "ver-name", placeholder: `Version ${list.length + 1}`, "aria-label": "Name for this version", maxlength: "60" });
    const saveV = h("button", { type: "button", ...(n ? {} : { disabled: "" }) }, "Save");
    const doSave = async () => {
      const nm = name.value.trim() || `Version ${list.length + 1}`;
      saveV.disabled = true;
      try { await api(verUrl, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: nm, edits: snap() }) }); say(`Saved “${nm}”.`); }
      catch (e) { say(`Couldn't save the version: ${e.message}`); }
      await loadVersions();
      const f = verPop.querySelector("input"); if (f) f.focus();
    };
    saveV.onclick = doSave;
    name.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); if (n) doSave(); } });
    const restoreV = async (v) => {
      try {
        const full = await api(`${verUrl}/${v.id}`);
        undoSnap = snap(); restore(full.edits); closeVer(true); sayUndo(`Restored “${v.name}”.`);
      } catch (e) { say(`Couldn't restore “${v.name}”: ${e.message}`); }
    };
    const deleteV = async (v) => {
      try { await api(`${verUrl}/${v.id}`, { method: "DELETE" }); say(`Deleted “${v.name}”.`); } catch (e) { say(`Couldn't delete “${v.name}”: ${e.message}`); }
      await loadVersions();
      verBtn.focus();
    };
    verPop.replaceChildren(
      h("p", { class: "muted ver-note" }, n ? `Your edits (${secWord(n)}) save automatically with this track. Save a version to come back to it later.`
        : "No edits yet. Edits save automatically with this track; save a version to keep a copy you can come back to."),
      h("div", { class: "ver-save" }, name, saveV),
      verErr ? h("p", { class: "error ver-note" }, `Couldn't load the saved versions: ${verErr}`)
        : !versions ? h("p", { class: "muted ver-note" }, "Loading saved versions…")
        : list.length ? h("ul", { class: "ver-list", "aria-label": "Saved versions" }, list.map((v) => h("li", {},
          h("span", { class: "ver-meta", title: v.name }, h("b", {}, v.name), h("span", { class: "muted" }, ` · ${fmtDate(v.created_at)} · ${secWord(v.sections)}`)),
          h("button", { type: "button", "aria-label": `Restore ${v.name}`, onclick: () => restoreV(v) }, "Restore"),
          h("button", { type: "button", class: "ver-del", "aria-label": `Delete ${v.name}`, onclick: () => deleteV(v) }, "Delete"))))
        : h("p", { class: "muted ver-note" }, "No saved versions yet."),
      n ? h("button", { type: "button", class: "linklike ver-discard", onclick: () => { undoSnap = snap(); restore({}); closeVer(true); sayUndo("Back to the detected patterns everywhere."); } }, "Discard all edits (back to detected)") : null);
  }
  verBtn.onclick = () => {
    if (!verPop.hidden) { closeVer(true); return; }
    verPop.hidden = false; verBtn.setAttribute("aria-expanded", "true"); verPop.style.left = "0px";
    paintVersions(); loadVersions();
    // Keep the popover inside the viewport.
    const r = verPop.getBoundingClientRect(), over = r.right - (document.documentElement.clientWidth - 8);
    if (over > 0) verPop.style.left = `${-Math.min(over, r.left - 8)}px`;
    const f = verPop.querySelector("input"); if (f) f.focus();
    document.addEventListener("pointerdown", outsideVer, true);
  };
  verPop.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); closeVer(true); } });
  // Goes through the autosave like any edit, so the server copy is cleared too.
  resetAllBtn.onclick = () => { undoSnap = snap(); restore({}); sayUndo("Edits reset in every section."); };
  const tools = h("div", { class: "seq-tools" }, playBtn, h("label", { class: "seq-tempo" }, bpmIn, h("span", {}, "BPM")), expWrap, verWrap, resetBtn, resetAllBtn, status);

  // The playhead lights the drum step, the bass roll column and the current bass note / chord bar.
  function setNow(st) {
    nowStep = st;
    wrap.querySelectorAll(".now").forEach((el) => el.classList.remove("now"));
    if (st < 0) return;
    wrap.querySelectorAll(`.drum-grid i[data-step="${st % BAR}"]`).forEach((el) => el.classList.add("now"));
    wrap.querySelectorAll(`[data-bar="${Math.floor(st / BAR)}"]`).forEach((el) => el.classList.add("now"));
    const ph = wrap.querySelector(".roll-head"); if (ph) { ph.setAttribute("x", String(st * 10)); ph.classList.add("now"); }
  }

  // the three parts; a part with edits in this section gets a "N edits · Revert" pill (Revert resets only that part)
  const revert = (part) => {
    delete editsFor(card, part)[ek(sel)];
    if (part === "drums") delete srcStore()[ek(sel)];
    paint(true);
    say(`${cap(part)} back to ${part === "drums" && !drumsSug ? "no drums" : "detected"} for ${secName() || "this track"}.`);
  };
  const panel = (part, title, kids, foot, nEdits) => h("div", { class: "part" + (nEdits ? " has-edits" : "") + (partOn(part) ? "" : " quiet"), "data-part": part },
    h("div", { class: "part-head" }, h("span", { class: "part-sm" }, smBtns(pSolo, pMute, part, part)), h("b", { class: "tip-title", tabindex: "0", "data-tip": PART_TIPS[part] }, title), foot ? h("span", { class: "muted part-foot" }, foot) : null,
      nEdits ? h("span", { class: "edit-pill" }, nEdits === true ? "Edited" : `${nEdits} edit${nEdits > 1 ? "s" : ""}`, " · ",
        h("button", { type: "button", class: "linklike", "aria-label": `Revert the ${part} in ${secName() || "this track"}`, onclick: () => revert(part) }, "Revert")) : null), markRow(part, nEdits), ...kids);
  // Your verdict on what was detected (pattern reviews, tastr/labels.py): Right, Close or Way off per part and section,
  // and your edit saved as the right pattern, which a pattern benchmark scores the engine against. The server keys them
  // by the section's bars, so they outlive a re-analysis; one whose pattern a re-analysis changed comes back "stale".
  let marks = [];
  const barsAt = (k) => (secs ? [secs[k].start_bar, secs[k].end_bar] : [0, 0]);
  const markOf = (part, k = sel) => { const [a, b] = barsAt(k); return marks.find((m) => m.part === part && m.start_bar === a && m.end_bar === b) || null; };
  const truthShape = (part, m) => (part === "drums" ? Object.fromEntries(Object.entries(m).filter(([, v]) => typeof v === "string"))
    : part === "bass" ? { notes: m.notes.map((x) => x || null), rhythm: m.rhythm || null } : { chords: m.chords.map((x) => x || null) });
  const putMark = async (part, change, k = sel) => {
    const m = markOf(part, k) || {};
    const body = { part, section: ek(k), verdict: m.verdict || null, truth: m.truth || null, note: m.note || null, ...change };
    marks = await api(`/api/tracks/${card.id}/pattern-reviews`, JSON_PUT(body));
  };
  const mark = async (part, change, said) => {
    try { await putMark(part, change); paint(true); say(said); loadReview(); } catch (e) { say(e.message); }
  };
  const rightEverywhere = async (part) => {
    try { for (let k = 0; k < secs.length; k++) if (!(markOf(part, k) || {}).verdict) await putMark(part, { verdict: "right" }, k); } catch (e) { say(e.message); return; }
    paint(true); say(`${cap(part)} marked right in every section you hadn't marked.`); loadReview();
  };
  if (card.id != null) api(`/api/tracks/${card.id}/pattern-reviews`).then((r) => { marks = r || []; paint(true); }).catch(() => {}); // an older server: no marks
  const VERDICTS = [["right", "✓ Right", "right", "What was detected is right"], ["close", "≈ Close", "close", "Close: usable with small changes"], ["way_off", "✗ Way off", "way off", "Way off: what was detected is wrong"]];
  function markRow(part, nEdits) {
    if (card.id == null) return null;
    const m = markOf(part), where = secName() || "this track";
    const btns = VERDICTS.map(([v, label, word, tip]) => {
      const on = !!m && m.verdict === v;
      return h("button", { type: "button", class: `mark-btn ${v}`, "aria-pressed": String(on), title: `${tip} (${where})`,
        onclick: () => mark(part, { verdict: on ? null : v }, on ? `Mark taken off the ${part} in ${where}.` : `Detected ${part} in ${where} marked ${word}.`) }, label);
    });
    const mine = truthShape(part, cur(part)), same = m && m.truth && JSON.stringify(m.truth) === JSON.stringify(mine);
    const truthEl = nEdits && !same
      ? h("button", { type: "button", class: "linklike", title: "Keep your edit as the right answer for this section. The pattern benchmark scores the engine against it.",
        onclick: () => mark(part, { truth: mine }, `Your ${part} edit is saved as the right pattern for ${where}.`) }, m && m.truth ? "Save my edit as the right pattern again" : "Save my edit as the right pattern")
      : m && m.truth ? h("span", { class: "mark-truth" }, same ? "Your edit is saved as the right pattern · " : "Right pattern saved · ",
        h("button", { type: "button", class: "linklike", "aria-label": `Remove the right ${part} pattern saved for ${where}`, onclick: () => mark(part, { truth: null }, `Right ${part} pattern removed for ${where}.`) }, "Remove")) : null;
    return h("div", { class: "mark-row" }, h("span", { class: "muted" }, "Detected:"),
      h("span", { class: "mark-btns", role: "group", "aria-label": `Is the detected ${part} right in ${where}?` }, btns),
      m && m.stale ? h("span", { class: "mark-stale", title: "A re-analysis changed this pattern after you marked it. Mark it again." }, "marked on an older analysis") : null,
      secs && secs.length > 1 ? h("button", { type: "button", class: "linklike mark-all", title: `Mark the detected ${part} right in every section you haven't marked yet`, onclick: () => rightEverywhere(part) }, "Right everywhere else") : null,
      truthEl);
  }
  // Picking a template copies it into the section's drum edits, so playback picks it up on the next step.
  const srcStore = () => editsFor(card, "drumsrc"); // a function: restoring a version replaces the edits object
  // What the user's edits are measured against: the picked template, or what was detected.
  const drumsOrig = () => { const v = srcStore()[ek(sel)]; return v != null && templates[v] ? templates[v].data.pattern : baseOf("drums", sel); };
  const origOf = (part) => (part === "drums" ? drumsOrig() : baseOf(part, sel));
  const stepDiffs = (a, b) => { let n = 0; for (const lane of new Set([...Object.keys(a), ...Object.keys(b)])) for (let i = 0; i < BAR; i++) if (((a[lane] || "")[i] || ".") !== ((b[lane] || "")[i] || ".")) n++; return n; };
  const tplName = (t) => t.title.replace(/^Pattern idea: /, "");
  const useTemplate = (v, all) => {
    const keys = all && secs ? secs.map((_, k) => ek(k)) : [ek(sel)];
    for (const kk of keys) {
      if (v === "") { delete srcStore()[kk]; delete editsFor(card, "drums")[kk]; }
      else { srcStore()[kk] = v; editsFor(card, "drums")[kk] = JSON.parse(JSON.stringify(templates[v].data.pattern)); }
    }
    paint(true);
    const name = v === "" ? (drumsSug ? "the detected drums" : "no drums") : tplName(templates[v]);
    say(all ? `Every section now uses ${name}.` : `${secName() || "The track"} now uses ${name}.`);
  };
  const drumsPicker = () => {
    if (!templates.length) return null;
    const v = srcStore()[ek(sel)], t = v != null ? templates[v] || null : null;
    const pickEl = h("select", { class: "drum-src", "aria-label": "Drum pattern for this section", onchange: (e) => useTemplate(e.target.value, false) },
      h("option", { value: "", ...(t ? {} : { selected: "" }) }, drumsSug ? "Detected groove" : "None detected"),
      h("optgroup", { label: "Pattern ideas (by tempo)" }, templates.map((x, i) => h("option", { value: String(i), ...(String(i) === String(v) ? { selected: "" } : {}) }, tplName(x)))));
    const isEdited = t && JSON.stringify(cur("drums")) !== JSON.stringify(t.data.pattern);
    return h("div", { class: "drum-src-row" }, h("label", { class: "seq-tempo" }, h("span", {}, "Pattern"), pickEl),
      t ? h("span", { class: "origin tpl" }, "Template for ", h("span", { class: "term", tabindex: "0", "data-tip": styleTip(t.data) }, t.data.style || "this tempo"), isEdited ? ", edited" : "") : null,
      secs ? h("button", { type: "button", class: "linklike", onclick: () => useTemplate(v != null ? String(v) : "", true) }, t ? "Use in every section" : drumsSug ? "Use detected in every section" : "Use no drums in every section") : null);
  };
  const drumsPanel = (prev) => {
    const tpl = srcStore()[ek(sel)] != null, pat = cur("drums"), orig = drumsOrig();
    const lanes = laneRows(pat, showChg && !tpl ? prev : null, !tpl && !!drumsSug, (lane, i) => {
      edit("drums", (m) => { m[lane] = flipStep(m[lane], i); });
      const el = wrap.querySelector(`.drum-grid i[data-lane="${lane}"][data-step="${i}"]`); if (el) el.focus();
    }, drumsSug && drumsSug.data.lane_help, orig, laneCtl);
    return panel("drums", "Drums", [drumsPicker(), ...(lanes.length ? lanes : [h("p", { class: "muted part-empty" }, "No drums detected. Pick a pattern idea to start from one.")])],
      `1 bar, ${BAR} steps; click any step to edit`, stepDiffs(pat, orig) || (tpl ? true : 0));
  };
  const bassPanel = (prev) => {
    const m = cur("bass"), has = m.notes.some(Boolean) && m.rhythm, slots = m.notes.length ? m.notes.slice(0, 4) : [null, null, null, null];
    const o = baseOf("bass", sel), oRhythm = o.rhythm || EMPTY;
    const noteMine = (n, b) => pcOf(o.notes[b]) !== pcOf(n);
    const nEdits = slots.filter(noteMine).length + [...(m.rhythm || EMPTY)].filter((ch, i) => ch !== oRhythm[i]).length;
    const noteRow = h("div", { class: "bar-picks" }, slots.map((n, b) => h("label", { class: "bar-pick" + (noteMine(n, b) ? " mine" : ""), "data-bar": b,
      title: noteMine(n, b) ? `Your edit (detected: ${o.notes[b] || "none"})` : null },
      h("span", { class: "muted" }, `Bar ${b + 1}`, noteMine(n, b) ? h("span", { class: "mine-tag" }, " · edited")
        : prev && prev.notes.some(Boolean) && pcOf(prev.notes[b]) !== pcOf(n) ? h("span", { class: "chg-tag", title: `Δ = different from the previous section (${secName(sel - 1)}). Not an edit.` }, ` · Δ ${secName(sel - 1)}`) : null),
      h("select", { "aria-label": `Bass note, bar ${b + 1}`, onchange: (e) => edit("bass", (x) => {
        if (!x.notes.length) x.notes = [null, null, null, null];
        x.notes[b] = e.target.value || null;
        if (!x.rhythm && e.target.value) x.rhythm = BAR === 16 ? "x.......x......." : "x" + ".".repeat(BAR - 1); // beats 1 and 3, or 1 in 3/4
      }) }, noteOptions(n, key)))));
    return panel("bass", "Bass", [
      has ? bassRoll(m, key, o, BAR) : h("p", { class: "muted part-empty" }, "No bass in this section. Pick a note for a bar to add one."),
      noteRow,
      ...laneRows({ rhythm: m.rhythm || EMPTY }, showChg && prev && prev.rhythm ? { rhythm: prev.rhythm } : null, true, (lane, i) => edit("bass", (x) => {
        x.rhythm = flipStep(x.rhythm || EMPTY, i);
        if (!x.notes.some(Boolean)) x.notes = [0, 1, 2, 3].map(() => NOTES[key.tonic]);
      }), BASS_HELP, { rhythm: oRhythm }),
    ], "One note per bar; the rhythm repeats every bar", nEdits);
  };
  const chordsPanel = (prev) => {
    const m = cur("chords"), isEdited = !!editsFor(card, "chords")[ek(sel)];
    const sc = key.mode === "minor" ? MIN_SCALE : MAJ_SCALE, q = key.mode === "minor" ? [1, 0, 0, 1, 1, 0, 0] : [0, 1, 1, 0, 0, 1, 0];
    // Diatonic major and minor triads, leaving out the diminished one.
    const diat = sc.map((d, i) => NOTES[(key.tonic + d) % 12] + (q[i] ? "m" : "")).filter((n, i) => !(key.mode === "minor" ? i === 1 : i === 6));
    const slots = m.chords.length ? m.chords.slice(0, 4) : [null, null, null, null];
    const o = baseOf("chords", sel), chordMine = (c, b) => (o.chords[b] || null) !== (c || null);
    const nEdits = slots.filter(chordMine).length;
    const cells = slots.map((c, b) => {
      const p = parseChord(c), changed = prev && prev.chords.some(Boolean) && prev.chords[b] !== c, mine = chordMine(c, b);
      return h("div", { class: "chord-cell" + (p ? "" : " none") + (mine ? " mine" : ""), "data-bar": b, title: mine ? `Your edit (detected: ${o.chords[b] || "no clear chord"})` : null },
        h("span", { class: "muted chord-bar" }, `Bar ${b + 1}`, mine ? h("span", { class: "mine-tag" }, " · edited") : changed ? h("span", { class: "chg-tag", title: `Δ = different from the previous section (${secName(sel - 1)}). Not an edit.` }, ` · Δ ${secName(sel - 1)}`) : null),
        h("select", { class: "chord-pick", "aria-label": `Chord, bar ${b + 1}`, onchange: (e) => edit("chords", (x) => { if (!x.chords.length) x.chords = [null, null, null, null]; x.chords[b] = e.target.value || null; }) },
          h("option", { value: "", ...(c ? {} : { selected: "" }) }, "—"),
          [...new Set([...(c ? [c] : []), ...diat])].map((n) => h("option", { value: n, ...(n === c ? { selected: "" } : {}) }, n))),
        h("span", { class: "chord-roman" }, p ? (!isEdited && m.roman && m.roman[b]) || romanOf(c, key.tonic, key.mode) : "no clear chord"),
        p ? miniKeys(chordTones(c), p.root) : null,
        h("span", { class: "muted chord-tones" }, p ? chordTones(c).map((t) => NOTES[t]).join(" · ") : ""));
    });
    return panel("chords", "Chords", [m.chords.some(Boolean) ? null : h("p", { class: "muted part-empty" }, "No clear chords in this section. Pick one for a bar to add it."), h("div", { class: "chord-row" }, cells)].filter(Boolean),
      "One chord per bar, root in the darker key", nEdits);
  };
  // What changed against the previous section, per part ("" when nothing did).
  const partDiff = (part, m, p) => {
    if (part === "drums") { if (srcStore()[ek(sel)] != null) return ""; const t = diffText(m, p); return t === "same as previous section" ? "" : t; }
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
  };
  const body = h("div", { class: "sec-body groove-parts" });
  const tabs = h("div", { class: "sec-tabs", role: "tablist", "aria-label": "Section" });
  const note = h("div", { class: "sec-note" });
  const toggle = h("input", { type: "checkbox" }); // Δ outlines start off
  toggle.addEventListener("change", () => { showChg = toggle.checked; paint(true); });
  function paint(quiet) {
    saveEdits(card);
    bus.dispatchEvent(new CustomEvent("edits")); // the Strudel sketch regenerates from these
    resetBtn.hidden = !edited();
    resetBtn.textContent = secs ? "Reset this section" : "Reset edits";
    const editedNow = new Set(editedKeys(allEdits(card)));
    resetAllBtn.hidden = !editedNow.size;
    [...tabs.children].forEach((b, k) => b.classList.toggle("edited", editedNow.has(String(ek(k)))));
    paintVersions();
    const prevOf = (part) => (secs && sel > 0 ? baseOf(part, sel - 1) : null);
    body.replaceChildren(...parts.map((p) => (p === "drums" ? drumsPanel(prevOf(p)) : p === "bass" ? bassPanel(prevOf(p)) : chordsPanel(prevOf(p)))));
    setNow(nowStep);
    if (!secs) return;
    [...tabs.children].forEach((b, k) => b.setAttribute("aria-selected", String(k === sel)));
    const sec = secs[sel], pv = sel > 0 ? secs[sel - 1] : null;
    const diff = pv ? parts.map((p) => partDiff(p, cur(p), baseOf(p, sel - 1))).filter(Boolean).join("; ") || "same as previous section" : "";
    note.replaceChildren(h("b", {}, secName()), h("span", { class: "muted" }, ` · bars ${sec.start_bar}-${sec.end_bar}`),
      h("div", { class: "muted sec-diff" }, pv
        ? ["Compared with ", h("button", { type: "button", class: "sec-link", title: "Go to this section", style: `--c:${colorOf(pv)}`, onclick: () => { sel -= 1; paint(); } }, secName(sel - 1)), `: ${diff}`]
        : "First section, nothing before it to compare with"));
    if (quiet !== true) bus.dispatchEvent(new CustomEvent("section", { detail: { idx: sec.idx, from: "groove" } }));
  }
  const mineKey = h("span", { tabindex: "0", "data-tip": "A blue ring marks a step, note or chord you changed from what was detected (or from the picked pattern idea). The fill still shows how loud the step is now, so a ring around an empty step is a hit you removed. Hover one to see what it was." }, h("i", { class: "sw mine" }), "Your edit");
  // One legend above all parts: its states apply to the drums, the bass rhythm and edits everywhere.
  const foot = h("div", { class: "grid-foot" }, h("div", { class: "grid-legend" }, legend(true, BAR, !!secs), mineKey), secs ? h("label", { class: "chg-toggle" }, toggle, "Outline Δ from the previous section") : null);
  if (secs) {
    bus.addEventListener("section", (e) => {
      if (e.detail.from !== "chart") return;
      const k = secs.findIndex((x) => x.idx === e.detail.idx);
      if (k >= 0) { sel = k; paint(true); }
    });
    secs.forEach((x, k) => tabs.append(h("button", { type: "button", role: "tab", class: "sec-tab", style: `--c:${colorOf(x)}`, onclick: () => { sel = k; paint(); } }, secName(k))));
    wrap.append(tabs, note);
  }
  wrap.append(tools, foot, body);
  paint(true);
  return wrap;
}

// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  window.addEventListener("pagehide", () => [...saveTimers.keys()].forEach((id) => putEdits(id, true)));
}
