// A track's earlier cards (card_history): each re-analysis keeps the card it replaces, the newest 5 per track.
// GET /api/tracks/{id}/history -> [{id, analyzed_at, replaced_at, stages, core_version, bpm, key_name, camelot, energy}],
// newest first; [] until the track is re-analyzed. The card shows a line when the newest earlier card's numbers moved
// ("Changed in the core 0.6.0 update: BPM 117.45 → 118.79"), and Metadata › Earlier analyses lists them all.

import { $, api, h, isNum } from "./core.js";

// "Oct 3, 2026" from an ISO time (or the metadata's "2026-10-29 01:12 UTC").
export const fmtWhen = (iso) => {
  const d = new Date(String(iso).replace(/^(\d{4}-\d\d-\d\d) (\d\d:\d\d) UTC$/, "$1T$2Z"));
  return isNaN(d) ? String(iso) : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
};
export const histNum = (v) => (isNum(v) ? (Math.round(v * 100) / 100).toFixed(2) : "–");
const histEnergy = (v) => (isNum(v) ? v.toFixed(2) : "–");
const curCore = (c) => (c.stages_run && c.stages_run.core) || "?";

// What moved between the newest earlier card and the current one: BPM by 0.05 or more, a different key, energy by 0.02 or more.
export function histMoves(c) {
  const e = (c.history || [])[0], core = c.core || {}, cam = core.key && core.key.camelot;
  if (!e) return [];
  const out = [];
  if (isNum(e.bpm) && isNum(core.bpm) && Math.abs(e.bpm - core.bpm) >= 0.05) out.push(["BPM", histNum(e.bpm), histNum(core.bpm)]);
  if (e.camelot && cam && e.camelot !== cam) out.push(["Key", e.camelot, cam]);
  if (isNum(e.energy) && isNum(core.energy) && Math.abs(e.energy - core.energy) >= 0.02) out.push(["Energy", histEnergy(e.energy), histEnergy(core.energy)]);
  return out;
}

// Open Metadata and bring its Earlier analyses table into view.
function showHistory(c) {
  const d = document.querySelector('#card details[data-sec="meta"]');
  if (d && !d.open) d.open = true;  // its toggle event remembers it, like a click
  const t = document.getElementById(`hist-${c.id}`);
  if (t) t.scrollIntoView({ block: "center" });
}

// The card line, after the engine line: "Changed in the core 0.6.0 update: BPM 117.45 → 118.79 · Earlier analyses".
// Hidden while the history loads, and when nothing moved.
export function historyLine(c) {
  const e = (c.history || [])[0], mv = histMoves(c);
  if (!e || !mv.length) return h("p", { class: "hist-line", hidden: "" });
  const now = curCore(c);
  const upd = e.core_version && e.core_version !== now ? `the core ${now} update` : `re-analysis on ${fmtWhen(e.replaced_at)}`;
  return h("p", { class: "hist-line", title: `Re-analyzed ${fmtWhen(e.replaced_at)}. The earlier card was from ${fmtWhen(e.analyzed_at)}, engine core ${e.core_version || "?"}.` },
    h("span", { class: "muted" }, `Changed in ${upd}: `),
    ...mv.flatMap(([k, a, b], i) => [i ? h("span", { class: "muted" }, " · ") : null, h("span", { class: "hist-mv" }, `${k} `, h("s", {}, a), " → ", h("b", {}, b))]),
    h("span", { class: "muted" }, " · "),
    h("button", { type: "button", class: "linklike", onclick: () => showHistory(c) }, "Earlier analyses"));
}

// Metadata › Earlier analyses: the current card first, then each kept card, newest first; a value that differs from
// the row above is marked. Empty (hidden) while the history loads.
export function historyBlock(c) {
  const hs = c.history;
  if (!Array.isArray(hs)) return h("div", { class: "meta-hist", id: `hist-${c.id}`, hidden: "" });
  const core = c.core || {}, an = c.metadata && c.metadata.Analysis;
  const rows = [{ now: true, when: an && an["Analyzed at"] ? fmtWhen(an["Analyzed at"]) : null, core_version: curCore(c),
    bpm: core.bpm, camelot: core.key && core.key.camelot, energy: core.energy },
  ...hs.map((x) => ({ ...x, when: x.analyzed_at ? fmtWhen(x.analyzed_at) : null }))];
  const cell = (v, prev, fmt) => { const t = fmt(v), p = prev === undefined ? null : fmt(prev);
    return h("td", { class: p != null && t !== "–" && p !== "–" && t !== p ? "hist-diff" : null }, t); };
  const cam = (v) => v || "–";
  return h("div", { class: "meta-hist", id: `hist-${c.id}` }, h("h4", {}, "Earlier analyses"),
    hs.length ? h("div", { class: "table-wrap" }, h("table", { class: "hist-table" },
      h("thead", {}, h("tr", {}, ...["", "Analyzed", "Engine", "BPM", "Key", "Energy"].map((t) => h("th", {}, t)))),
      h("tbody", {}, ...rows.map((r, i) => { const up = i ? rows[i - 1] : null;
        return h("tr", { class: r.now ? "now" : null }, h("td", {}, r.now ? h("b", {}, "Now") : "Earlier"), h("td", {}, r.when || "–"),
          h("td", {}, `core ${r.core_version || "?"}`),
          cell(r.bpm, up ? up.bpm : undefined, histNum), cell(r.camelot, up ? up.camelot : undefined, cam), cell(r.energy, up ? up.energy : undefined, histEnergy)); }))))
      : null,
    h("p", { class: "muted hist-note" }, hs.length ? "Tastr keeps the last 5 cards a track had before each re-analysis. Marked values differ from the row above."
      : "None yet. When this track is re-analyzed, Tastr keeps the card it had, the last 5."));
}

// The history for a card that came without it (a re-analysis, a retag or a lookup returns a fresh card): fetch it,
// then fill in the line and the table in place, if this card is still the one shown. An older server has no history.
export const fetchHistory = (id) => api(`/api/tracks/${id}/history`).catch(() => null);
let painting = null;
export function loadHistory(c) {
  painting = c;
  if (c.history !== undefined) return;
  c.history = null;  // asked; not again for this card
  fetchHistory(c.id).then((hs) => {
    if (!Array.isArray(hs)) return;
    c.history = hs;
    if (painting !== c || $("card").dataset.id !== String(c.id)) return;
    document.querySelector("#card .hist-line")?.replaceWith(historyLine(c));
    document.querySelector("#card .meta-hist")?.replaceWith(historyBlock(c));
  });
}
