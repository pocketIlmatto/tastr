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
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
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
  stopAllPlayback();
  sectionBus = new EventTarget();
  const core = c.core || {};
  const key = core.key;
  const root = $("card");
  root.replaceChildren();
  const head = h("div", { class: "card-head" },
    h("h2", {}, (c.source && (c.source.title || c.source.uri)) || `Track ${c.id}`),
    h("p", { class: "card-sub" + (c.source.artist ? "" : " muted") }, [c.source.artist, c.source.album].filter(Boolean).join(" · ") || "No artist or album in the file tags"),
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

  if (c.metadata) root.append(metadataSection(c.metadata));

  const energy = c.series && c.series.energy;
  if (energy && energy.times && energy.times.length > 1 || (c.sections || []).length) {
    root.append(section("energy", "Energy & sections", [energyChart(energy, c.sections || [], c.duration)]));
  }

  if ((c.tags || []).length) {
    root.append(section("vibe", "Vibe", [
      h("div", { class: "chips" }, [...c.tags].sort((a, b) => b.score - a.score).map((t) =>
        h("span", { class: "chip", tabindex: "0", style: `--s:${isNum(t.score) ? t.score : 0}`, "data-tip": `${tagHelp(t.tag)}${isNum(t.score) ? ` Score ${t.score.toFixed(2)}.` : ""}`.trim() }, t.tag))),
    ]));
  }

  // Template grids are tempo-based guesses, so they stay hidden (here and in the Try list) until asked for.
  const isTemplate = (s) => s.data && s.data.origin === "template";
  const templates = (c.suggestions || []).filter(isTemplate);
  const shown = ideasShown.has(c.id);
  const sugs = (c.suggestions || []).filter((s) => shown || !isTemplate(s));
  const grids = sugs.filter((s) => s.data && s.data.pattern && typeof s.data.pattern === "object");
  if (grids.length || templates.length) {
    const body = grids.map((g) => drumGrid(g, c));
    if (templates.length) {
      body.push(h("div", { class: "ideas-bar" },
        h("button", { type: "button", "data-tip": "Starting points picked by this track's tempo.", onclick: () => { if (shown) ideasShown.delete(c.id); else ideasShown.add(c.id); renderCard(c); } }, shown ? "Hide pattern ideas" : "Suggest pattern ideas")));
    }
    root.append(section("drums", "Drum patterns", body));
  }
  if (sugs.length) {
    const pick = h("select", { class: "daw-pick", "aria-label": "Your DAW", onchange: (e) => {
      daw = e.target.value;
      try { localStorage.setItem("tastr-daw", daw); } catch (_) {}
      renderCard(c);
    } }, Object.entries(DAWS).map(([k, v]) => h("option", { value: k, ...(k === daw ? { selected: "" } : {}), ...(v.soon ? { disabled: "" } : {}) }, v.soon ? `${v.name} (coming soon)` : v.name)));
    const groups = {};
    for (const s of sugs) (groups[s.category || "other"] ||= []).push(s);
    const body = [];
    for (const [cat, items] of Object.entries(groups)) {
      body.push(h("h3", {}, cat));
      for (const s of items) body.push(h("div", { class: "sug" }, h("b", {}, s.title), h("p", {}, dawText(s))));
    }
    root.append(section("try", ["Try this in ", pick], body));
  }

  const errs = Object.entries(c.errors || {});
  if (errs.length) {
    root.append(h("div", { class: "errors" }, "Some stages failed: ", errs.map(([k, v]) => h("div", {}, `${k}: ${v}`))));
  }
}

// Card sections are <details>; which are open is remembered across tracks (Metadata starts closed).
const openState = {};
const ideasShown = new Set(); // track ids whose template pattern ideas are revealed
function section(key, title, body, defaultOpen = true) {
  const open = key in openState ? openState[key] : defaultOpen;
  const det = h("details", { class: "sec", ...(open ? { open: "" } : {}) }, h("summary", {}, title), h("div", { class: "sec-content" }, body));
  det.addEventListener("toggle", () => { openState[key] = det.open; });
  return det;
}

