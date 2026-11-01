// Explore › Blend: group your tracks in a weighted space of measurements, find the tracks between two songs,
// and compare saved lists. All in the browser, from the track list and /api/vibe-scores; the math is in blend-space.js.

import { $, api, h, svg, tracks } from "./core.js";
import { openTrack, switchTo } from "./card.js";
import { deleteList, lists, loadLists, onListsChange, saveList } from "./lists.js";
import { BL_DIMS, BL_PRESETS, blendSpace, blendable, compareLists, defaultWeights, dist, groupSummary, kmeans, lerp, makePath, nearest, pca2, presetOn } from "./blend-space.js";

const BL_COLORS = ["var(--sec0)", "var(--sec1)", "var(--sec2)", "var(--sec3)", "var(--sec4)", "#8a8a8a", "#a0723a", "#3a9a9a"];
// a, b: From and To track ids; pick: which one a map click sets next; path: track ids of the 5-step path; focus: the picked group
const blend = { w: defaultWeights(), k: 5, a: null, b: null, t: 50, pick: "a", path: null, focus: null };
const blCmp = { a: null, b: null, show: false, t: 50 }; // list ids
let scores = {}; // track id -> { vibe: score }
const txt = (s) => document.createTextNode(s);
const items = () => tracks.map((t) => ({ ...t, vibes: scores[t.id] || {} })).filter(blendable);
const nameOf = (c) => (c ? (c.title || `Track ${c.id}`) + (c.artist ? ` — ${c.artist}` : "") : "");
const open = (id) => { switchTo("library"); openTrack(id); };
const say = (m) => ($("bl-why").textContent = m);
const saveAs = (name, ids) => saveList(name, ids).then(say, (e) => say(e.message));

export async function renderBlend() {
  try { scores = await api("/api/vibe-scores"); } catch (_) { scores = {}; }
  try { await loadLists(); } catch (_) { /* the panel says there are no lists */ }
  paintBlend();
}

function paintWeights() {
  const presets = document.querySelector(".bl-presets");
  const pressAll = () => presets.querySelectorAll(".bl-preset").forEach((b, i) => b.setAttribute("aria-pressed", String(presetOn(blend.w, BL_PRESETS[i][2]))));
  presets.replaceChildren(h("span", { class: "muted" }, "Start from:"), ...BL_PRESETS.map(([name, tip, w]) => h("button", { type: "button", class: "bl-preset", title: tip,
    "aria-pressed": String(presetOn(blend.w, w)),
    onclick: () => { BL_DIMS.forEach((d) => (blend.w[d.k] = w[d.k] ?? 0)); blend.path = null; paintBlend(); } }, name)));
  $("bl-dims").replaceChildren(...BL_DIMS.map((d) => {
    const v = String(blend.w[d.k]);
    const r = h("input", { type: "range", min: "0", max: "2", step: "0.5", value: v, "aria-label": `${d.name} weight`, "aria-valuetext": v }), o = h("output", {}, v);
    r.addEventListener("input", () => {
      blend.w[d.k] = +r.value; o.textContent = r.value; r.setAttribute("aria-valuetext", r.value);
      r.closest(".bl-dim").classList.toggle("off", !+r.value);
      pressAll(); blend.path = null; drawBlend();
    });
    const tip = d.k === "tempo" ? "Log scale, and half or double tempo counts as close (70 sits near 140)"
      : d.k === "key" ? "Position on the Camelot wheel, so 12A sits next to 1A; major and minor sit a little apart" : `The ${d.name.toLowerCase()} measurement`;
    return h("label", { class: "bl-dim" + (blend.w[d.k] ? "" : " off"), title: tip }, d.name, r, o);
  }));
}

function paintBlend() {
  paintWeights();
  const cs = items(), have = new Set(cs.map((c) => c.id));
  $("bl-titles").replaceChildren(...cs.map((c) => h("option", { value: nameOf(c) })));
  if (!have.has(blend.a) || !have.has(blend.b)) {  // lowest energy to highest, until you pick
    const byE = [...cs].sort((x, y) => x.energy - y.energy);
    blend.a = byE.length > 1 ? byE[0].id : null; blend.b = byE.length > 1 ? byE[byE.length - 1].id : null; blend.path = null;
  }
  paintLists();
  const byId = new Map(cs.map((c) => [c.id, c]));
  $("bl-a").value = nameOf(byId.get(blend.a)); $("bl-b").value = nameOf(byId.get(blend.b));
  $("bl-k").value = String(blend.k); $("bl-tv").value = String(blend.t); $("bl-tout").textContent = `${blend.t}%`;
  drawBlend();
}

