"use strict";

const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";
const SEC_COLORS = ["var(--sec0)", "var(--sec1)", "var(--sec2)", "var(--sec3)", "var(--sec4)"];

const EXAMPLES = {
  "Mixes with 8A, 120-128 BPM":
    "SELECT title, bpm, camelot FROM tracks WHERE camelot IN ('7A','8A','9A','8B') AND bpm BETWEEN 120 AND 128",
  "Dark, bass-heavy":
    "SELECT t.title FROM tracks t JOIN tags g ON g.track_id = t.id WHERE g.tag = 'dark'\n  AND t.id IN (SELECT track_id FROM features WHERE name = 'low_end' AND value > 0.3)",
  "Average drop length (bars)":
    "SELECT avg(end_bar - start_bar + 1) AS avg_drop_bars FROM sections WHERE label = 'drop'",
};

const TAG_HELP = {
  "high energy": "Overall energy is above about 0.6, so the track is loud and dense.",
  "chill": "Low energy for its tempo, so it sits back.",
  "melancholic": "Minor key with low energy.",
  "dark": "Dull, low-frequency sound, helped along by a minor key.",
  "bright": "Lots of high-frequency content, helped along by a major key.",
  "euphoric": "Major key, bright sound and high energy together.",
  "driving": "Strong, clear pulse at 115-150 BPM.",
  "danceable": "Steady beat that is easy to dance to.",
  "hypnotic": "Very clear pulse and little change between sections.",
  "bass-heavy": "A large share of the energy sits in the low end.",
  "percussive": "Dominated by drum hits and sharp attacks.",
  "atmospheric": "Few drum hits and a weak pulse, so it floats.",
  "dynamic": "Sections differ a lot in energy, with big builds and drops.",
  "swung": "Off-beat 16ths land late, giving a shuffle feel.",
};
const tagHelp = (tag) => TAG_HELP[tag] || (/ tempo$/.test(tag) ? "Tempo falls in the typical range for this style. A hint, not a genre call." : "");

let tracks = [];
let tagsByTrack = {};
let currentId = null;

// ---------- helpers ----------
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid);
  return el;
}
function svg(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  for (const kid of kids.flat()) if (kid != null) el.append(kid);
  return el;
}
const isNum = (x) => typeof x === "number" && isFinite(x);
function mmss(sec) {
  if (!isNum(sec)) return "–";
  const s = Math.round(sec);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (_) {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

// ---------- views ----------
document.querySelectorAll("nav button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("active", x === b));
    for (const v of ["library", "explore"]) $("view-" + v).hidden = v !== b.dataset.view;
    if (b.dataset.view === "explore") renderExplore();
  })
);

// ---------- library ----------
async function refresh(selectId) {
  tracks = await api("/api/tracks");
  renderList();
  if (selectId != null) openTrack(selectId);
  else if (currentId != null && tracks.some((t) => t.id === currentId)) renderList();
}

function renderList() {
  const ul = $("track-list");
  ul.replaceChildren();
  if (!tracks.length) ul.append(h("li", { class: "muted" }, "No tracks yet."));
  for (const t of tracks) {
    ul.append(
      h("li", { class: t.id === currentId ? "active" : "", onclick: () => openTrack(t.id) },
        h("div", { class: "t" }, t.title || `Track ${t.id}`),
        h("div", { class: "s" }, [isNum(t.bpm) ? `${t.bpm.toFixed(0)} BPM` : null, t.camelot, mmss(t.duration)].filter(Boolean).join(" · "))
      )
    );
  }
}

async function openTrack(id) {
  currentId = id;
  renderList();
  switchTo("library");
  try {
    renderCard(await api(`/api/tracks/${id}`));
  } catch (e) {
    $("card").replaceChildren(h("p", { class: "error" }, e.message));
  }
}
function switchTo(view) {
  document.querySelector(`nav button[data-view=${view}]`).click();
}

function stat(value, label) {
  return h("div", { class: "stat" }, h("b", {}, value), h("span", {}, label));
}

let sectionBus = new EventTarget();