function metadataSection(meta) {
  const body = [];
  for (const [group, kv] of Object.entries(meta)) {
    const rows = Object.entries(kv).filter(([, v]) => v != null && v !== "");
    body.push(h("div", { class: "meta-group" }, h("h4", {}, group), rows.length
      ? h("dl", { class: "kv" }, rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, String(v))]))
      : h("p", { class: "muted meta-empty" }, "None found. The title above came from the file name.")));
  }
  return section("meta", "Metadata", [h("div", { class: "meta-cols" }, body)], false);
}

// Suggestion text marks DAW-specific parts as {tokens}; each DAW supplies a glossary for them.
// Ableton Live is the only glossary so far. Other DAWs show in the picker but can't be selected.
const DAWS = {
  ableton: {
    name: "Ableton Live",
    tokens: {
      drumrack: () => "a Drum Rack",
      pads: (d) => {
        const lanes = Object.keys(d.pattern || {}).filter((l) => d.pads && d.pads[l]);
        return lanes.length ? ` (${lanes.map((l) => `${laneName(l)} ${d.pads[l]}`).join(", ")})` : "";
      },
      swing: (d) => `The off-beat 16ths sit late. Drag 'Swing 16ths' from the Groove Pool (Core Library > Swing) onto your drum clip and start around ${d.swing_percent}%, or use a Groove with Timing at about ${Math.round((d.swing_percent - 50) * 4)}%.`,
      sidechain: (d) => `Put a Compressor on the bass, enable Sidechain from the kick, ratio 4:1, attack 0.1 ms, release about ${Math.round(d.release_ms * 0.6)}-${d.release_ms} ms (an 8th note at this tempo is ${d.release_ms} ms). Or use Live 12's Shaper/Envelope Follower for a drawn ducking curve.`,
      setup: (d) => {
        const scale = d.root ? `Turn on Scale in the Control Bar with root ${d.root} and scale ${d.scale}` : "";
        if (!isNum(d.bpm)) return `${scale}.`;
        const key = d.root ? `${scale} so MIDI devices and the piano roll follow it.${d.camelot ? ` Camelot ${d.camelot} if you are mixing it.` : ""} ` : "";
        return `Set the tempo to ${d.bpm.toFixed(1)} (half/double: ${(d.bpm / 2).toFixed(0)}/${(d.bpm * 2).toFixed(0)}). ${key}If you drop the audio in, warp it in ${d.warp_mode} mode.`;
      },
    },
  },
  // Placeholders so the picker and switching work; add a glossary (and drop `soon`) to enable one.
  logic: { name: "Logic Pro", soon: true },
  fl: { name: "FL Studio", soon: true },
  bitwig: { name: "Bitwig Studio", soon: true },
};
let daw = "ableton";
try { const v = localStorage.getItem("tastr-daw"); if (DAWS[v] && !DAWS[v].soon) daw = v; } catch (_) {}

function dawText(s) {
  const tpl = s.data && s.data.template;
  if (!tpl) return s.body;
  const tokens = DAWS[daw].tokens || {};
  return tpl.replace(/\{(\w+)\}/g, (m, k) => (tokens[k] ? tokens[k](s.data) : m));
}

const NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
function keyName(k) {
  return isNum(k.tonic) ? `${NOTES[k.tonic % 12]} ${k.mode || ""}`.trim() : "–";
}

