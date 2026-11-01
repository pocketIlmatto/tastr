// Your labels and the review list (tastr/labels.py), and the state the card's label bar (label-bar.js) shares with them.

import { $, api, cap, currentId, h, isNum, labels, patternMarks, reviewRows, setLabels, setPatternMarks, setReviewRows, tracks } from "./core.js";
import { LIB_CLEAR, fmtUp, fmtUpLong, lib, libCollapsed, renderList, setLibCollapsed, syncTools } from "./library.js";
import { openTrack } from "./card.js";
import { accid } from "./daw.js";
import { downloadBytes } from "./sequencer.js";
import { setStatus } from "./upload.js";

// GET /api/review is the list (tracks you flagged first, then the most suspect), GET /api/labels the log; a label is
// PUT /api/tracks/{id}/labels, and PUT /api/tracks/{id}/review {state: "flagged" | "skipped" | null} puts a track in
// the list or takes it out. Labels are kept apart from the analysis: re-analyzing never changes them.
export let reviewOn = false, labelOpen = null, reviewDone = 0, reviewTab = "todo";
export const JSON_PUT = (body) => ({ method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export async function loadReview(paint = true) {
  try {
    const [q, labs, pats] = await Promise.all([api("/api/review?limit=1000"), api("/api/labels"), api("/api/pattern-reviews").catch(() => [])]);
    setReviewRows(q); setLabels(new Map(labs.map((l) => [l.track_id, l]))); setPatternMarks(pats);
  } catch (_) { /* an older server, or offline: keep what we had */ }
  if (paint) renderList();
}
export const reviewRow = (id) => reviewRows.find((r) => r.track_id === id);
export const inReview = (id) => !!reviewRow(id);
export let repaintLabel = null; // the open card's label bar repaints itself through this
export async function setInReview(ids, on) {
  try { await Promise.all(ids.map((id) => api(`/api/tracks/${id}/review`, JSON_PUT({ state: on ? "flagged" : "skipped" })))); }
  catch (e) { setStatus(e.message, true); }
  await loadReview();
  if (repaintLabel && ids.includes(repaintLabel.id)) repaintLabel.paint();
}
// Camelot code <-> key: 8B = C major, 8A = A minor, each step round the wheel = up a fifth. Names are plain text ("Bb major").
const MAJ_ASCII = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"], MIN_ASCII = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"];
export const camPc = (cam) => { const m = /^(\d+)([AB])$/.exec(cam || ""); return m ? ((((m[2] === "B" ? 0 : 9) + (+m[1] - 8) * 7) % 12) + 12) % 12 : null; };
export const CAM_NAME = (cam) => { const m = /^(\d+)([AB])$/.exec(cam || ""); if (!m) return cam; const pc = camPc(cam); return m[2] === "A" ? `${MIN_ASCII[pc]} minor` : `${MAJ_ASCII[pc]} major`; };
export const ALL_CAMS = [...Array(12)].flatMap((_, i) => [`${i + 1}A`, `${i + 1}B`]);
const PC_OF = { c: 0, "b#": 0, "c#": 1, db: 1, d: 2, "d#": 3, eb: 3, e: 4, fb: 4, "e#": 5, f: 5, "f#": 6, gb: 6, g: 7, "g#": 8, ab: 8, a: 9, "a#": 10, bb: 10, b: 11, cb: 11 };
const camOf = (pc, minor) => `${((7 * (minor ? (pc + 3) % 12 : pc)) % 12 + 7) % 12 + 1}${minor ? "A" : "B"}`;
// Any key spelling the server takes ("A minor", "Am", "F# major", "8A") -> its Camelot code, or null.
export function nameToCam(n) {
  const s = String(n || "").trim().replace(/♯/g, "#").replace(/♭/g, "b");
  let m = /^(1[0-2]|[1-9])\s*([ab])$/i.exec(s);
  if (m) return `${+m[1]}${m[2].toUpperCase()}`;
  m = /^([a-g])([#b]?)\s*:?\s*(major|minor|maj|min|m)?$/i.exec(s);
  if (!m) return null;
  const pc = PC_OF[(m[1] + m[2]).toLowerCase()];
  return pc == null ? null : camOf(pc, /^(minor|min|m)$/i.test(m[3] || ""));
}
export const camLabel = (cam) => `${accid(CAM_NAME(cam))} (${cam})`;
export function renderReview() {
  const q = reviewRows;
  $("lib-review-n").textContent = q.length ? String(q.length) : "";
  $("lib-review").setAttribute("aria-selected", String(reviewOn)); $("lib-tracks").setAttribute("aria-selected", String(!reviewOn));
  $("review").hidden = !reviewOn; $("track-list").hidden = reviewOn; $("lib-tools").hidden = reviewOn || !tracks.length;
  if (!reviewOn) return;
  $("rv-n-todo").textContent = `(${q.length})`; $("rv-n-done").textContent = `(${reviewedLog().length})`;
  $("rv-tab-todo").setAttribute("aria-selected", String(reviewTab === "todo")); $("rv-tab-done").setAttribute("aria-selected", String(reviewTab === "done"));
  $("review").querySelector(".rv-intro").hidden = reviewTab !== "todo";
  if (reviewTab === "done") { renderReviewLog(); return; }
  const ul = $("review-list"), top = ul.scrollTop;
  ul.replaceChildren(); ul.setAttribute("aria-label", "Tracks to review");
  if (!q.length) { ul.append(h("li", { class: "empty" }, reviewDone ? `All caught up: you labeled ${reviewDone} this session.` : "Nothing to review: every track looks right or is labeled.")); return; }
  for (const r of q.slice(0, 20)) {
    const pct = isNum(r.chance_right) ? Math.round(r.chance_right * 100) : null, title = r.title || `Track ${r.track_id}`;
    const open = () => openForReview(r.track_id);
    ul.append(h("li", { class: currentId === r.track_id ? "active" : "", "data-id": r.track_id, tabindex: "0", role: "button", "aria-label": `${title}: ${r.reasons.join(", ")}`,
        onclick: open, onkeydown: (e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); open(); } } },
      h("div", { class: "rv-top" }, h("span", { class: "rv-t", title }, title),
        r.mine ? h("span", { class: "rv-chance", title: "You added this one" }, "added by you")
          : pct != null ? h("span", { class: "rv-chance" + (pct < 40 ? " low" : ""), title: "How likely Tastr's BPM and key are right (the lower of the two)" }, `${pct}% likely right`) : null),
      h("div", { class: "rv-s" }, [isNum(r.bpm) ? `${r.bpm.toFixed(0)} BPM` : null, r.camelot ? `${r.camelot} ${accid(CAM_NAME(r.camelot))}` : null].filter(Boolean).join(" · ")),
      h("ul", { class: "rv-why" }, r.reasons.map((x) => h("li", {}, accid(x)))),
      h("button", { type: "button", class: "rv-x", title: "Take it out of review", "aria-label": `Take ${title} out of review`, onclick: (e) => { e.stopPropagation(); setInReview([r.track_id], false); } }, "×")));
  }
  if (q.length > 20) ul.append(h("li", { class: "empty" }, `${q.length - 20} more after these`));
  ul.scrollTop = top;
}
// What a label changed from Tastr's reading when it was saved: [kind, text], kind same | chg | note.
function labelDiffs(lab) {
  const out = [], tb = lab.tastr_bpm, tcam = nameToCam(lab.tastr_key), tk = tcam ? accid(CAM_NAME(tcam)) : lab.tastr_key || "no key";
  const note = lab.note ? [["note", `“${lab.note}”`]] : [];
  if (lab.kept) return [["same", `Kept as is: ${[isNum(tb) ? `${tb.toFixed(1)} BPM` : null, tk].filter(Boolean).join(" · ")}`], ...note];
  if (isNum(lab.bpm)) out.push(isNum(tb) && Math.abs(lab.bpm - tb) < 0.06 ? ["same", `BPM ✓ ${tb.toFixed(1)}`] : ["chg", `BPM ${isNum(tb) ? tb.toFixed(1) : "–"} → ${lab.bpm}`]);
  if (lab.key_name) { const cam = nameToCam(lab.key_name); out.push(cam && cam === tcam ? ["same", `Key ✓ ${tk}`] : ["chg", `Key ${tk} → ${cam ? accid(CAM_NAME(cam)) : lab.key_name}`]); }
  if (lab.boundaries) out.push(["chg", `${lab.boundaries.split(" ").length} section starts`]);
  return [...out, ...note];
}
// One pattern review as a log line: [kind, text], a Right mark "same", anything else "chg" (or "note" once stale).
function patternDiff(m) {
  const s = m.section, where = s ? ` in ${cap(s.label || "section")}${s.letter ? " " + s.letter : ""}` : m.end_bar ? ` in bars ${m.start_bar}-${m.end_bar}` : "";
  const word = { right: "right", close: "close", way_off: "way off" }[m.verdict], name = cap(m.part);
  const text = [word ? `${name} ${word}${where}` : `${name}${where}`, m.truth ? "right pattern saved" : null].filter(Boolean).join(": ");
  return [m.stale ? "note" : m.verdict === "right" && !m.truth ? "same" : "chg", m.stale ? `${text} (older analysis)` : text];
}
// The log: every track with a label or a pattern review, newest first.
function reviewedLog() {
  const byId = new Map();
  const at = (id, title, when) => { const r = byId.get(id) || { track_id: id, title, updated_at: "", diffs: [] }; if (String(when) > r.updated_at) r.updated_at = String(when); byId.set(id, r); return r; };
  for (const lab of labels.values()) at(lab.track_id, lab.title, lab.updated_at).diffs.push(...labelDiffs(lab));
  for (const m of patternMarks) at(m.track_id, m.title, m.updated_at).diffs.push(patternDiff(m));
  return [...byId.values()].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
}
function renderReviewLog() {
  const ul = $("review-list"), top = ul.scrollTop;
  ul.replaceChildren(); ul.setAttribute("aria-label", "Tracks you reviewed");
  const rows = reviewedLog();
  if (!rows.length) { ul.append(h("li", { class: "empty" }, "Nothing reviewed yet. Labels you save and patterns you mark show up here.")); return; }
  const fixed = rows.filter((r) => r.diffs.some(([k]) => k === "chg")).length;
  ul.append(h("li", { class: "empty rv-logsum" }, `${rows.length} reviewed · Tastr was off on ${fixed}. `,
    h("button", { type: "button", class: "linklike", onclick: () => { Object.assign(lib, LIB_CLEAR(), { lab: "yes" }); syncTools(); setReview(false); } }, "Show them in the library"), " · ",
    h("button", { type: "button", class: "linklike", onclick: exportLabels }, "Download CSV")));
  for (const r of rows) {
    const id = r.track_id, title = r.title || `Track ${id}`, at = Date.parse(r.updated_at);
    ul.append(h("li", { class: currentId === id ? "active" : "", "data-id": id, tabindex: "0", role: "button", onclick: () => openTrack(id),
        onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openTrack(id); } } },
      h("div", { class: "rv-top" }, h("span", { class: "rv-t", title }, title), at ? h("time", { class: "rv-when", title: fmtUpLong(at) }, fmtUp(at)) : null),
      h("ul", { class: "rv-diff" }, r.diffs.map(([k, t]) => h("li", { class: k }, t)))));
  }
  ul.scrollTop = top;
}
// Your labels as CSV, Tastr's reading when each was saved next to it.
function exportLabels() {
  const esc = (v) => (v == null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
  const cols = ["track_id", "title", "artist", "tastr_bpm", "bpm", "tastr_key", "key_name", "boundaries", "note", "updated_at", "kept"];
  const lines = [cols.join(","), ...[...labels.values()].map((lab) => cols.map((k) => esc(k === "kept" ? (lab.kept ? 1 : 0) : lab[k])).join(","))];
  downloadBytes(new TextEncoder().encode(lines.join("\n") + "\n"), "tastr-labels.csv", "text/csv");
}
export function openForReview(id) { labelOpen = id; openTrack(id); renderReview(); }
function setReview(on) { reviewOn = on; if (on && libCollapsed) setLibCollapsed(false); renderList(); }

// Section-start times: m:ss with a tenth when it isn't a whole second (bar lines rarely land on one). Also reads seconds.
export const fmtMark = (t) => { const r = Math.round(t * 10) / 10, s = r % 60; return `${Math.floor(r / 60)}:${(Number.isInteger(s) ? String(s) : s.toFixed(1)).padStart(Number.isInteger(s) ? 2 : 4, "0")}`; };
export const parseTimes = (str) => { const parts = String(str || "").trim().split(/[;\s,]+/).filter(Boolean); const ts = parts.map((x) => { const m = /^(?:(\d+):)?(\d+(?:\.\d+)?)$/.exec(x); return m ? (+(m[1] || 0)) * 60 + +m[2] : NaN; }); return ts.some((t) => !isNum(t)) ? null : ts; };
// Marking section starts on the chart: the open label panel sets this; the card's chart reads it, draws the marks and edits them.
export let markHook = null;
export const snapBar = (c, t) => { const bars = (c.core && c.core.downbeat_times) || []; let best = t, bd = Infinity; for (const b of bars) { const d = Math.abs(b - t); if (d < bd) { bd = d; best = b; } } return bd < 4 ? best : t; };
export const charts = new Map(); // card id -> {now, draw, mode, setMode, el, big} for the chart on screen
export function endMarking(id) {
  if (!markHook || markHook.cardId !== id) return;
  markHook = null;
  const ch = charts.get(id); if (ch) { ch.big(false); ch.draw(); }
}

export function setLabelOpen(v) { return (labelOpen = v); }
export function setMarkHook(v) { return (markHook = v); }
export function setRepaintLabel(v) { return (repaintLabel = v); }
export function countReviewed() { reviewDone++; } // a track labeled this session (the review list's "All caught up" count)

// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  $("rv-tab-todo").addEventListener("click", () => { reviewTab = "todo"; renderReview(); });
  $("rv-tab-done").addEventListener("click", () => { reviewTab = "done"; renderReview(); });
  $("lib-review").addEventListener("click", (e) => { e.stopPropagation(); if (libCollapsed) setLibCollapsed(false); if (!reviewOn) setReview(true); });
  $("lib-tracks").addEventListener("click", (e) => { if (libCollapsed) return; e.stopPropagation(); if (reviewOn) setReview(false); });
  document.addEventListener("keydown", (e) => {
    if (!markHook || e.key.toLowerCase() !== "m" || e.metaKey || e.ctrlKey || e.altKey || /INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName || "")) return;
    const ch = charts.get(markHook.cardId); if (!ch) return;
    e.preventDefault(); markHook.add(ch.now(), true);
  });
}