function renderCard(c) {
  sectionBus = new EventTarget();
  const core = c.core || {};
  const key = core.key;
  const root = $("card");
  root.replaceChildren();
  const head = h("div", { class: "card-head" },
    h("h2", {}, (c.source && (c.source.title || c.source.uri)) || `Track ${c.id}`),
    stat(mmss(c.duration), "duration"),
    stat(isNum(core.bpm) ? core.bpm.toFixed(1) : "–", isNum(core.bpm_confidence) ? `BPM · ${Math.round(core.bpm_confidence * 100)}% conf` : "BPM"),
    stat(key ? `${keyName(key)}` : "–", key ? [key.camelot, key.scale].filter(Boolean).join(" · ") || "key" : "key")
  );
  const en = h("div", { class: "stat" },
    h("b", {}, isNum(core.energy) ? core.energy.toFixed(2) : "–"),
    h("span", {}, "energy"),
    h("div", { class: "meter" }, h("i", { style: `width:${isNum(core.energy) ? Math.max(0, Math.min(1, core.energy)) * 100 : 0}%` }))
  );
  head.append(en);
  root.append(head);
  root.append(h("div", { class: "card-actions" }, h("button", { onclick: () => deleteTrack(c.id) }, "Delete track")));

  const energy = c.series && c.series.energy;
  if (energy && energy.times && energy.times.length > 1 || (c.sections || []).length) {
    root.append(h("h3", {}, "Energy & sections"), energyChart(energy, c.sections || [], c.duration));
  }

  if ((c.tags || []).length) {
    root.append(h("h3", {}, "Vibe"),
      h("div", { class: "chips" }, [...c.tags].sort((a, b) => b.score - a.score).map((t) =>
        h("span", { class: "chip", tabindex: "0", style: `--s:${isNum(t.score) ? t.score : 0}`, "data-tip": `${tagHelp(t.tag)}${isNum(t.score) ? ` Score ${t.score.toFixed(2)}.` : ""}`.trim() }, t.tag))));
  }

  const sugs = c.suggestions || [];
  const grids = sugs.filter((s) => s.data && s.data.pattern && typeof s.data.pattern === "object");
  if (grids.length) {
    root.append(h("h3", {}, "Drum patterns"));
    for (const s of grids) root.append(drumGrid(s));
  }
  if (sugs.length) {
    root.append(h("h3", {}, "Try this in Ableton"));
    const groups = {};
    for (const s of sugs) (groups[s.category || "other"] ||= []).push(s);
    for (const [cat, items] of Object.entries(groups)) {
      root.append(h("h3", {}, cat));
      for (const s of items) root.append(h("div", { class: "sug" }, h("b", {}, s.title), h("p", {}, s.body)));
    }
  }

  const errs = Object.entries(c.errors || {});
  if (errs.length) {
    root.append(h("div", { class: "errors" }, "Some stages failed: ", errs.map(([k, v]) => h("div", {}, `${k}: ${v}`))));
  }
}

const NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
function keyName(k) {
  return isNum(k.tonic) ? `${NOTES[k.tonic % 12]} ${k.mode || ""}`.trim() : "–";
}