function energyChart(series, sections, duration) {
  const W = 700, H = 220, L = 40, R = 8, T = 28, B = 22;
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
      // Label band above the grid: solid block per section, text shortens to the letter, then disappears.
      const x0 = Math.max(L, x(sec.start)), x1 = Math.min(W - R, x(sec.end)), room = x1 - x0;
      plot.append(svg("rect", { x: x0, y: 4, width: Math.max(1, room), height: 18, rx: 2, fill: color, stroke: si === selIdx ? "var(--fg)" : "var(--panel)", "stroke-width": si === selIdx ? 2 : 1 }));
      const full = [cap(sec.label), sec.letter].filter(Boolean).join(" ");
      const label = room > full.length * 6.2 + 8 ? full : room > 22 ? (sec.letter || cap(sec.label).slice(0, 3)) : "";
      if (label) plot.append(svg("text", { class: "band", x: x0 + 5, y: 16.5 }, document.createTextNode(label)));
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
      sec ? h("div", { class: "muted" }, `${cap(sec.label)} ${sec.letter || ""} · bars ${sec.start_bar}–${sec.end_bar}`.replace("  ", " ")) : null);
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

// The engine sends what each measured lane means (data.lane_help); this covers cards analyzed before it did.
const LANE_HELP = {
  kick: "Kick: low hits whose energy sits below about 120 Hz.",
  snare: "Snare: noisy mid hits (400 Hz to 5 kHz), usually with some body around 200 Hz.",
  hat: "Hi-hat: short hits above 6 kHz.",
};
const laneName = (lane) => lane.replace(/_/g, " ");
const laneHelp = (lane, measured, help) => {
  if (!measured) return "Part of a template pattern, not measured from the audio.";
  const text = help && help[lane];
  return text ? `${cap(laneName(lane))}: ${text.charAt(0).toLowerCase()}${text.slice(1)}` : LANE_HELP[lane] || "Measured from the audio.";
};

// One lane per instrument; the 16 steps sit in four tinted beat groups, with beat numbers above the first lane.
const STEP_NAMES = { x: "strong", o: "softer", ".": "empty" };
function laneRows(pattern, prev, measured, onToggle, help) {
  const rows = [];
  for (const [lane, pat] of Object.entries(pattern)) {
    if (typeof pat !== "string") continue;
    const cells = [...pat.slice(0, 16)].map((ch, i) => {
      const changed = prev && prev[lane] && prev[lane][i] !== ch;
      return h("i", { class: [ch === "x" ? "full" : ch === "o" ? "half" : "", changed ? "chg" : ""].join(" ").trim(),
        "data-step": i, "data-lane": lane, tabindex: onToggle ? "0" : null, role: onToggle ? "button" : null,
        "aria-label": onToggle ? `${laneName(lane)}, step ${i + 1}: ${STEP_NAMES[ch] || "empty"}. Press to change.` : null,
        onclick: onToggle ? () => onToggle(lane, i) : null,
        onkeydown: onToggle ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onToggle(lane, i); } } : null });
    });
    const beats = [0, 1, 2, 3].map((b) => h("div", { class: "beat-group" }, cells.slice(b * 4, b * 4 + 4)));
    rows.push(h("div", { class: "drum-lane" }, h("span", { class: "name", tabindex: "0", "data-tip": laneHelp(lane, measured, help) }, laneName(lane)), h("div", { class: "drum-grid" }, beats)));
  }
  if (rows.length) rows.unshift(h("div", { class: "drum-lane beat-nums", "aria-hidden": "true" }, h("span", { class: "name" }), h("div", { class: "drum-grid" }, [1, 2, 3, 4].map((n) => h("span", {}, n)))));
  return rows;
}

