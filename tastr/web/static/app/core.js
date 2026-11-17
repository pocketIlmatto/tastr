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
// Style holds the ML style tags (Discogs styles, docs/essentia.md); the ML moods join Mood and vocal/instrumental Sound.
export const VIBE_GROUPS = [
  { key: "style", name: "Style", color: "var(--sec3)", tags: [], match: (tag) => mlStyles.has(tag) },
  { key: "mood", name: "Mood", color: "var(--sec0)", tags: ["dark", "bright", "melancholic", "euphoric",
    "relaxed", "happy", "sad", "aggressive", "party", "calm", "cool", "deep", "dramatic", "dreamy", "emotional", "energetic", "epic",
    "fast", "fun", "groovy", "heavy", "hopeful", "inspiring", "meditative", "positive", "powerful", "retro", "romantic", "sexy", "slow",
    "soft", "space", "upbeat", "uplifting"] },
  { key: "energy", name: "Energy", color: "var(--sec2)", tags: ["high energy", "chill", "dynamic"] },
  { key: "rhythm", name: "Rhythm", color: "var(--sec1)", tags: ["driving", "danceable", "hypnotic", "percussive", "swung"] },
  { key: "sound", name: "Sound", color: "var(--sec4)", tags: ["bass-heavy", "atmospheric", "vocal", "instrumental"] },
  { key: "tempo", name: "Tempo", color: "var(--muted)", tags: [], match: (tag) => /tempo$/.test(tag) },
  { key: "other", name: "Other", color: "var(--other)", tags: [] },
];
export const groupOf = (tag) => VIBE_GROUPS.find((g) => g.tags.includes(tag) || (g.match && g.match(tag))) || VIBE_GROUPS[VIBE_GROUPS.length - 1];

/* ML tags: Essentia's trained models tag every upload on a server with TASTR_ML_TAGS=auto (docs/essentia.md). A card has
   ml_tags {tags: [{tag, group: style|mood|voice, score}]} once they're in, and ml_tags_state (none, queued, running, done
   or error) for the run. Once a track has them, they replace the engine's weak vibes below; the engine keeps the rest
   ("dark" among them). Until then, or if they fail, the track shows the engine's vibes as before. */
export const ML_REPLACES = { driving: "energetic", euphoric: "uplifting", hypnotic: "meditative", chill: "relaxed", bright: "happy" };
export const ML_TIPS = {
  relaxed: "Calm and laid back.", happy: "Sounds cheerful.", sad: "Sounds sad or melancholy.", aggressive: "Hard and forceful.", party: "Made for a party.",
  energetic: "Full of energy and drive.", uplifting: "Lifts you up; builds to a bright, positive feel.", meditative: "Steady and trance-like; draws you in.",
  dreamy: "Soft and hazy.", deep: "Deep, warm and rolling.", dark: "Dark and brooding.", vocal: "Has singing or rapping.", instrumental: "No vocals.",
};
export const mlTip = (tag) => ML_TIPS[tag] || (mlStyles.has(tag) ? "A Discogs style." : "A mood from Essentia's trained models.");
// The ML style names seen so far (cards and the track list), for groupOf: styles are open-ended, moods and voice aren't.
export const mlStyles = new Set();
const ML_NOT_STYLE = new Set([...VIBE_GROUPS[1].tags, "vocal", "instrumental"]);
// Where a card's ML tags are: "done" once it has them (they stay through a later rerun), else the run's state, with
// "queued" for a track that has none yet on a server that adds them (the upload or the one-time library pass queues it).
export function mlState(c) {
  if (c.ml_tags && Array.isArray(c.ml_tags.tags)) return "done";
  const s = c.ml_tags_state;
  if (s === "queued" || s === "running" || s === "error") return s;
  return c.ml_tags_available ? "queued" : null;
}
// The one list of vibes for a card, [{tag, score, src: "engine"|"ml", mlGroup?}]: the chips, the summary, the filters.
export function vibeTags(c) {
  const eng = (c.tags || []).map((t) => ({ ...t, src: "engine" }));
  if (mlState(c) !== "done") return eng;
  const kept = eng.filter((t) => !ML_REPLACES[t.tag]), have = new Set(kept.map((t) => t.tag));
  const ml = c.ml_tags.tags.filter((t) => t && t.tag && !have.has(t.tag));
  for (const t of ml) if (t.group === "style") mlStyles.add(t.tag);
  return [...kept, ...ml.map((t) => ({ tag: t.tag, score: t.score, src: "ml", mlGroup: t.group }))];
}
// The same for a /api/tracks row: `tags` (the engine's, strongest first) and `ml_tags` (names, or null before they're in).
export function rowVibes(row) {
  const eng = row.tags || [];
  if (!Array.isArray(row.ml_tags)) return eng;
  const kept = eng.filter((t) => !ML_REPLACES[t]), have = new Set(kept);
  for (const t of row.ml_tags) if (!ML_NOT_STYLE.has(t)) mlStyles.add(t);
  return [...kept, ...row.ml_tags.filter((t) => !have.has(t))];
}
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
// View as (T34): while an admin views the app as someone else, the server refuses every write with 403
// "Read-only while viewing as X."; that message shows as a toast, and the caller's own error handling rolls back.
export const READ_ONLY = /^Read-only while viewing as /;
export function readOnlyToast(msg) {
  document.querySelectorAll(".va-toast").forEach((t) => t.remove());
  const t = h("div", { class: "va-toast", role: "alert" }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), 3500);
}
export async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = null;
    try { detail = (await r.json()).detail; } catch (_) {}
    if (r.status === 403 && READ_ONLY.test(detail || "")) readOnlyToast(detail);
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
