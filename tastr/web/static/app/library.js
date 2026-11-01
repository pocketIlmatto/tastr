// The track list: search, sort and filters, placeholder rows while files analyze, the collapsible sidebar and quick jump.

import { $, api, currentId, h, isNum, labels, mmss, progBar, reviewRows, setTracks, tracks } from "./core.js";
import { jobs, renderBulk, sel, selecting, setSelecting, toggleSel } from "./bulk.js";
import { loadReview, renderReview } from "./labels.js";
import { openTrack } from "./card.js";
import { A_STEPS } from "./upload.js";

export const LIB_CLEAR = () => ({ q: "", vibes: [], key: "", bmin: "", bmax: "", up: "", upFrom: "", upTo: "", lab: "" });
export const lib = { sort: "new", ...LIB_CLEAR() };
// Upload dates, as the sidebar shows them: the time if today, the weekday this week, else the date.
const DAY = 864e5, dayStart = (t) => { const d = new Date(t); d.setHours(0, 0, 0, 0); return +d; };
export const fmtUp = (t) => {
  if (!t) return "";
  const today = dayStart(Date.now()), d = new Date(t);
  if (t >= today) return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (t >= today - DAY) return "Yesterday";
  if (t >= today - 6 * DAY) return d.toLocaleDateString([], { weekday: "short" });
  return d.toLocaleDateString([], { month: "short", day: "numeric", ...(d.getFullYear() !== new Date().getFullYear() ? { year: "numeric" } : {}) });
};
export const fmtUpLong = (t) => new Date(t).toLocaleString([], { weekday: "short", month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
// Filters take a filter state (the sidebar's `lib`, or the Files view's own), so both lists filter the same way.
function upMatch(t, f) {
  const today = dayStart(Date.now());
  switch (f.up) {
    case "today": return t >= today;
    case "yesterday": return t >= today - DAY && t < today;
    case "7": return t >= today - 6 * DAY;
    case "30": return t >= today - 29 * DAY;
    case "old": return t < today - 29 * DAY;
    case "range": {
      const a = f.upFrom ? dayStart(new Date(f.upFrom + "T00:00")) : -Infinity, b = f.upTo ? dayStart(new Date(f.upTo + "T00:00")) + DAY : Infinity;
      return t >= a && t < b;
    }
    default: return true;
  }
}
export const keyRank = (k) => { const m = /^(\d+)([AB])$/.exec(k || ""); return m ? +m[1] * 2 + (m[2] === "B" ? 1 : 0) : 999; };
export const SORTS = {
  new: (a, b) => b.uploaded - a.uploaded || b.id - a.id,
  old: (a, b) => a.uploaded - b.uploaded || a.id - b.id,
  title: (a, b) => (a.title || "").localeCompare(b.title || ""),
  bpm: (a, b) => (a.bpm ?? 999) - (b.bpm ?? 999),
  bpmd: (a, b) => (b.bpm ?? -1) - (a.bpm ?? -1),
  key: (a, b) => keyRank(a.camelot) - keyRank(b.camelot) || (a.bpm ?? 0) - (b.bpm ?? 0),
  energy: (a, b) => (b.energy ?? -1) - (a.energy ?? -1),
  len: (a, b) => (b.duration ?? 0) - (a.duration ?? 0),
};
export const filtersOn = (f) => !!(f.q || f.vibes.length || f.key || f.bmin || f.bmax || f.up || f.lab);
export const libActive = () => filtersOn(lib);
// Search, sort and filters run in the browser, which is fine for a few hundred tracks. `f` is a filter state like `lib`.
export function matchTracks(f, list = tracks) {
  const q = f.q.trim().toLowerCase(), lo = f.bmin === "" ? null : +f.bmin, hi = f.bmax === "" ? null : +f.bmax;
  const need = f.lab === "need" ? new Set(reviewRows.map((r) => r.track_id)) : null;
  return list.filter((t) =>
    (!q || `${t.title} ${t.artist || ""}`.toLowerCase().includes(q)) &&
    f.vibes.every((v) => (t.tags || []).includes(v)) && (!f.key || keyMatch(t.camelot, f)) &&
    (lo === null || (isNum(t.bpm) && t.bpm >= lo)) && (hi === null || (isNum(t.bpm) && t.bpm <= hi)) &&
    upMatch(t.uploaded, f) && (!f.lab || (need ? need.has(t.id) : (f.lab === "yes") === labels.has(t.id))));
}
export const visibleTracks = () => matchTracks(lib).sort(SORTS[lib.sort]);
// lib.key is a Camelot code, or "8A~" for that key plus the keys that mix with it (one step round the wheel, or the relative major/minor).
export const camelotNear = (k) => { const m = /^(\d+)([AB])$/.exec(k || ""); if (!m) return [k]; const n = +m[1], l = m[2], o = l === "A" ? "B" : "A", w = (d) => ((n + d + 11) % 12) + 1; return [k, `${w(-1)}${l}`, `${w(1)}${l}`, `${n}${o}`]; };
const keyMatch = (cam, f) => (f.key.endsWith("~") ? camelotNear(f.key.slice(0, -1)).includes(cam) : cam === f.key);
const vibeLabel = () => lib.vibes.join(" + ");
// The sidebar selects follow the card filters: an extra "dark + driving" or "8A + compatible" option while one is on.
function syncFilterSelects() {
  const vs = $("lib-vibe"), ks = $("lib-key");
  vs.querySelector("[value='__multi']")?.remove(); ks.querySelector("option[value$='~']")?.remove();
  if (lib.vibes.length > 1) vs.append(h("option", { value: "__multi" }, vibeLabel()));
  else if (lib.vibes.length === 1 && ![...vs.options].some((o) => o.value === lib.vibes[0])) vs.append(h("option", { value: "__multi" }, vibeLabel()));
  if (lib.key.endsWith("~")) ks.append(h("option", { value: lib.key }, `${lib.key.slice(0, -1)} + compatible`));
  vs.value = vs.querySelector("[value='__multi']") ? "__multi" : lib.vibes[0] || ""; ks.value = lib.key;
}
function fillFilterOptions() {
  const vibes = [...new Set(tracks.flatMap((t) => t.tags || []))].sort(), keys = [...new Set(tracks.map((t) => t.camelot).filter(Boolean))].sort((a, b) => keyRank(a) - keyRank(b));
  $("lib-vibe").replaceChildren(h("option", { value: "" }, "Any vibe"), ...vibes.map((v) => h("option", { value: v }, v)));
  $("lib-key").replaceChildren(h("option", { value: "" }, "Any key"), ...keys.map((k) => h("option", { value: k }, k)));
  syncFilterSelects();
}
export function syncTools() {
  $("lib-q").value = lib.q; $("lib-sort").value = lib.sort; syncFilterSelects(); $("lib-bmin").value = lib.bmin; $("lib-bmax").value = lib.bmax;
  $("lib-up").value = lib.up; $("lib-up-from").value = lib.upFrom; $("lib-up-to").value = lib.upTo; $("lib-up-range").hidden = lib.up !== "range"; $("lib-lab").value = lib.lab;
}
export async function loadTracks() {
  const [rows] = await Promise.all([api("/api/tracks"), loadReview(false)]);
  setTracks(rows);
  for (const t of tracks) t.uploaded = Date.parse(t.created_at || t.analyzed_at) || 0; // when it was uploaded, for the list, sort and filter
  fillFilterOptions();
}
export async function refresh(selectId) {
  await loadTracks();
  // A freshly analyzed track must be visible, so show everything, newest first.
  if (selectId != null) { Object.assign(lib, { ...LIB_CLEAR(), sort: "new" }); syncTools(); }
  renderList();
  if (selectId != null) openTrack(selectId);
}
// Clicking a vibe chip on the card filters the track list to that vibe; more chips narrow it (a track needs every picked vibe).
export function vibeFilter(tag) {
  lib.vibes = lib.vibes.includes(tag) ? lib.vibes.filter((v) => v !== tag) : [...lib.vibes, tag];
  showFiltered(lib.vibes.includes(tag));
}
// Clicking the key or BPM on the card does the same: compatible keys, or the BPM ± 5. Clicking it again turns it off.
export function keyFilter(cam) { const k = cam + "~"; lib.key = lib.key === k ? "" : k; showFiltered(!!lib.key); }
export function bpmFilter(bpm) {
  const lo = String(Math.round(bpm - 5)), hi = String(Math.round(bpm + 5)), on = lib.bmin === lo && lib.bmax === hi;
  lib.bmin = on ? "" : lo; lib.bmax = on ? "" : hi; showFiltered(!on);
}
function showFiltered(added) {
  syncTools();
  if (added && libCollapsed) setLibCollapsed(false);
  renderList();
  if (added && matchMedia("(max-width: 700px)").matches) $("lib").scrollIntoView({ behavior: "smooth", block: "start" });
}
// The card's own filter lines: one under the vibe chips, one under the key/BPM stats, each with its own Clear.
export function syncCardFilters(n) {
  document.querySelectorAll("#card .chip[data-tag]").forEach((c) => { const on = lib.vibes.includes(c.dataset.tag); c.classList.toggle("on", on); c.setAttribute("aria-pressed", String(on)); });
  const bpmOn = lib.bmin !== "" || lib.bmax !== "";
  document.querySelectorAll("#card .stat-btn").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.f === "key" ? !!lib.key : bpmOn)));
  const count = `${n} track${n === 1 ? "" : "s"}`;
  document.querySelectorAll("#card .card-filter").forEach((el) => {
    const vibe = el.dataset.kind === "vibe";
    const parts = vibe ? (lib.vibes.length ? [vibeLabel()] : [])
      : [lib.key ? (lib.key.endsWith("~") ? `${lib.key.slice(0, -1)} + compatible keys` : lib.key) : null, bpmOn ? `${lib.bmin || "…"}–${lib.bmax || "…"} BPM` : null].filter(Boolean);
    el.hidden = !parts.length;
    if (!parts.length) { el.replaceChildren(); return; }
    el.replaceChildren(h("span", { class: "cf-dot", "aria-hidden": "true" }), h("span", {}, `Track list: ${parts.join(" · ")} · ${count}`),
      h("button", { type: "button", class: "cf-clear", onclick: () => { if (vibe) lib.vibes = []; else { lib.key = ""; lib.bmin = ""; lib.bmax = ""; } syncTools(); renderList(); } }, vibe ? "Clear vibes" : "Clear"));
  });
}
function clearFilters() { Object.assign(lib, LIB_CLEAR()); syncTools(); renderList(); }