// ---------- sequencer (WebAudio, no samples) and MIDI export ----------
let actx = null, noiseBuf = null, player = null;
function audioCtx() {
  if (!actx) {
    actx = new (window.AudioContext || window.webkitAudioContext)();
    noiseBuf = actx.createBuffer(1, actx.sampleRate, actx.sampleRate);
    const d = noiseBuf.getChannelData(0);
    for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
  }
  return actx;
}
function stopAllPlayback() { if (player) player.stop(); }
const isOpenHat = (l) => l.includes("hat") && l.includes("open");
function voice(ctx, lane, t, vel) {
  const out = ctx.createGain(); out.connect(ctx.destination);
  const noise = (type, freq, dur, q) => {
    const n = ctx.createBufferSource(); n.buffer = noiseBuf;
    const f = ctx.createBiquadFilter(); f.type = type; f.frequency.value = freq; if (q) f.Q.value = q;
    n.connect(f); f.connect(out); n.start(t, Math.random() * 0.5); n.stop(t + dur + 0.02);
  };
  const tone = (type, f0, f1, dur) => {
    const o = ctx.createOscillator(); o.type = type; o.frequency.setValueAtTime(f0, t);
    o.frequency.exponentialRampToValueAtTime(f1, t + dur * 0.6); o.connect(out); o.start(t); o.stop(t + dur + 0.02);
  };
  const env = (peak, dur) => { out.gain.setValueAtTime(peak * vel, t); out.gain.exponentialRampToValueAtTime(0.001, t + dur); };
  const l = lane.toLowerCase();
  if (l.includes("kick")) { env(0.9, 0.35); tone("sine", 150, 45, 0.35); }
  else if (l.includes("snare")) { env(0.5, 0.2); noise("highpass", 1800, 0.2); tone("triangle", 190, 150, 0.12); }
  else if (l.includes("clap")) { env(0.5, 0.25); noise("bandpass", 1500, 0.25, 1.2); }
  else if (isOpenHat(l)) { env(0.25, 0.22); noise("highpass", 7000, 0.22); }
  else if (l.includes("hat")) { env(0.25, 0.045); noise("highpass", 7000, 0.045); }
  else if (l.includes("tom")) { env(0.6, 0.3); tone("sine", 170, 120, 0.3); }
  else { env(0.4, 0.12); tone("sine", 800, 700, 0.12); }
}
// Lookahead scheduler: a 25 ms timer queues every step starting within the next 150 ms. getPat/getBpm are read per step, so edits apply live.
function startPlayer(getPat, getBpm, onStep, onEnd) {
  stopAllPlayback();
  const ctx = audioCtx(); if (ctx.state === "suspended") ctx.resume();
  let step = 0, next = ctx.currentTime + 0.06, alive = true, timers = [];
  const tick = () => {
    while (next < ctx.currentTime + 0.15) {
      const t = next, st = step, pat = getPat();
      for (const [lane, p] of Object.entries(pat)) if (typeof p === "string" && p[st] && p[st] !== ".") voice(ctx, lane, t, p[st] === "x" ? 1 : 0.55);
      timers.push(setTimeout(() => alive && onStep(st), Math.max(0, (t - ctx.currentTime) * 1000)));
      if (timers.length > 64) timers = timers.slice(-32);
      next += 60 / getBpm() / 4; step = (step + 1) % 16;
    }
  };
  const iv = setInterval(tick, 25); tick();
  const me = { stop() { if (!alive) return; alive = false; clearInterval(iv); timers.forEach(clearTimeout); if (player === me) player = null; onEnd(); } };
  return (player = me);
}
// General MIDI drum map (channel 10): kick 36, snare 38, clap 39, closed hat 42, open hat 46, low tom 45, perc (cowbell) 56, anything else 37.
const GM_NOTE = (lane) => { const l = lane.toLowerCase(); return l.includes("kick") ? 36 : l.includes("snare") ? 38 : l.includes("clap") ? 39 : isOpenHat(l) ? 46 : l.includes("hat") ? 42 : l.includes("tom") ? 45 : l.includes("perc") ? 56 : 37; };
// Format 0, 480 PPQN, one 4/4 bar (1920 ticks), 120 ticks per 16th step, notes 60 ticks long.
function midiBytes(pattern, bpm, name) {
  const vlq = (n) => { const o = [n & 127]; while ((n >>= 7)) o.unshift((n & 127) | 128); return o; };
  const ev = [];
  for (const [lane, p] of Object.entries(pattern)) {
    if (typeof p !== "string") continue;
    [...p.slice(0, 16)].forEach((ch, i) => {
      if (ch === ".") return;
      const note = GM_NOTE(lane), vel = ch === "x" ? 110 : 70;
      ev.push([i * 120, 1, [0x99, note, vel]], [i * 120 + 60, 0, [0x89, note, 0]]);
    });
  }
  ev.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const us = Math.round(60e6 / bpm), nm = [...name].map((ch) => ch.charCodeAt(0) & 127);
  const tr = [0, 0xff, 0x03, nm.length, ...nm, 0, 0xff, 0x51, 3, us >> 16 & 255, us >> 8 & 255, us & 255, 0, 0xff, 0x58, 4, 4, 2, 24, 8];
  let last = 0;
  ev.forEach(([t, , bytes]) => { tr.push(...vlq(t - last), ...bytes); last = t; });
  tr.push(...vlq(1920 - last), 0xff, 0x2f, 0);
  const len = tr.length;
  return new Uint8Array([0x4d, 0x54, 0x68, 0x64, 0, 0, 0, 6, 0, 0, 0, 1, 1, 0xe0, 0x4d, 0x54, 0x72, 0x6b, len >> 24 & 255, len >> 16 & 255, len >> 8 & 255, len & 255, ...tr]);
}
function downloadBytes(bytes, fileName, type) {
  const url = URL.createObjectURL(new Blob([bytes], { type }));
  const a = h("a", { href: url, download: fileName });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function diffText(pattern, prev) {
  const parts = [];
  for (const lane of Object.keys(pattern)) {
    if (!prev || typeof prev[lane] !== "string") continue;
    let add = 0, drop = 0;
    [...pattern[lane]].forEach((ch, i) => { const was = prev[lane][i] !== "."; const is = ch !== "."; if (is && !was) add++; if (!is && was) drop++; });
    if (add || drop) parts.push(`${laneName(lane)} ${[add ? "+" + add : "", drop ? "−" + drop : ""].filter(Boolean).join(" ")}`);
  }
  return parts.length ? parts.join(", ") : "same as previous section";
}

const TPL_TIPS = ["A hit on this step in the template.", "A lighter hit in the template, such as an accent or open hat.", "No hit on this step."];
const legend = (measured = true) => h("div", { class: "grid-legend" },
  h("span", { tabindex: "0", "data-tip": !measured ? TPL_TIPS[0] : "Strong: at least 72% of the loudest hit in this lane." }, h("i", { class: "sw full" }), measured ? "Strong" : "Hit"),
  h("span", { tabindex: "0", "data-tip": !measured ? TPL_TIPS[1] : "Softer: 50% to 72% of the loudest hit in this lane. Often a ghost note, a quieter hat, or a hit that only lands in some bars." }, h("i", { class: "sw half" }), measured ? "Softer" : "Lighter"),
  h("span", { tabindex: "0", "data-tip": !measured ? TPL_TIPS[2] : "None: under 50% of the loudest hit in this lane, so treated as silence." }, h("i", { class: "sw" }), "None"),
  h("span", { tabindex: "0", "data-tip": "The grid covers one typical bar of the music, split into 16 equal steps from left to right. Every group of 4 steps is one beat. Each lane (kick, snare, hat) shows which of those steps it plays on." }, "1 bar, 16 steps"));

// Mirrors _style() in the suggestions stage: which tempo range picks which template style.
const STYLE_INFO = {
  four_floor: ["118-136 BPM", "Four on the floor means a kick on every beat, typical of house and techno."],
  dnb: ["160-185 BPM", "Drum and bass leans on a fast, syncopated kick and snare."],
  halftime: ["136-152 BPM with a percussive track", "Halftime puts the snare on beat 3 so the groove feels half as fast."],
  hiphop: ["80-105 BPM", "Hip-hop grooves sit on a laid-back kick and backbeat snare."],
  generic: ["any other tempo", "A plain backbeat: kick on 1, snare on 2 and 4."],
};
const styleTip = (d) => {
  const [range, what] = STYLE_INFO[d.style_key] || ["", ""];
  return `${what} Chosen only because this track is ${d.bpm} BPM and ${range} maps to this style. The track's actual rhythm was not checked.`.trim();
};

function drumGrid(s, card) {
  const det = s.data.origin === "detected";
  const wrap = h("div", { class: "sug" }, det
      ? h("b", { class: "tip-title", tabindex: "0", "data-tip": "Measured from the audio. For each bar in the section, the app takes the jump in loudness at each of 16 steps within each lane's frequency band, then averages across the section's bars." }, s.title)
      : h("b", {}, s.title),
    det || !s.data.origin ? null : h("span", { class: "origin tpl" }, "Template for ", h("span", { class: "term", tabindex: "0", "data-tip": styleTip(s.data) }, s.data.style || "this tempo")));
  const secs = s.data.by_section && s.data.by_section.length ? s.data.by_section : null;
  const letters = secs ? [...new Set(secs.map((x) => x.letter || x.label))] : [];
  const bus = sectionBus;
  let sel = secs ? secs.reduce((best, x, k) => ((x.energy || 0) > (secs[best].energy || 0) ? k : best), 0) : 0, showChg = true, nowStep = -1;
  const edits = {}; // per-section copies of the pattern; the originals stay untouched and nothing is saved
  const cur = () => edits[sel] || (secs ? secs[sel].pattern : s.data.pattern);
  const colorOf = (x) => SEC_COLORS[letters.indexOf(x.letter || x.label) % SEC_COLORS.length];
  const name = (x) => `${cap(x.label)} ${x.letter || ""}`.trim();
  const secName = () => (secs ? name(secs[sel]) : "");
  const trackBpm = Math.round(s.data.bpm || (card.core && card.core.bpm) || 120);

  // tools row
  const status = h("span", { class: "seq-status muted", role: "status", "aria-live": "polite" });
  const say = (m) => { status.textContent = m; };
  const playBtn = h("button", { type: "button", class: "seq-play", "aria-pressed": "false" }, "▶ Play");
  const bpmIn = h("input", { type: "number", min: "40", max: "240", step: "1", value: String(trackBpm), "aria-label": "Playback tempo in BPM", class: "seq-bpm" });
  const bpmVal = () => Math.max(40, Math.min(240, +bpmIn.value || trackBpm));
  const resetBtn = h("button", { type: "button", class: "seq-reset", hidden: "" }, "Reset edits");
  const setNow = (st) => {
    nowStep = st;
    wrap.querySelectorAll(".drum-grid i.now").forEach((el) => el.classList.remove("now"));
    if (st >= 0) wrap.querySelectorAll(`.drum-grid i[data-step="${st}"]`).forEach((el) => el.classList.add("now"));
  };
  const ui = (on) => { playBtn.textContent = on ? "■ Stop" : "▶ Play"; playBtn.setAttribute("aria-pressed", String(on)); if (!on) setNow(-1); };
  playBtn.onclick = () => {
    if (playBtn.getAttribute("aria-pressed") === "true") { stopAllPlayback(); return; }
    try { startPlayer(cur, bpmVal, setNow, () => ui(false)); ui(true); say(""); }
    catch (e) { say("Audio isn't available in this browser."); }
  };
  resetBtn.onclick = () => { delete edits[sel]; paint(true); say("Edits reset."); };
  const fileName = () => `${(card.source.title || "Pattern").replace(/[^\w\- ().]+/g, "").trim() || "Pattern"}${secs ? " - " + secName() : ""}.mid`;
  const exportBtn = h("button", { type: "button", "data-tip": "Saves a one-bar MIDI file. Kick, snare, hat and so on use General MIDI drum notes, so it loads into any DAW or sequencer." }, "Export MIDI");
  exportBtn.onclick = () => {
    const file = fileName(), bytes = midiBytes(cur(), bpmVal(), file.replace(/\.mid$/, ""));
    downloadBytes(bytes, file, "audio/midi");
    say(`Saved "${file}" (${bpmVal()} BPM, 1 bar).`);
  };
  const textOf = () => {
    const pat = cur(), lanes = Object.entries(pat).filter(([, v]) => typeof v === "string"), w = Math.max(...lanes.map(([k]) => k.length));
    return [`${card.source.title || "Pattern"}${secs ? " · " + secName() : ""} · ${bpmVal()} BPM`, ...lanes.map(([k, v]) => `${k.padEnd(w)}  ${v}`), "x = strong, o = softer, . = none"].join("\n");
  };
  const copyBtn = h("button", { type: "button", "data-tip": "Copies the pattern as plain text, for notes or a chat." }, "Copy as text");
  copyBtn.onclick = async () => {
    try { await navigator.clipboard.writeText(textOf()); say("Copied the pattern as text."); }
    catch (e) { say("Couldn't copy to the clipboard from here."); }
  };
  const tools = h("div", { class: "seq-tools" }, playBtn,
    h("label", { class: "seq-tempo" }, bpmIn, h("span", {}, "BPM")),
    exportBtn, copyBtn, resetBtn, h("span", { class: "muted seq-hint" }, "Click any step to edit"), status);

  const body = h("div", { class: "sec-body" });
  const toggleStep = (lane, i) => {
    const src = cur(), ch = src[lane][i];
    edits[sel] = { ...src, [lane]: src[lane].slice(0, i) + (ch === "." ? "x" : ch === "x" ? "o" : ".") + src[lane].slice(i + 1) };
    paint(true);
    const el = wrap.querySelector(`.drum-grid i[data-lane="${lane}"][data-step="${i}"]`); if (el) el.focus();
  };
  const tabs = h("div", { class: "sec-tabs", role: "tablist", "aria-label": "Section" });
  const note = h("div", { class: "sec-note" });
  const toggle = h("input", { type: "checkbox", checked: "" });
  toggle.addEventListener("change", () => { showChg = toggle.checked; paint(true); });
  function paint(quiet) {
    resetBtn.hidden = !edits[sel];
    if (!secs) { body.replaceChildren(...laneRows(cur(), null, det, toggleStep, s.data.lane_help)); setNow(nowStep); return; }
    [...tabs.children].forEach((b, k) => b.setAttribute("aria-selected", String(k === sel)));
    const sec = secs[sel], before = sel > 0 ? secs[sel - 1] : null;
    body.replaceChildren(...laneRows(cur(), before && showChg ? before.pattern : null, true, toggleStep, s.data.lane_help));
    setNow(nowStep);
    note.replaceChildren(
      h("b", {}, name(sec)), h("span", { class: "muted" }, ` · bars ${sec.start_bar}-${sec.end_bar}`),
      h("div", { class: "muted sec-diff" }, before
        ? ["Compared with ", h("button", { type: "button", class: "sec-link", title: "Go to this section", style: `--c:${colorOf(before)}`, onclick: () => { sel -= 1; paint(); } }, name(before)), `: ${diffText(sec.pattern, before.pattern)}`]
        : "First section, nothing before it to compare with"));
    if (quiet !== true) bus.dispatchEvent(new CustomEvent("section", { detail: { idx: sec.idx, from: "tabs" } }));
  }
  if (!secs) { wrap.append(tools, body, h("div", { class: "grid-foot" }, legend(det))); paint(true); return wrap; }
  bus.addEventListener("section", (e) => {
    if (e.detail.from !== "chart") return;
    const k = secs.findIndex((x) => x.idx === e.detail.idx);
    if (k >= 0) { sel = k; paint(true); }
  });
  secs.forEach((x, k) => tabs.append(h("button", {
    type: "button", role: "tab", class: "sec-tab", style: `--c:${colorOf(x)}`,
    onclick: () => { sel = k; paint(); },
  }, name(x))));
  wrap.append(tabs, note, tools, body, h("div", { class: "grid-foot" }, legend(true), h("label", { class: "chg-toggle" }, toggle, "Outline steps that changed")));
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