function energyChart(series, sections, duration) {
  const W = 700, H = 200, L = 40, R = 8, T = 8, B = 22;
  const times = (series && series.times) || [], vals = (series && series.values) || [];
  const end = Math.max(duration || 0, times.length ? times[times.length - 1] : 0, ...sections.map((s) => s.end || 0), 1);
  const letters = [...new Set(sections.map((x) => x.letter || x.label))];
  const pts = times.map((t, i) => [t, vals[i]]).filter(([t, v]) => isNum(t) && isNum(v));
  const view = { a: 0, b: end };
  const minSpan = Math.min(end, 12);
  const wrap = h("div", { class: "chart-wrap" });
  const bar = h("div", { class: "chart-bar" });
  const zoomLabel = h("span", { class: "muted" });
  const btn = (label, title, fn) => h("button", { type: "button", title, "aria-label": title, onclick: fn }, label);
  bar.append(btn("−", "Zoom out", () => zoom(1.6)), btn("+", "Zoom in", () => zoom(1 / 1.6)), btn("Reset", "Show whole track", () => { view.a = 0; view.b = end; draw(); }), zoomLabel,
    h("span", { class: "muted hint" }, "Click a section to pick it. Scroll to zoom, drag to pan"));
  const tip = h("div", { class: "chart-tip", hidden: "" });
  const host = h("div", { class: "chart-host" });
  wrap.append(bar, host, tip,
    h("p", { class: "muted chart-key" },
      h("span", { class: "key-line" }), " Energy: how loud and dense the track is at each moment, sampled every ~3 s. ",
      h("span", { class: "key-box" }), " Shaded blocks are detected sections; same color means the same part repeats."));

  const x = (t) => L + ((t - view.a) / (view.b - view.a)) * (W - L - R);
  const tAt = (px) => view.a + ((px - L) / (W - L - R)) * (view.b - view.a);
  const y = (v) => T + (1 - Math.max(0, Math.min(1, v))) * (H - T - B);
  const energyAt = (t) => {
    if (!pts.length) return null;
    let lo = 0, hi = pts.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (pts[m][0] <= t) lo = m; else hi = m; }
    const [t0, v0] = pts[lo], [t1, v1] = pts[hi];
    return t1 === t0 ? v0 : v0 + ((Math.max(t0, Math.min(t1, t)) - t0) / (t1 - t0)) * (v1 - v0);
  };
  const clampView = () => {
    const span = Math.max(minSpan, Math.min(end, view.b - view.a));
    view.a = Math.max(0, Math.min(end - span, view.a)); view.b = view.a + span;
  };
  function zoom(f, center) {
    const c = center == null ? (view.a + view.b) / 2 : center;
    const span = Math.max(minSpan, Math.min(end, (view.b - view.a) * f));
    const r = (c - view.a) / (view.b - view.a);
    view.a = c - r * span; view.b = view.a + span; clampView(); draw();
  }

  const bus = sectionBus;
  let selIdx = -1;
  bus.addEventListener("section", (e) => { if (e.detail.from !== "chart") { selIdx = e.detail.idx; draw(); } });
  let cursorT = null;
  const svgEl = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Energy over time with sections" });
  host.append(svgEl);
  const clipId = "clip" + Math.random().toString(36).slice(2, 8);
  function draw() {
    const s = svgEl;
    s.replaceChildren();
    s.append(svg("defs", {}, svg("clipPath", { id: clipId }, svg("rect", { x: L, y: 0, width: W - L - R, height: H }))));
    const plot = svg("g", { "clip-path": `url(#${clipId})` });
    sections.forEach((sec, si) => {
      if (!isNum(sec.start) || !isNum(sec.end) || sec.end < view.a || sec.start > view.b) return;
      const color = SEC_COLORS[letters.indexOf(sec.letter || sec.label) % SEC_COLORS.length];
      plot.append(svg("rect", { x: x(sec.start), y: T, width: Math.max(1, x(sec.end) - x(sec.start)), height: H - T - B, fill: color, opacity: si === selIdx ? 0.4 : 0.18, stroke: si === selIdx ? color : "none", "stroke-width": 2 }));
      plot.append(svg("text", { class: "lbl", x: Math.max(L, x(sec.start)) + 3, y: T + 12 }, document.createTextNode([sec.label, sec.letter].filter(Boolean).join(" "))));
    });
    [0.25, 0.5, 0.75].forEach((v) => plot.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: "var(--line)", "stroke-dasharray": "2 3" })));
    if (pts.length > 1) {
      const line = pts.map(([t, v]) => `${x(t).toFixed(1)},${y(v).toFixed(1)}`);
      plot.append(svg("polygon", { points: `${x(pts[0][0])},${y(0)} ${line.join(" ")} ${x(pts[pts.length - 1][0])},${y(0)}`, fill: "var(--accent)", opacity: 0.25 }));
      plot.append(svg("polyline", { points: line.join(" "), fill: "none", stroke: "var(--accent)", "stroke-width": 2 }));
    }
    s.append(plot);
    [0, 0.5, 1].forEach((v) => s.append(svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, document.createTextNode(v))));
    s.append(svg("text", { x: 10, y: (H - B + T) / 2, "text-anchor": "middle", transform: `rotate(-90 10 ${(H - B + T) / 2})` }, document.createTextNode("energy")));
    const span = view.b - view.a;
    const step = [5, 10, 15, 30, 60, 120].find((st) => span / st <= 8) || 120;
    for (let t = Math.ceil(view.a / step) * step; t <= view.b; t += step) s.append(svg("text", { x: x(t), y: H - 6, "text-anchor": "middle" }, document.createTextNode(mmss(t))));
    s.append(svg("line", { class: "cursor", x1: 0, x2: 0, y1: T, y2: H - B, stroke: "var(--fg)", "stroke-width": 1, opacity: 0, "pointer-events": "none" }));
    s.append(svg("circle", { class: "cursor-dot", r: 4, fill: "var(--accent)", stroke: "var(--bg)", "stroke-width": 2, opacity: 0, "pointer-events": "none" }));
    zoomLabel.textContent = view.b - view.a >= end - 0.01 ? "Whole track" : `${mmss(view.a)}–${mmss(view.b)}`;
    if (!drag) s.style.cursor = view.b - view.a < end - 0.01 ? "grab" : "crosshair";
    if (cursorT != null) showTip(cursorT);
  }
  const toVB = (e) => { const r = svgEl.getBoundingClientRect(); return { px: ((e.clientX - r.left) / r.width) * W }; };
  function showTip(t) {
    const cur = svgEl.querySelector(".cursor"), dot = svgEl.querySelector(".cursor-dot");
    if (t < view.a || t > view.b) { hideTip(); return; }
    const v = energyAt(t), sec = sections.find((q) => t >= q.start && t < q.end);
    cur.setAttribute("x1", x(t)); cur.setAttribute("x2", x(t)); cur.setAttribute("opacity", 0.5);
    if (v != null) { dot.setAttribute("cx", x(t)); dot.setAttribute("cy", y(v)); dot.setAttribute("opacity", 1); }
    tip.replaceChildren(
      h("b", {}, mmss(t)),
      v != null ? h("div", {}, `Energy ${v.toFixed(2)}`, h("span", { class: "muted" }, v > .75 ? " (loud, full)" : v > .45 ? " (medium)" : " (quiet, sparse)")) : null,
      sec ? h("div", { class: "muted" }, `${sec.label} ${sec.letter || ""} · bars ${sec.start_bar}–${sec.end_bar}`.replace("  ", " ")) : null);
    tip.hidden = false;
    const r = svgEl.getBoundingClientRect(), hr = wrap.getBoundingClientRect();
    const left = r.left - hr.left + (x(t) / W) * r.width;
    tip.style.left = Math.min(Math.max(left + 12, 0), hr.width - tip.offsetWidth) + "px";
    tip.style.top = (r.top - hr.top + 8) + "px";
  }
  function hideTip() {
    cursorT = null; tip.hidden = true;
    if (svgEl) for (const q of [".cursor", ".cursor-dot"]) svgEl.querySelector(q).setAttribute("opacity", 0);
  }
  let drag = null;
  const atEdge = () => view.b - view.a < end - 0.01;
  svgEl.addEventListener("pointerdown", (e) => { svgEl.setPointerCapture(e.pointerId); drag = { px: toVB(e).px, a: view.a, b: view.b }; });
  svgEl.addEventListener("pointermove", (e) => {
    const { px } = toVB(e);
    if (drag && atEdge()) {
      const dt = ((px - drag.px) / (W - L - R)) * (drag.b - drag.a);
      view.a = drag.a - dt; view.b = drag.b - dt; clampView();
      svgEl.style.cursor = "grabbing"; hideTip(); draw(); return;
    }
    cursorT = tAt(Math.max(L, Math.min(W - R, px))); showTip(cursorT);
  });
  const up = (e) => {
    if (e && e.type === "pointerup" && drag && Math.abs(toVB(e).px - drag.px) < 3) {
      const t0 = tAt(toVB(e).px), si = sections.findIndex((q) => t0 >= q.start && t0 < q.end);
      if (si >= 0) { selIdx = si; draw(); bus.dispatchEvent(new CustomEvent("section", { detail: { idx: si, from: "chart" } })); }
    }
    drag = null; svgEl.style.cursor = atEdge() ? "grab" : "crosshair";
  };
  svgEl.addEventListener("pointerup", up);
  svgEl.addEventListener("pointercancel", up);
  svgEl.addEventListener("pointerleave", () => { if (!drag) hideTip(); });
  svgEl.addEventListener("wheel", (e) => {
    e.preventDefault();
    zoom(Math.exp(Math.sign(e.deltaY) * 0.25), tAt(Math.max(L, Math.min(W - R, toVB(e).px))));
  }, { passive: false });
  draw();
  return wrap;
}