function paintLists() {
  const ids = lists.map((l) => l.id);
  if (!ids.includes(blCmp.a)) blCmp.a = ids[0] ?? null;
  if (!ids.includes(blCmp.b)) blCmp.b = ids.find((x) => x !== blCmp.a) ?? null;
  $("bl-lists").replaceChildren(...lists.map((l) => h("span", { class: "bl-list" }, h("span", {}, l.name), h("span", { class: "muted" }, String(l.track_ids.length)),
    h("button", { type: "button", "aria-label": `Delete list ${l.name}`, title: "Delete this list (the tracks stay in your library)",
      onclick: () => deleteList(l.id).catch((e) => say(e.message)) }, "×"))));
  if (!lists.length) $("bl-lists").append(h("span", { class: "muted" }, "No lists yet."));
  const opts = (sel) => lists.map((l) => h("option", { value: String(l.id), selected: l.id === sel ? "" : null }, `${l.name} (${l.track_ids.length})`));
  $("bl-la").replaceChildren(...opts(blCmp.a)); $("bl-lb").replaceChildren(...opts(blCmp.b));
  $("bl-showl").checked = blCmp.show;
}

function clearAll(msg) {
  $("bl-svg").replaceChildren(h("p", { class: "muted" }, msg));
  for (const id of ["bl-legend", "bl-near", "bl-cmp"]) $(id).replaceChildren();
  say("");
}

function drawBlend() {
  const cs = items();
  if (!BL_DIMS.some((d) => blend.w[d.k] > 0)) return clearAll("Turn on at least one measurement.");
  if (cs.length < 3) return clearAll("Blend needs at least 3 analyzed tracks with a tempo, key and energy.");
  const vec = blendSpace(cs, blend.w), lab = kmeans(vec, Math.min(blend.k, cs.length)), P = pca2(vec), cmp = drawCompare(cs, vec);
  const show = cmp && blCmp.show, inA = new Set(show ? cmp.ia : []), inB = new Set(show ? cmp.ib : []);
  const idx = new Map(cs.map((c, i) => [c.id, i])), ia = idx.get(blend.a), ib = idx.get(blend.b), pair = ia != null && ib != null && ia !== ib;
  // the spot t of the way from A to B, and the nearest other tracks to it
  let target = null, near = [];
  if (pair) {
    target = lerp(vec[ia], vec[ib], blend.t / 100);
    const span = dist(vec[ia], vec[ib]) || 1;
    near = nearest(vec, cs.map((_, i) => i).filter((i) => i !== ia && i !== ib), target).map((x) => ({ ...x, off: x.d / span }));
  }
  const pathIdx = pair && blend.path ? blend.path.map((id) => idx.get(id)).filter((i) => i != null) : null;
  drawMap(cs, P, lab, { ia, ib, pair, target, near, pathIdx, show, inA, inB, cmp });
  drawLegend(cs, lab);
  drawNear(cs, { ia, ib, pair, near, pathIdx });
}