// Placeholder rows for files being analyzed (or that failed), shown above the saved tracks.
export const pending = [];
const cleanName = (n) => String(n || "").replace(/\.[a-z0-9]+$/i, "").replace(/^https?:\/\/(www\.)?/, "").slice(0, 48) || "Untitled";
// The server's error can be a multi-line ffmpeg dump with its own file paths; show a short version and keep the full text in a tooltip.
function shortError(err) {
  if (!err) return "";
  if (/^DecodeError/.test(err)) return "It couldn't be decoded as audio.";
  const line = err.split("\n")[0].replace(/\S*[\\/]uploads[\\/]\w{8}_/g, "");
  return line.length > 200 ? line.slice(0, 197) + "…" : line;
}
// One placeholder row: the step it's on, elapsed time and a bar while it runs; where it stopped if it failed.
function pendingLi(p) {
  const drop = () => { pending.splice(pending.indexOf(p), 1); renderList(); };
  if (p.failed) {
    const at = p.stage && A_STEPS.find(([k]) => k === p.stage);
    // `why`: a hosted upload that stopped before the analysis (TAS-66), said in full, maybe with a Retry.
    return h("li", { class: "pending failed", "aria-label": `${cleanName(p.name)}: ${p.why ? "not uploaded" : "could not be analyzed"}` },
      h("div", { class: "t" }, cleanName(p.name)),
      h("div", { class: "s" }, p.why || `Couldn't analyze this file${at ? ` (it stopped while ${at[3]})` : ""}. `, h("span", { class: "p-acts" },
        p.retry ? h("button", { type: "button", class: "linklike", onclick: () => { drop(); p.retry(); } }, "Retry") : null,
        h("button", { type: "button", class: "linklike", onclick: drop }, "Dismiss"))),
      p.error ? h("div", { class: "s why", title: p.error }, shortError(p.error)) : null);
  }
  const busy = p.active || p.uploading;  // a hosted upload runs before its turn to be analyzed
  return h("li", { class: "pending", "aria-busy": "true" },
    h("div", { class: "t" }, h("span", { class: "spin" + (busy ? "" : " idle"), "aria-hidden": "true" }), cleanName(p.name)),
    h("div", { class: "s" }, h("span", { class: p.retrying ? "p-retry" : null }, p.say || "Waiting…"), p.active && p.secs != null ? h("span", { class: "p-time", title: "Elapsed" }, mmss(p.secs)) : null),
    busy && p.frac != null ? progBar(p.frac, `${cleanName(p.name)} progress`) : null);
}
// Repaint just the placeholder rows (every second while analyzing), so the rest of the list keeps focus, hover and scroll.
export function paintPending() {
  const lis = $("track-list").querySelectorAll("li.pending");
  if (lis.length !== pending.length) { renderList(); return; }
  pending.forEach((p, i) => lis[i].replaceWith(pendingLi(p)));
}
export function renderList() {
  const ul = $("track-list"), top = ul.scrollTop;
  // Rows are rebuilt while bulk jobs run, so put keyboard focus back on the same track afterwards.
  const focused = ul.contains(document.activeElement) && document.activeElement.closest("li.trk");
  const focusId = focused ? focused.dataset.id : null;
  ul.replaceChildren();
  const shown = visibleTracks();
  $("lib-total").textContent = tracks.length ? `(${tracks.length})` : "";
  $("lib-n").textContent = libActive() ? `${shown.length} of ${tracks.length} tracks` : `${tracks.length} track${tracks.length === 1 ? "" : "s"}`;
  $("lib-clear").hidden = !libActive();
  $("lib-tools").hidden = !tracks.length;
  if (!tracks.length && !pending.length) ul.append(h("li", { class: "muted empty" }, "No tracks yet."));
  else if (!shown.length && !pending.length) ul.append(h("li", { class: "muted empty" }, "No tracks match. ", h("button", { type: "button", class: "linklike", onclick: clearFilters }, "Clear filters")));
  for (const p of pending) ul.append(pendingLi(p));
  const inQ = new Set(reviewRows.map((r) => r.track_id));
  for (const t of shown) {
    const picked = sel.has(t.id), job = jobs.get(t.id);
    ul.append(h("li", { class: "trk" + (t.id === currentId ? " active" : "") + (picked ? " picked" : ""), "data-id": t.id, tabindex: "0",
        "aria-current": t.id === currentId ? "true" : null, role: selecting ? "checkbox" : null, "aria-checked": selecting ? String(picked) : null,
        onclick: (e) => { if (selecting) toggleSel(t.id, e.shiftKey); else if (e.metaKey || e.ctrlKey) { setSelecting(true); toggleSel(t.id, false); } else openTrack(t.id); } },
      selecting ? h("span", { class: "pick" + (picked ? " on" : ""), "aria-hidden": "true" }) : null,
      h("div", { class: "trk-main" },
        h("div", { class: "t-row" }, h("div", { class: "t", title: t.title }, t.title || `Track ${t.id}`),
          t.uploaded ? h("time", { class: "up", datetime: new Date(t.uploaded).toISOString(), title: `Uploaded ${fmtUpLong(t.uploaded)}` }, fmtUp(t.uploaded)) : null),
        job ? [h("div", { class: "s job" + (job.error ? " fail" : ""), title: job.error || null }, job.error ? null : h("span", { class: "spin" + (job.active ? "" : " idle"), "aria-hidden": "true" }), job.text),
            job.frac != null ? progBar(job.frac, `${t.title || "Track"} progress`) : null]
          : h("div", { class: "s" }, h("span", { class: "s-txt" }, [t.artist, isNum(t.bpm) ? `${t.bpm.toFixed(0)} BPM` : null, t.camelot, mmss(t.duration)].filter(Boolean).join(" · ")),
            // status marks in a fixed slot next to the energy bar, so they line up whatever the text length
            h("span", { class: "st-marks" },
              labels.has(t.id) ? h("span", { class: "lb-tick", title: "You labeled this track", "aria-label": "You labeled this track" }, "✓") : null,
              inQ.has(t.id) ? h("span", { class: "rv-dot", title: "In your review list", "aria-label": "In your review list" }) : null),
            h("span", { class: "e-bar", "aria-hidden": "true", title: isNum(t.energy) ? `Energy ${t.energy.toFixed(2)}` : "" }, h("i", { style: `width:${isNum(t.energy) ? Math.round(t.energy * 100) : 0}%` }))))));
  }
  ul.scrollTop = top;
  if (focusId) { const li = ul.querySelector(`li.trk[data-id="${focusId}"]`); if (li) li.focus({ preventScroll: true }); }
  renderBulk(shown);
  syncCardFilters(shown.length);
  renderReview();
}

