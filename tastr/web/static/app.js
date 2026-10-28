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

function renderCard(c) {
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
        h("span", { class: "chip", style: `--s:${isNum(t.score) ? t.score : 0}`, title: isNum(t.score) ? t.score.toFixed(2) : "" }, t.tag))));
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
  const W = 700, H = 200, L = 30, R = 8, T = 8, B = 22;
  const times = (series && series.times) || [];
  const vals = (series && series.values) || [];
  const end = Math.max(duration || 0, times.length ? times[times.length - 1] : 0, ...sections.map((s) => s.end || 0), 1);
  const x = (t) => L + (t / end) * (W - L - R);
  const y = (v) => T + (1 - Math.max(0, Math.min(1, v))) * (H - T - B);
  const s = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Energy over time with sections" });
  const letters = [...new Set(sections.map((x) => x.letter || x.label))];
  sections.forEach((sec) => {
    if (!isNum(sec.start) || !isNum(sec.end)) return;
    const color = SEC_COLORS[letters.indexOf(sec.letter || sec.label) % SEC_COLORS.length];
    s.append(svg("rect", { x: x(sec.start), y: T, width: Math.max(1, x(sec.end) - x(sec.start)), height: H - T - B, fill: color, opacity: 0.18 }));
    const label = [sec.label, sec.letter].filter(Boolean).join(" ");
    s.append(svg("text", { class: "lbl", x: x(sec.start) + 3, y: T + 12 }, document.createTextNode(label)));
  });
  [0, 0.5, 1].forEach((v) => s.append(svg("text", { x: 2, y: y(v) + 4 }, document.createTextNode(v))));
  const pts = times.map((t, i) => [t, vals[i]]).filter(([t, v]) => isNum(t) && isNum(v));
  if (pts.length > 1) {
    const line = pts.map(([t, v]) => `${x(t).toFixed(1)},${y(v).toFixed(1)}`);
    s.append(svg("polygon", { points: `${x(pts[0][0])},${y(0)} ${line.join(" ")} ${x(pts[pts.length - 1][0])},${y(0)}`, fill: "var(--accent)", opacity: 0.25 }));
    s.append(svg("polyline", { points: line.join(" "), fill: "none", stroke: "var(--accent)", "stroke-width": 2 }));
  }
  const step = end > 300 ? 60 : end > 120 ? 30 : 15;
  for (let t = 0; t <= end; t += step) s.append(svg("text", { x: x(t), y: H - 6 }, document.createTextNode(mmss(t))));
  return s;
}

function drumGrid(s) {
  const wrap = h("div", { class: "sug" }, h("b", {}, s.title));
  for (const [lane, pat] of Object.entries(s.data.pattern)) {
    if (typeof pat !== "string") continue;
    const cells = [...pat.slice(0, 16)].map((ch, i) =>
      h("i", { class: [i % 4 === 0 && i ? "beat" : "", ch === "x" ? "full" : ch === "o" ? "half" : ""].join(" ").trim() }));
    wrap.append(h("div", { class: "drum-lane" }, h("span", { class: "name" }, lane), h("div", { class: "drum-grid" }, cells)));
  }
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