function drawMap(cs, P, lab, o) {
  const { ia, ib, pair, target, near, pathIdx, show, inA, inB, cmp } = o;
  const W = 640, H = 340, M = 16, xs = P.pts.map((p) => p[0]), ys = P.pts.map((p) => p[1]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const X = (v) => M + ((v - x0) / (x1 - x0 || 1)) * (W - 2 * M), Y = (v) => H - M - ((v - y0) / (y1 - y0 || 1)) * (H - 2 * M);
  const at = (i) => [X(P.pts[i][0]), Y(P.pts[i][1])];
  const sv = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Map of your tracks in the picked measurements, colored by group" });
  const hit = new Set([...near.map((x) => x.i), ...(pathIdx || [])]);
  if (pathIdx) sv.append(svg("polyline", { points: [ia, ...pathIdx, ib].map((i) => at(i).join(",")).join(" "), fill: "none", stroke: "var(--fg)", "stroke-width": 1.5, "stroke-dasharray": "4 3", opacity: 0.7 }));
  else if (pair) sv.append(svg("line", { x1: at(ia)[0], y1: at(ia)[1], x2: at(ib)[0], y2: at(ib)[1], stroke: "var(--muted)", "stroke-dasharray": "3 4" }));
  const nameA = show && lists.find((l) => l.id === blCmp.a).name, nameB = show && lists.find((l) => l.id === blCmp.b).name;
  cs.forEach((c, i) => {
    const end = i === ia || i === ib, on = hit.has(i), member = inA.has(i) || inB.has(i);
    // a picked group always wins, even with lists shown; otherwise shown lists fade everything else
    const dimmed = end ? false : blend.focus != null ? lab[i] !== blend.focus : show && !member;
    const [cx, cy] = at(i), r = end ? 8 : on ? 7 : 5;
    const fill = show && member ? (inA.has(i) ? "var(--accent)" : "var(--sec4)") : BL_COLORS[lab[i]];
    const inLists = show && member ? ` · in ${[inA.has(i) ? nameA : "", inB.has(i) ? nameB : ""].filter(Boolean).join(" and ")}` : "";
    const tip = svg("title", {}, txt(`${c.title || c.id} · ${c.bpm.toFixed(0)} BPM · ${c.camelot} · energy ${c.energy.toFixed(2)}${inLists}. Click to set as ${blend.pick === "a" ? "From" : "To"}, double-click to open.`));
    const attrs = { class: "bl-pt" + (end ? " end" : on ? " hit" : ""), fill, opacity: dimmed ? 0.15 : end || on || member ? 1 : 0.75 };
    if (show && inA.has(i) && inB.has(i)) Object.assign(attrs, { stroke: "var(--sec4)", "stroke-width": 3 });
    const pt = show && inB.has(i) && !inA.has(i) && !end
      ? svg("rect", { ...attrs, x: cx - r * 0.85, y: cy - r * 0.85, width: r * 1.7, height: r * 1.7, rx: 1.5 }, tip)
      : svg("circle", { ...attrs, cx, cy, r }, tip);
    pt.addEventListener("click", () => { blend[blend.pick] = c.id; blend.pick = blend.pick === "a" ? "b" : "a"; blend.path = null; paintBlend(); });
    pt.addEventListener("dblclick", () => open(c.id));
    sv.append(pt);
  });
  if (target && !pathIdx) {
    const [tx, ty] = P.project(target).map((v, k) => (k ? Y(v) : X(v)));
    sv.append(svg("path", { d: `M${tx - 7},${ty}h14M${tx},${ty - 7}v14`, stroke: "var(--fg)", "stroke-width": 2 }, svg("title", {}, txt(`${blend.t}% of the way`))));
  }
  if (show) for (const [cen, col, nm] of [[cmp.cA, "var(--accent)", nameA], [cmp.cB, "var(--sec4)", nameB]]) {
    const q = P.project(cen), qx = X(q[0]), qy = Y(q[1]), left = qx > W - 140;
    sv.append(svg("circle", { cx: qx, cy: qy, r: 11, fill: "none", stroke: col, "stroke-width": 2.5 }, svg("title", {}, txt(`Middle of ${nm}`))),
      svg("text", { x: left ? qx - 14 : qx + 14, "text-anchor": left ? "end" : "start", y: qy + 4, "font-size": 11, fill: col, "font-weight": 700 }, txt(`${nm} middle`)));
  }
  for (const [i, tag] of [[ia, "A"], [ib, "B"]]) if (i != null) sv.append(svg("text", { x: at(i)[0] + 10, y: at(i)[1] - 8, "font-size": 12, "font-weight": 700, fill: "var(--fg)" }, txt(tag)));
  $("bl-svg").replaceChildren(sv);
}

function drawLegend(cs, lab) {
  const kk = Math.max(...lab) + 1;
  $("bl-legend").replaceChildren(...Array.from({ length: kk }, (_, j) => {
    const sum = groupSummary(cs, lab, j, blend.w), on = blend.focus === j;
    return h("li", { class: (blend.focus != null && !on ? "dim" : "") + (on ? " on" : ""), title: on ? "Click again to show all groups" : "Click to pick this group out on the map",
      role: "button", tabindex: "0", "aria-pressed": String(on),
      onkeydown: (e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); e.currentTarget.click(); } },
      onclick: () => { blend.focus = on ? null : j; drawBlend(); document.querySelectorAll("#bl-legend li")[j]?.focus(); } },
    h("i", { style: `background:${BL_COLORS[j]}` }), h("span", {}, sum),
    h("button", { type: "button", class: "linklike bl-save", title: "Save this group's tracks as a list",
      onclick: (e) => { e.stopPropagation(); saveAs(`Group ${j + 1}: ${sum.split(" · ")[0]}`, cs.filter((_, i) => lab[i] === j).map((c) => c.id)); } }, "save as list"));
  }));
}

