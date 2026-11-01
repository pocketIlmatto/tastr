// Your verdict on what the groove view detected (pattern reviews, tastr/labels.py): Right, Close or Way off per part and section,
// and your edit saved as the right pattern, which a pattern benchmark scores the engine against. The server keys them
// by the section's bars, so they outlive a re-analysis; one whose pattern a re-analysis changed comes back "stale".

import { api, cap, h } from "./core.js";
import { JSON_PUT, loadReview } from "./labels.js";

const VERDICTS = [["right", "✓ Right", "right", "What was detected is right"], ["close", "≈ Close", "close", "Close: usable with small changes"], ["way_off", "✗ Way off", "way off", "Way off: what was detected is wrong"]];

const barsAt = (g, k) => (g.secs ? [g.secs[k].start_bar, g.secs[k].end_bar] : [0, 0]);
const markOf = (g, part, k = g.sel) => { const [a, b] = barsAt(g, k); return g.marks.find((m) => m.part === part && m.start_bar === a && m.end_bar === b) || null; };
const truthShape = (part, m) => (part === "drums" ? Object.fromEntries(Object.entries(m).filter(([, v]) => typeof v === "string"))
  : part === "bass" ? { notes: m.notes.map((x) => x || null), rhythm: m.rhythm || null } : { chords: m.chords.map((x) => x || null) });

async function putMark(g, part, change, k = g.sel) {
  const m = markOf(g, part, k) || {};
  const body = { part, section: g.ek(k), verdict: m.verdict || null, truth: m.truth || null, note: m.note || null, ...change };
  g.marks = await api(`/api/tracks/${g.card.id}/pattern-reviews`, JSON_PUT(body));
}
async function mark(g, part, change, said) {
  try { await putMark(g, part, change); g.paint(true); g.say(said); loadReview(); } catch (e) { g.say(e.message); }
}
async function rightEverywhere(g, part) {
  try { for (let k = 0; k < g.secs.length; k++) if (!(markOf(g, part, k) || {}).verdict) await putMark(g, part, { verdict: "right" }, k); } catch (e) { g.say(e.message); return; }
  g.paint(true); g.say(`${cap(part)} marked right in every section you hadn't marked.`); loadReview();
}

// Fetch this track's marks, then repaint with them.
export function loadMarks(g) {
  if (g.card.id != null) api(`/api/tracks/${g.card.id}/pattern-reviews`).then((r) => { g.marks = r || []; g.paint(true); }).catch(() => {}); // an older server: no marks
}

// Save your edit as the right pattern, or show the one saved and a way to remove it.
function truthControl(g, part, m, nEdits, where) {
  const mine = truthShape(part, g.cur(part)), same = m && m.truth && JSON.stringify(m.truth) === JSON.stringify(mine);
  if (nEdits && !same) {
    return h("button", { type: "button", class: "linklike", title: "Keep your edit as the right answer for this section. The pattern benchmark scores the engine against it.",
      onclick: () => mark(g, part, { truth: mine }, `Your ${part} edit is saved as the right pattern for ${where}.`) }, m && m.truth ? "Save my edit as the right pattern again" : "Save my edit as the right pattern");
  }
  if (!m || !m.truth) return null;
  return h("span", { class: "mark-truth" }, same ? "Your edit is saved as the right pattern · " : "Right pattern saved · ",
    h("button", { type: "button", class: "linklike", "aria-label": `Remove the right ${part} pattern saved for ${where}`, onclick: () => mark(g, part, { truth: null }, `Right ${part} pattern removed for ${where}.`) }, "Remove"));
}

export function markRow(g, part, nEdits) {
  if (g.card.id == null) return null;
  const m = markOf(g, part), where = g.secName() || "this track";
  const btns = VERDICTS.map(([v, label, word, tip]) => {
    const on = !!m && m.verdict === v;
    return h("button", { type: "button", class: `mark-btn ${v}`, "aria-pressed": String(on), title: `${tip} (${where})`,
      onclick: () => mark(g, part, { verdict: on ? null : v }, on ? `Mark taken off the ${part} in ${where}.` : `Detected ${part} in ${where} marked ${word}.`) }, label);
  });
  const truthEl = truthControl(g, part, m, nEdits, where);
  return h("div", { class: "mark-row" }, h("span", { class: "muted" }, "Detected:"),
    h("span", { class: "mark-btns", role: "group", "aria-label": `Is the detected ${part} right in ${where}?` }, btns),
    m && m.stale ? h("span", { class: "mark-stale", title: "A re-analysis changed this pattern after you marked it. Mark it again." }, "marked on an older analysis") : null,
    g.secs && g.secs.length > 1 ? h("button", { type: "button", class: "linklike mark-all", title: `Mark the detected ${part} right in every section you haven't marked yet`, onclick: () => rightEverywhere(g, part) }, "Right everywhere else") : null,
    truthEl);
}
