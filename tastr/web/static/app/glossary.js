// The glossary: Camelot wheel, scales to hear, benchmark numbers.

import { $, NOTES, api, cap, currentId, h, isNum, svg } from "./core.js";
import { shownCard, switchTo } from "./card.js";
import { accid } from "./daw.js";
import { laneRows } from "./groove-lanes.js";
import { audioCtx, stopAllPlayback } from "./sequencer.js";

// openGlossary switches to the Glossary and scrolls to an anchor, with the wheel and Hear it set to the open track's key.
export function openGlossary(anchor) {
  switchTo("glossary");
  const key = shownCard && shownCard.id === currentId && shownCard.core && shownCard.core.key;
  if (key && key.camelot) camPick(key.camelot);
  if (key && isNum(key.tonic)) { glKey.value = key.tonic; glScale.value = key.mode === "minor" ? "Minor" : "Major"; }
  const el = anchor && document.getElementById(anchor);
  if (el) el.scrollIntoView({ block: "start" }); else window.scrollTo(0, 0);
}

// Benchmark scores and pattern ideas come from the engine (GET /api/bench, /api/patterns) the first time the
// Glossary opens; `tastr bench ... --record` keeps the scores current. If a fetch fails, the HTML's numbers stay.
let glossaryLoaded = false;
export async function loadGlossaryData() {
  if (glossaryLoaded) return;
  glossaryLoaded = true;
  const [bench, pats] = await Promise.allSettled([api("/api/bench"), api("/api/patterns")]);
  if (bench.status === "fulfilled") fillBench(bench.value);
  $("gl-patterns").replaceChildren(...(pats.status === "fulfilled" ? pats.value.map((p) =>
    h("figure", { class: "gl-pat" }, h("figcaption", {}, h("b", {}, p.name), h("span", { class: "muted" }, ` · ${cap(p.style)}`)), h("div", { class: "drum-grid-wrap" }, laneRows(p.pattern, null, false))))
    : [h("p", { class: "muted" }, "Couldn't load the pattern ideas.")]));
}
const benchDate = (d) => new Date(d + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
function fillBench(data) {
  const scores = (data && data.scores) || {};
  document.querySelectorAll("[data-bench]").forEach((el) => {
    const r = scores[el.dataset.bench];
    if (r) { el.textContent = `${Math.round(r.exact * 100)}%`; el.title = `${r.tracks} ${r.what || "tracks, exact key"} · benchmark run ${benchDate(r.run)}`; }
  });
  // A sentence's date tag shows the newest run among the scores it quotes.
  document.querySelectorAll("[data-bench-asof]").forEach((el) => {
    const runs = el.dataset.benchAsof.split(" ").map((k) => scores[k] && scores[k].run).filter(Boolean).sort();
    if (runs.length) { el.textContent = ` (benchmark run ${benchDate(runs[runs.length - 1])})`; el.title = "Updates automatically after each milestone benchmark run"; }
  });
}

// Camelot wheel: 12 at the top, clockwise; B (major) outside, A (minor) inside. Click a key to see its neighbors.
const CAM_KEYS = { B: ["B", "F#", "Db", "Ab", "Eb", "Bb", "F", "C", "G", "D", "A", "E"], A: ["Abm", "Ebm", "Bbm", "Fm", "Cm", "Gm", "Dm", "Am", "Em", "Bm", "F#m", "C#m"] };
function camelotWheel(host, note) {
  const R = [52, 86, 120], C = 124;
  const pt = (r, a) => [C + r * Math.sin(a), C - r * Math.cos(a)];
  const arc = (r0, r1, a0, a1) => { const [x0, y0] = pt(r1, a0), [x1, y1] = pt(r1, a1), [x2, y2] = pt(r0, a1), [x3, y3] = pt(r0, a0);
    return `M${x0} ${y0}A${r1} ${r1} 0 0 1 ${x1} ${y1}L${x2} ${y2}A${r0} ${r0} 0 0 0 ${x3} ${y3}Z`; };
  const cells = {};
  const root = svg("svg", { viewBox: "0 0 248 248", role: "img", "aria-label": "Camelot wheel" });
  for (const ring of ["B", "A"]) for (let n = 1; n <= 12; n++) {
    const a = ((n % 12) / 12) * 2 * Math.PI, a0 = a - Math.PI / 12, a1 = a + Math.PI / 12;
    const [r0, r1] = ring === "B" ? [R[1], R[2]] : [R[0], R[1]];
    const [tx, ty] = pt((r0 + r1) / 2, a), id = n + ring;
    const g = svg("g", { class: "cam-cell", tabindex: "0", role: "button", "aria-label": `${id}, ${CAM_KEYS[ring][n - 1]}` },
      svg("path", { d: arc(r0, r1, a0, a1) }),
      svg("text", { x: tx, y: ty - 3, class: "cam-id" }, id),
      svg("text", { x: tx, y: ty + 8, class: "cam-key" }, accid(CAM_KEYS[ring][n - 1])));
    g.addEventListener("click", () => pick(id));
    g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(id); } });
    cells[id] = g; root.append(g);
  }
  root.append(svg("text", { x: C, y: C + 4, class: "cam-mid" }, "Camelot"));
  function pick(id) {
    if (!cells[id]) return;
    const n = parseInt(id, 10), ring = id.slice(-1), other = ring === "A" ? "B" : "A";
    const near = [`${(n + 10) % 12 + 1}${ring}`, `${n % 12 + 1}${ring}`, `${n}${other}`];
    for (const [k, g] of Object.entries(cells)) g.setAttribute("class", "cam-cell" + (k === id ? " sel" : near.includes(k) ? " near" : ""));
    note.textContent = `${id} (${accid(CAM_KEYS[ring][n - 1])}) mixes with ${near.map((k) => `${k} (${accid(CAM_KEYS[k.slice(-1)][parseInt(k, 10) - 1])})`).join(", ")}.`;
  }
  host.replaceChildren(root);
  return pick;
}
const camPick = camelotWheel($("gl-wheel"), $("gl-cam-note"));