const trackRow = (c, extra) => h("li", {}, h("button", { type: "button", class: "linklike", onclick: () => open(c.id) }, c.title || `Track ${c.id}`), " ",
  h("span", { class: "muted" }, `${c.bpm.toFixed(0)} BPM · ${c.camelot}${extra}`));

function drawNear(cs, { ia, ib, pair, near, pathIdx }) {
  const row = (c, extra) => trackRow(c, ` · energy ${c.energy.toFixed(2)}${extra ? ` · ${extra}` : ""}`);
  if (!pair) { say("Pick two different tracks, or click two points on the map."); $("bl-near").replaceChildren(); return; }
  if (pathIdx) {
    say("A 5-step path from A to B: each step is the nearest track to the next 1/6 of the way that isn't already used.");
    const ids = [ia, ...pathIdx, ib].map((i) => cs[i].id);
    $("bl-near").replaceChildren(row(cs[ia], "start"), ...pathIdx.map((i) => row(cs[i])), row(cs[ib], "end"),
      h("li", { class: "bl-nonum" }, h("button", { type: "button", class: "linklike bl-save", onclick: () => saveAs(`Path: ${cs[ia].title} to ${cs[ib].title}`, ids) }, "Save this path as a list")));
    return;
  }
  say(`Nearest tracks to the spot ${blend.t}% of the way from A to B. "Off by" is the distance from that spot, as a share of the A–B distance.`);
  $("bl-near").replaceChildren(...near.map((x) => row(cs[x.i], `off by ${Math.round(x.off * 100)}%`)));
}