// Collapsible sidebar: a 36px rail on desktop, a folded header on phones. Remembered across reloads.
export let libCollapsed = false;
export function setLibCollapsed(on) {
  libCollapsed = on;
  document.querySelector(".split").classList.toggle("lib-collapsed", on);
  $("lib-body").hidden = on;
  const b = $("lib-collapse");
  const narrow = matchMedia("(max-width: 700px)").matches;
  b.setAttribute("aria-expanded", String(!on)); b.textContent = narrow ? (on ? "▸" : "▾") : on ? "»" : "«"; b.title = on ? "Show the track list" : "Hide the track list";
  try { localStorage.setItem("tastr-lib-collapsed", on ? "1" : "0"); } catch (_) {}
}

// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  try { libCollapsed = localStorage.getItem("tastr-lib-collapsed") === "1"; } catch (_) {}
  $("lib-collapse").addEventListener("click", () => setLibCollapsed(!libCollapsed));
  // Quick jump (phone, list collapsed): type, then tap a match or press Enter for the highlighted one. The list stays collapsed.
  (() => {
    const q = $("lib-quick-q"), ul = $("lib-quick-list");
    let hits = [], at = 0;
    const pick = (id) => { q.value = ""; ul.hidden = true; q.blur(); openTrack(id); $("card").scrollIntoView({ block: "start" }); };
    const paint = () => {
      ul.replaceChildren(...(hits.length ? hits.map((t, i) => {
        const li = h("li", { role: "option", id: `lq-${t.id}`, "aria-selected": String(i === at) },
          h("span", { class: "t" }, t.title || `Track ${t.id}`, t.artist ? h("span", { class: "muted" }, ` · ${t.artist}`) : null),
          h("span", { class: "m" }, [isNum(t.bpm) ? t.bpm.toFixed(0) : null, t.camelot].filter(Boolean).join(" · ")));
        // pointerdown, so the tap lands before the box loses focus and the list closes
        li.addEventListener("pointerdown", (e) => { e.preventDefault(); pick(t.id); });
        return li;
      }) : [h("li", { class: "none" }, "No tracks match.")]));
      q.setAttribute("aria-activedescendant", hits[at] ? `lq-${hits[at].id}` : "");
    };
    q.addEventListener("input", () => {
      const s = q.value.trim().toLowerCase();
      if (!s) { ul.hidden = true; return; }
      hits = tracks.filter((t) => `${t.title || ""} ${t.artist || ""}`.toLowerCase().includes(s)).slice(0, 8); at = 0;
      ul.hidden = false; paint();
    });
    q.addEventListener("keydown", (e) => {
      if (ul.hidden) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); at = (at + (e.key === "ArrowDown" ? 1 : hits.length - 1)) % Math.max(1, hits.length); paint(); }
      else if (e.key === "Enter" && hits[at]) { e.preventDefault(); pick(hits[at].id); }
      else if (e.key === "Escape") { q.value = ""; ul.hidden = true; }
    });
    q.addEventListener("blur", () => setTimeout(() => { ul.hidden = true; }, 150));
  })();
  document.querySelector(".lib-head").addEventListener("click", (e) => { if (libCollapsed && e.target.id !== "lib-collapse") setLibCollapsed(false); });
  setLibCollapsed(libCollapsed);
  matchMedia("(max-width: 700px)").addEventListener("change", () => setLibCollapsed(libCollapsed));
  $("lib-q").addEventListener("input", (e) => { lib.q = e.target.value; renderList(); });
  $("lib-sort").addEventListener("change", (e) => { lib.sort = e.target.value; renderList(); });
  $("lib-vibe").addEventListener("change", (e) => { const v = e.target.value; if (v !== "__multi") lib.vibes = v ? [v] : []; syncFilterSelects(); renderList(); });
  $("lib-key").addEventListener("change", (e) => { lib.key = e.target.value; syncFilterSelects(); renderList(); });
  $("lib-bmin").addEventListener("input", (e) => { lib.bmin = e.target.value; renderList(); });
  $("lib-bmax").addEventListener("input", (e) => { lib.bmax = e.target.value; renderList(); });
  $("lib-up").addEventListener("change", (e) => { lib.up = e.target.value; $("lib-up-range").hidden = lib.up !== "range"; renderList(); });
  $("lib-up-from").addEventListener("change", (e) => { lib.upFrom = e.target.value; renderList(); });
  $("lib-up-to").addEventListener("change", (e) => { lib.upTo = e.target.value; renderList(); });
  $("lib-lab").addEventListener("change", (e) => { lib.lab = e.target.value; renderList(); });
  $("lib-clear").addEventListener("click", clearFilters);
  $("lib-fbtn").addEventListener("click", () => { const f = $("lib-filters"), open = f.hidden; f.hidden = !open; $("lib-fbtn").setAttribute("aria-expanded", String(open)); });
  $("track-list").addEventListener("keydown", (e) => {
    const li = e.target.closest("li.trk"); if (!li) return;
    if (e.key === "Escape" && selecting) { e.preventDefault(); setSelecting(false); return; }
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); if (selecting) toggleSel(+li.dataset.id, e.shiftKey); else openTrack(+li.dataset.id); return; }
    const dir = { ArrowDown: "nextElementSibling", ArrowUp: "previousElementSibling" }[e.key];
    if (!dir) return;
    e.preventDefault();
    let n = li[dir]; while (n && !n.classList.contains("trk")) n = n[dir];
    if (n) n.focus();
  });
}
