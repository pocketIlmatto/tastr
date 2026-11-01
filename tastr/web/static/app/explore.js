// Explore: the vibe scatter plot.

import { $, h, isNum, setTagsByTrack, svg, tagsByTrack, tracks } from "./core.js";
import { loadTracks } from "./library.js";
import { openTrack } from "./card.js";
import { loadSaved, qMode, renderSimple, setQMode } from "./find.js";

function loadTags() {
  // /api/tracks already lists each track's tags, so this works where the SQL box is turned off.
  setTagsByTrack({});
  for (const t of tracks) if (t.tags && t.tags.length) tagsByTrack[t.id] = new Set(t.tags);
  const all = [...new Set(Object.values(tagsByTrack).flatMap((s) => [...s]))].sort();
  const sel = $("f-tag"), cur = sel.value;
  sel.replaceChildren(h("option", { value: "" }, "any"), ...all.map((t) => h("option", { value: t }, t)));
  sel.value = all.includes(cur) ? cur : "";
}

let findReady = false;
export async function renderExplore() {
  await loadTracks();
  loadTags();
  drawScatter();
  // Find tracks: the first visit sets it up; later ones refresh Simple's results (the library may have changed)
  if (!findReady) { findReady = true; setQMode(qMode); loadSaved(); } else if (qMode === "simple") renderSimple();
}

function drawScatter() {
  const tag = $("f-tag").value;
  const min = parseFloat($("f-min").value), max = parseFloat($("f-max").value);
  const pts = tracks.filter((t) => isNum(t.bpm) && isNum(t.energy)
    && (!tag || (tagsByTrack[t.id] && tagsByTrack[t.id].has(tag)))
    && (isNaN(min) || t.bpm >= min) && (isNaN(max) || t.bpm <= max));
  const W = 700, H = 320, L = 36, R = 12, T = 12, B = 30;
  const lo = Math.min(60, ...pts.map((p) => p.bpm)), hi = Math.max(180, ...pts.map((p) => p.bpm));
  const x = (b) => L + ((b - lo) / (hi - lo)) * (W - L - R);
  const y = (e) => T + (1 - e) * (H - T - B);
  const s = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "BPM versus energy" });
  for (const v of [0, 0.5, 1]) s.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: "var(--line)" }), svg("text", { x: 4, y: y(v) + 4 }, document.createTextNode(v)));
  for (let b = Math.ceil(lo / 20) * 20; b <= hi; b += 20) s.append(svg("text", { x: x(b) - 8, y: H - 10 }, document.createTextNode(b)));
  s.append(svg("text", { x: W / 2 - 10, y: H - 0 }, document.createTextNode("BPM")));
  for (const p of pts) {
    const c = svg("circle", { cx: x(p.bpm), cy: y(p.energy), r: 7, fill: "var(--accent)", opacity: 0.75, style: "cursor:pointer" },
      svg("title", {}, document.createTextNode(`${p.title || p.id} — ${p.bpm.toFixed(1)} BPM, energy ${p.energy.toFixed(2)}`)));
    c.addEventListener("click", () => openTrack(p.id));
    s.append(c);
  }
  $("scatter-wrap").replaceChildren(s, h("p", { class: "muted" }, `${pts.length} of ${tracks.length} tracks shown. Click a point to open its card.`));
}

// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  ["f-tag", "f-min", "f-max"].forEach((id) => $(id).addEventListener("input", drawScatter));
}