function laneRows(pattern, prev) {
  const rows = [];
  for (const [lane, pat] of Object.entries(pattern)) {
    if (typeof pat !== "string") continue;
    const cells = [...pat.slice(0, 16)].map((ch, i) => {
      const changed = prev && prev[lane] && prev[lane][i] !== ch;
      return h("i", { class: [i % 4 === 0 && i ? "beat" : "", ch === "x" ? "full" : ch === "o" ? "half" : "", changed ? "chg" : ""].join(" ").trim() });
    });
    rows.push(h("div", { class: "drum-lane" }, h("span", { class: "name" }, lane), h("div", { class: "drum-grid" }, cells)));
  }
  return rows;
}

function diffText(pattern, prev) {
  const parts = [];
  for (const lane of Object.keys(pattern)) {
    if (!prev || typeof prev[lane] !== "string") continue;
    let add = 0, drop = 0;
    [...pattern[lane]].forEach((ch, i) => { const was = prev[lane][i] !== "."; const is = ch !== "."; if (is && !was) add++; if (!is && was) drop++; });
    if (add || drop) parts.push(`${lane} ${[add ? "+" + add : "", drop ? "−" + drop : ""].filter(Boolean).join(" ")}`);
  }
  return parts.length ? parts.join(", ") : "same as previous section";
}

