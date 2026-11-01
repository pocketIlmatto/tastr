// Shared by every module: the DOM and SVG builders, the vibe vocabulary, the library's shared state, fetch helpers and job progress.

export const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";
export const SEC_COLORS = ["var(--sec0)", "var(--sec1)", "var(--sec2)", "var(--sec3)", "var(--sec4)"];

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
export const tagHelp = (tag) => TAG_HELP[tag] || (/ tempo$/.test(tag) ? "Tempo falls in the typical range for this style. A hint, not a genre call." : "");

export const VIBE_MIN = 0.2;
const VIBE_RULES = {
  "high energy": "Loudness and density well above average (energy above 0.6).",
  "chill": "Low energy; the bar is lower for tracks faster than 115 BPM.",
  "melancholic": "Minor key with energy below 0.5.",
  "dark": "Dull, low-treble sound, more so in a minor key.",
  "bright": "Lots of treble, more so in a major key.",
  "euphoric": "Major key, high energy and a bright sound together.",
  "driving": "Strong, steady pulse at 115-150 BPM.",
  "danceable": "Steady, regular rhythm that is easy to move to.",
  "hypnotic": "Strong pulse with very little change between sections.",
  "bass-heavy": "A large share of the energy sits in the low end.",
  "percussive": "Sharp, drum-like attacks stand out.",
  "atmospheric": "Few sharp attacks and a weak pulse, so washy and ambient.",
  "dynamic": "Big contrast in loudness between sections.",
  "swung": "16th notes land late by a clear, measurable amount (needs a confident swing reading).",
};
// The vibe-to-group map lives here, not in the engine. tests/test_web.py checks every vibe the engine can produce has a group.
export const VIBE_GROUPS = [
  { key: "mood", name: "Mood", color: "var(--sec0)", tags: ["dark", "bright", "melancholic", "euphoric"] },
  { key: "energy", name: "Energy", color: "var(--sec2)", tags: ["high energy", "chill", "dynamic"] },
  { key: "rhythm", name: "Rhythm", color: "var(--sec1)", tags: ["driving", "danceable", "hypnotic", "percussive", "swung"] },
  { key: "sound", name: "Sound", color: "var(--sec4)", tags: ["bass-heavy", "atmospheric"] },
  { key: "tempo", name: "Tempo", color: "var(--muted)", tags: [], match: (tag) => /tempo$/.test(tag) },
  { key: "other", name: "Other", color: "var(--other)", tags: [] },
];
export const groupOf = (tag) => VIBE_GROUPS.find((g) => g.tags.includes(tag) || (g.match && g.match(tag))) || VIBE_GROUPS[VIBE_GROUPS.length - 1];
export const vibeHelp = (tag) => VIBE_RULES[tag] || tagHelp(tag);


export let tracks = [];
export let tagsByTrack = {};
export let currentId = null;
export let labels = new Map(), reviewRows = [], patternMarks = []; // your labels by track id, the review list and your pattern reviews (GET /api/labels, /api/review, /api/pattern-reviews)

export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid);
  return el;
}
export function svg(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  for (const kid of kids.flat()) if (kid != null) el.append(kid);
  return el;
}
export const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
export const isNum = (x) => typeof x === "number" && isFinite(x);
export function mmss(sec) {
  if (!isNum(sec)) return "–";
  const s = Math.round(sec);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
export function fmtLeft(s) { return s < 8 ? "almost done" : s < 60 ? `about ${Math.max(5, Math.round(s / 5) * 5)} s left` : `about ${Math.round(s / 60)} min left`; }
export function progBar(frac, label) {
  const pct = Math.round(Math.min(1, Math.max(0, frac || 0)) * 100);
  return h("span", { class: "pbar", role: "progressbar", "aria-label": label, "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(pct) }, h("i", { style: `width:${pct}%` }));
}
// While a long job runs, the tab title shows how far along it is, so a background tab still says. One entry per kind of job.
const baseTitle = document.title, titleJobs = new Map();
export function jobTitle(key, frac) {
  if (frac == null) titleJobs.delete(key); else titleJobs.set(key, frac);
  const f = [...titleJobs.values()][0];
  document.title = f == null ? baseTitle : `(${Math.round(Math.min(1, f) * 100)}%) ${baseTitle}`;
}
// A pause between polls that ends early when the tab comes back to the front (background tabs barely run timers).
export function nap(ms) {
  return new Promise((resolve) => {
    const done = () => { clearTimeout(t); document.removeEventListener("visibilitychange", vis); resolve(); };
    const vis = () => { if (!document.hidden) done(); };
    const t = setTimeout(done, ms);
    document.addEventListener("visibilitychange", vis);
  });
}
// An error reply's message: the server's own `detail`, else something readable. A reply that isn't the app's
// (Cloudflare or Fly answering for it: a timeout, a restart) has no detail, and over HTTP/2 no status text either.
export function httpError(status, detail, statusText) {
  if (detail) return typeof detail === "string" ? detail : JSON.stringify(detail);
  if ([502, 503, 504, 520, 522, 524].includes(status)) return `The server didn't answer in time (error ${status}). It may have restarted; try again in a minute.`;
  if (status === 413) return "This file is too large for the server.";
  return statusText || `The server answered with error ${status}.`;
}
export async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = null;
    try { detail = (await r.json()).detail; } catch (_) {}
    throw Object.assign(new Error(httpError(r.status, detail, r.statusText)), { status: r.status });
  }
  return r.status === 204 ? null : r.json();
}

export const NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]; // note values as the bass and chord parts store them

export const trackHooks = new Set();  // told after each new track list (query-lib.js notices a stale library copy)
export function setTracks(v) { tracks = v; for (const f of trackHooks) f(); return tracks; }
export function setTagsByTrack(v) { return (tagsByTrack = v); }
export function setCurrentId(v) { return (currentId = v); }
export function setLabels(v) { return (labels = v); }
export function setReviewRows(v) { return (reviewRows = v); }
export function setPatternMarks(v) { return (patternMarks = v); }