// Hear a scale: pick a key and scale, play it up and down, or its tonic chord. Clicking a row of the modes table plays it.
const GL_SCALES = { "Major": [0, 2, 4, 5, 7, 9, 11], "Lydian": [0, 2, 4, 6, 7, 9, 11], "Mixolydian": [0, 2, 4, 5, 7, 9, 10],
  "Minor": [0, 2, 3, 5, 7, 8, 10], "Dorian": [0, 2, 3, 5, 7, 9, 10], "Phrygian": [0, 1, 3, 5, 7, 8, 10],
  "Harmonic Minor": [0, 2, 3, 5, 7, 8, 11], "Locrian": [0, 1, 3, 5, 6, 8, 10] };
const glKey = h("select", { "aria-label": "Key" }, NOTES.map((n, i) => h("option", { value: i }, n)));
const glScale = h("select", { "aria-label": "Scale" }, Object.keys(GL_SCALES).map((n) => h("option", { value: n }, n)));
function glTone(ctx, midi, t, dur) {
  const o = ctx.createOscillator(), g = ctx.createGain();
  o.type = "triangle"; o.frequency.value = 440 * 2 ** ((midi - 69) / 12);
  g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(0.18, t + 0.01); g.gain.exponentialRampToValueAtTime(0.001, t + dur);
  o.connect(g).connect(ctx.destination); o.start(t); o.stop(t + dur + 0.05);
}
function glPlay(kind) {
  stopAllPlayback();
  const ctx = audioCtx(); if (ctx.state === "suspended") ctx.resume();
  const root = 60 + Number(glKey.value), steps = GL_SCALES[glScale.value], t0 = ctx.currentTime + 0.05;
  if (kind === "chord") { [0, 2, 4].forEach((d) => glTone(ctx, root + steps[d], t0, 1.6)); glTone(ctx, root - 12, t0, 1.6); return; }
  const seq = [...steps, 12, ...steps.slice().reverse()];
  seq.forEach((iv, k) => glTone(ctx, root + iv, t0 + k * 0.22, k === seq.length - 1 ? 0.8 : 0.3));
}

// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  document.querySelectorAll(".gl-toc a").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); openGlossary(a.getAttribute("href").slice(1)); }));
  camPick("8A");
  $("gl-hear").append(h("span", { class: "muted" }, "Hear it:"), glKey, glScale,
    h("button", { type: "button", onclick: () => glPlay("scale") }, "▶ Scale"),
    h("button", { type: "button", onclick: () => glPlay("chord") }, "▶ Chord"));
  // The class is gl-row, not pick: .pick is the select-mode checkbox.
  document.querySelectorAll(".gl-modes tbody tr").forEach((tr) => {
    const name = tr.cells[0].textContent.replace(/ \(.*\)/, "");
    if (!GL_SCALES[name]) return;
    tr.classList.add("gl-row"); tr.title = `Hear ${name}`;
    tr.addEventListener("click", () => { glScale.value = name; glPlay("scale"); });
  });
  if (location.hash === "#glossary") switchTo("glossary");
}