function drumGrid(s) {
  const det = s.data.origin === "detected";
  const wrap = h("div", { class: "sug" }, h("b", {}, s.title));
  if (s.data.origin) {
    wrap.append(h("span", { class: "origin " + (det ? "det" : "tpl") },
      det ? "Measured from this track" : `Template for ${s.data.style || "this tempo"}, not from the audio`));
  }
  const secs = s.data.by_section;
  if (!secs || !secs.length) { wrap.append(...laneRows(s.data.pattern)); return wrap; }
  const letters = [...new Set(secs.map((x) => x.letter || x.label))];
  const bus = sectionBus;
  let sel = secs.reduce((best, x, k) => ((x.energy || 0) > (secs[best].energy || 0) ? k : best), 0), showChg = true;
  const tabs = h("div", { class: "sec-tabs", role: "tablist", "aria-label": "Section" });
  const body = h("div", { class: "sec-body" });
  const note = h("p", { class: "muted sec-note" });
  const toggle = h("input", { type: "checkbox", checked: "" });
  toggle.addEventListener("change", () => { showChg = toggle.checked; paint(true); });
  function paint(quiet) {
    [...tabs.children].forEach((b, k) => b.setAttribute("aria-selected", String(k === sel)));
    const sec = secs[sel], before = sel > 0 ? secs[sel - 1] : null;
    body.replaceChildren(...laneRows(sec.pattern, before && showChg ? before.pattern : null));
    const name = (x) => `${x.label} ${x.letter || ""}`.trim();
    note.textContent = `${name(sec)}, bars ${sec.start_bar}-${sec.end_bar}. ` + (before ? `Versus ${name(before)}: ${diffText(sec.pattern, before.pattern)}.` : "First section.");
    if (quiet !== true) bus.dispatchEvent(new CustomEvent("section", { detail: { idx: sec.idx, from: "tabs" } }));
  }
  bus.addEventListener("section", (e) => {
    if (e.detail.from !== "chart") return;
    const k = secs.findIndex((x) => x.idx === e.detail.idx);
    if (k >= 0) { sel = k; paint(true); }
  });
  secs.forEach((x, k) => tabs.append(h("button", {
    type: "button", role: "tab", class: "sec-tab", style: `--c:${SEC_COLORS[letters.indexOf(x.letter || x.label) % SEC_COLORS.length]}`,
    onclick: () => { sel = k; paint(); },
  }, `${x.label} ${x.letter || ""}`.trim())));
  wrap.append(tabs, body, note, h("label", { class: "chg-toggle muted" }, toggle, " Outline steps that changed from the previous section"));
  paint(true);
  return wrap;
}