function drawCompare(cs, vec) {
  const box = $("bl-cmp"), msg = (m) => { box.replaceChildren(h("p", { class: "muted" }, m)); return null; };
  if (lists.length < 2) return msg("Save at least two lists to compare them.");
  if (blCmp.a === blCmp.b) return msg("Pick two different lists.");
  const LA = lists.find((l) => l.id === blCmp.a), LB = lists.find((l) => l.id === blCmp.b), idx = new Map(cs.map((c, i) => [c.id, i]));
  const ia = LA.track_ids.map((id) => idx.get(id)).filter((i) => i != null), ib = LB.track_ids.map((id) => idx.get(id)).filter((i) => i != null);
  if (!ia.length || !ib.length) return msg("One of these lists has no analyzed tracks.");
  const r = compareLists(cs, vec, ia, ib, blend.w, LB.name);
  const sw = (b) => h("i", { class: "bl-sw" + (b ? " b" : ""), style: `background:${b ? "var(--sec4)" : "var(--accent)"}` });
  const sum = h("p", { class: "bl-sum" }, sw(0), h("b", {}, LA.name), ` ${ia.length} · `, sw(1), h("b", {}, LB.name), ` ${ib.length} · ${r.both.length ? `${r.both.length} in both · ` : ""}`,
    h("b", {}, r.apart), h("span", { class: "muted" }, ` (${Math.round(r.share * 100)}% of tracks sit closer to their own list's middle)`));
  const bar = (z) => {
    if (z == null) return h("span", { class: "muted" }, "–");
    const w = (Math.min(Math.abs(z), 2) / 2) * 50;
    return h("span", { class: "bl-bar", title: `${z > 0 ? "+" : ""}${z.toFixed(2)} spreads (B minus A)` }, h("i", { style: `left:${z >= 0 ? 50 : 50 - w}%;width:${w}%;background:${z >= 0 ? "var(--sec4)" : "var(--accent)"}` }));
  };
  const table = h("table", { class: "bl-diff" }, h("thead", {}, h("tr", {}, h("th", {}, "Dimension"), h("th", {}, LA.name), h("th", {}, LB.name), h("th", { class: "hide-s" }, "Difference"), h("th", {}, "In words"))),
    h("tbody", {}, ...r.rows.map((x) => h("tr", {}, h("td", {}, x.d.name), h("td", { class: "n" }, x.a), h("td", { class: "n" }, x.b), h("td", { class: "hide-s" }, bar(x.z)), h("td", {}, x.words)))));
  const span = dist(r.cA, r.cB) || 1, off = (x) => `off by ${Math.round((x.d / span) * 100)}%`;
  const col = (title, found, empty) => h("div", {}, h("h3", {}, title), found.length ? h("ol", { class: "bl-near" }, ...found.map((x) => trackRow(cs[x.i], ` · ${off(x)}`))) : h("p", { class: "muted" }, empty));
  const members = new Set([...ia, ...ib]), others = cs.map((_, i) => i).filter((i) => !members.has(i));
  const between = nearest(vec, others, lerp(r.cA, r.cB, blCmp.t / 100));
  const slider = h("input", { type: "range", min: "0", max: "100", step: "5", value: String(blCmp.t), "aria-label": `How far from ${LA.name} to ${LB.name}` });
  slider.addEventListener("input", () => { blCmp.t = +slider.value; drawBlend(); document.querySelector("#bl-cmp input[type=range]")?.focus(); });
  const saveBtn = h("button", { type: "button", class: "linklike bl-save", onclick: () => saveAs(`Between ${LA.name} and ${LB.name}`, between.map((x) => cs[x.i].id)) }, "Save as a list");
  box.replaceChildren(sum, table, h("div", { class: "bl-cols" },
    col(`In ${LB.name}, most like ${LA.name}`, nearest(vec, r.onlyB, r.cA), "Every track is in both."),
    col(`In ${LA.name}, most like ${LB.name}`, nearest(vec, r.onlyA, r.cB), "Every track is in both."),
    h("div", {}, h("h3", {}, "In neither list, between them"), h("label", { class: "bl-mid" }, sw(0), slider, sw(1), h("output", {}, `${blCmp.t}%`)),
      between.length ? h("ol", { class: "bl-near" }, ...between.map((x) => trackRow(cs[x.i], ` · ${off(x)}`))) : h("p", { class: "muted" }, "Every track is in one of the lists."), between.length ? saveBtn : null)),
  h("p", { class: "muted bl-hint" }, `"Off by" is the distance from the list's middle (or the spot between them) as a share of the distance between the two lists' middles.`));
  return { ia, ib, cA: r.cA, cB: r.cB };
}

function pickBy(inputId, key) {
  $(inputId).addEventListener("change", (e) => {
    const v = e.target.value.trim().toLowerCase(), cs = items();
    const c = cs.find((x) => nameOf(x).toLowerCase() === v) || cs.find((x) => (x.title || "").toLowerCase().startsWith(v));
    if (c) { blend[key] = c.id; blend.path = null; }
    paintBlend();
  });
}

// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  pickBy("bl-a", "a"); pickBy("bl-b", "b");
  $("bl-tv").addEventListener("input", (e) => { blend.t = +e.target.value; $("bl-tout").textContent = `${blend.t}%`; blend.path = null; drawBlend(); });
  $("bl-k").addEventListener("change", (e) => { blend.k = +e.target.value; blend.focus = null; drawBlend(); });
  $("bl-swap").addEventListener("click", () => { [blend.a, blend.b] = [blend.b, blend.a]; blend.path = null; paintBlend(); });
  $("bl-path").addEventListener("click", () => {
    const cs = items(), idx = new Map(cs.map((c, i) => [c.id, i])), ia = idx.get(blend.a), ib = idx.get(blend.b);
    if (ia == null || ib == null || ia === ib) return;
    blend.path = makePath(blendSpace(cs, blend.w), ia, ib).map((i) => cs[i].id); drawBlend();
  });
  $("bl-la").addEventListener("change", (e) => { blCmp.a = +e.target.value; drawBlend(); });
  $("bl-lb").addEventListener("change", (e) => { blCmp.b = +e.target.value; drawBlend(); });
  $("bl-showl").addEventListener("change", (e) => { blCmp.show = e.target.checked; drawBlend(); });
  // Lists saved from here or from Library › Actions: repaint the panel if Blend is on screen
  onListsChange(() => { if (!$("ex-blend").hidden && !$("view-explore").hidden) { paintLists(); drawBlend(); } });
}
