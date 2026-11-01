// Notes, chords and keys for the groove view: parsing names, roman numerals, MIDI notes, and the bass roll and mini keyboard.

import { NOTES, h, svg } from "./core.js";

export const MAJ_SCALE = [0, 2, 4, 5, 7, 9, 11], MIN_SCALE = [0, 2, 3, 5, 7, 8, 10];
// Numerals count from the key's own scale, like the engine (music.roman_numeral): in A minor, F is VI.
// ♭/♯ only mark chords from outside the key.
const ROMAN = { major: ["I", "♭II", "II", "♭III", "III", "IV", "♯IV", "V", "♭VI", "VI", "♭VII", "VII"],
  minor: ["I", "♭II", "II", "III", "♯III", "IV", "♯IV", "V", "VI", "♯VI", "VII", "♯VII"] };
// Note names can use sharps or flats (the engine writes bass notes like "Ab").
export const pcOf = (n) => { const m = /^([A-G])([#b]?)/.exec(n || ""); return m ? (NOTES.indexOf(m[1]) + (m[2] === "#" ? 1 : m[2] === "b" ? 11 : 0)) % 12 : -1; };
export const parseChord = (name) => { const m = /^([A-G][#b]?)(m(?!aj))?/.exec(name || ""); return m ? { root: pcOf(m[1]), minor: !!m[2] } : null; };
export const chordTones = (name) => { const p = parseChord(name); return p ? [0, p.minor ? 3 : 4, 7].map((d) => (p.root + d) % 12) : []; };
export const romanOf = (name, tonic, mode) => { const p = parseChord(name); if (!p) return ""; const r = ROMAN[mode === "minor" ? "minor" : "major"][(p.root - tonic + 12) % 12]; return p.minor ? r.toLowerCase() : r; };
export const bassMidi = (n) => { const i = pcOf(n); return i < 0 ? null : 12 * (i <= 4 ? 3 : 2) + i; }; // C2..E2, F1..B1
export const chordMidis = (name) => { const p = parseChord(name); if (!p) return []; const r = 48 + p.root; return [r, r + (p.minor ? 3 : 4), r + 7]; };
const degreeOf = (pc, key) => { const sc = key.mode === "minor" ? MIN_SCALE : MAJ_SCALE; const d = sc.indexOf((pc - key.tonic + 12) % 12); return d < 0 ? null : d + 1; };
export const BASS_HELP = { rhythm: "Where bass notes start in the bar: measured from jumps in low-frequency energy (below about 150 Hz), averaged over the section's bars." };
export function noteOptions(val, key) {
  const sc = (key.mode === "minor" ? MIN_SCALE : MAJ_SCALE).map((d) => NOTES[(key.tonic + d) % 12]);
  const want = val ? NOTES[pcOf(val)] : "";
  const opt = (n, label) => h("option", { value: n, ...(n === want ? { selected: "" } : {}) }, label);
  return [opt("", "—"), h("optgroup", { label: `In ${NOTES[key.tonic]} ${key.mode}` }, sc.map((n) => opt(n, `${n} · ${degreeOf(NOTES.indexOf(n), key)}`))),
    h("optgroup", { label: "Other notes" }, NOTES.filter((n) => !sc.includes(n)).map((n) => opt(n, n)))];
}
// Piano roll: 4 bars x `bar` steps (16 in 4/4, 12 in 3/4); rows from F1 to E2, widened to fit the notes used. Each bar's note plays on the rhythm's hits.
export function bassRoll(m, key, orig, bar = 16) {
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
export function miniKeys(tones, root) {
  const W = [0, 2, 4, 5, 7, 9, 11], B = [[1, 0], [3, 1], [6, 3], [8, 4], [10, 5]], kids = [];
  W.forEach((pc, i) => kids.push(svg("rect", { x: i * 12, y: 0, width: 12, height: 34, class: "kw" + (tones.includes(pc) ? (pc === root ? " on root" : " on") : "") })));
  B.forEach(([pc, i]) => kids.push(svg("rect", { x: i * 12 + 8, y: 0, width: 8, height: 21, class: "kb" + (tones.includes(pc) ? (pc === root ? " on root" : " on") : "") })));
  return svg("svg", { viewBox: "0 0 84 34", class: "mini-keys", "aria-hidden": "true" }, kids);
}
export const flipStep = (s, i) => s.slice(0, i) + (s[i] === "." ? "x" : s[i] === "x" ? "o" : ".") + s.slice(i + 1);