async function deleteTrack(id) {
  if (!confirm("Delete this track?")) return;
  await api(`/api/tracks/${id}`, { method: "DELETE" });
  currentId = null;
  $("card").replaceChildren(h("p", { class: "muted" }, "Select a track."));
  await refresh();
}

// ---------- upload ----------
function setStatus(msg, isErr) {
  const el = $("status");
  el.textContent = msg;
  el.className = isErr ? "error" : "muted";
}
function report(res) {
  const results = res.results || [];
  const errs = results.filter((r) => r.error);
  setStatus(errs.length ? errs.map((r) => `${r.title}: ${r.error}`).join("; ") : `Analyzed ${results.length} track(s).`, errs.length > 0);
  const ok = results.filter((r) => r.track_id != null);
  return ok.length ? ok[ok.length - 1].track_id : null;
}
async function uploadFiles(files) {
  if (!files.length) return;
  setStatus(`Analyzing ${files.length} file(s)… this can take a while.`);
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  try {
    const id = report(await api("/api/analyze", { method: "POST", body: fd }));
    await refresh(id);
  } catch (e) { setStatus(e.message, true); }
}
const drop = $("drop");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));
$("file-input").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });
$("url-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("url-input").value.trim();
  if (!url) return;
  setStatus("Fetching and analyzing…");
  try {
    const id = report(await api("/api/analyze-url", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) }));
    $("url-input").value = "";
    await refresh(id);
  } catch (err) { setStatus(err.message, true); }
});

// ---------- explore ----------
async function loadTags() {
  try {
    const r = await sql("SELECT track_id, tag FROM tags");
    tagsByTrack = {};
    for (const [tid, tag] of r.rows) (tagsByTrack[tid] ||= new Set()).add(tag);
  } catch (_) { tagsByTrack = {}; }
  const all = [...new Set(Object.values(tagsByTrack).flatMap((s) => [...s]))].sort();
  const sel = $("f-tag"), cur = sel.value;
  sel.replaceChildren(h("option", { value: "" }, "any"), all.map((t) => h("option", { value: t }, t)));
  sel.value = all.includes(cur) ? cur : "";
}

async function renderExplore() {
  tracks = await api("/api/tracks");
  await loadTags();
  drawScatter();
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
["f-tag", "f-min", "f-max"].forEach((id) => $(id).addEventListener("input", drawScatter));

async function sql(text) {
  return api("/api/query", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sql: text }) });
}
for (const [label, q] of Object.entries(EXAMPLES)) {
  $("examples").append(h("button", { onclick: () => { $("sql").value = q; runSql(); } }, label));
}
async function runSql() {
  $("sql-error").textContent = "";
  const table = $("sql-result");
  table.replaceChildren();
  try {
    const r = await sql($("sql").value);
    table.append(h("tr", {}, r.columns.map((c) => h("th", {}, c))));
    for (const row of r.rows) table.append(h("tr", {}, row.map((v) => h("td", {}, v == null ? "" : String(v)))));
  } catch (e) { $("sql-error").textContent = e.message; }
}
$("run-sql").addEventListener("click", runSql);

refresh().catch((e) => setStatus(e.message, true));
