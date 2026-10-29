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

const VIBE_MIN = 0.2;
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
const VIBE_GROUPS = [
  { key: "mood", name: "Mood", color: "var(--sec0)", tags: ["dark", "bright", "melancholic", "euphoric"] },
  { key: "energy", name: "Energy", color: "var(--sec2)", tags: ["high energy", "chill", "dynamic"] },
  { key: "rhythm", name: "Rhythm", color: "var(--sec1)", tags: ["driving", "danceable", "hypnotic", "percussive", "swung"] },
  { key: "sound", name: "Sound", color: "var(--sec4)", tags: ["bass-heavy", "atmospheric"] },
  { key: "tempo", name: "Tempo", color: "var(--muted)", tags: [], match: (tag) => /tempo$/.test(tag) },
  { key: "other", name: "Other", color: "var(--other)", tags: [] },
];
const groupOf = (tag) => VIBE_GROUPS.find((g) => g.tags.includes(tag) || (g.match && g.match(tag))) || VIBE_GROUPS[VIBE_GROUPS.length - 1];
const vibeHelp = (tag) => VIBE_RULES[tag] || tagHelp(tag);

function vibeBody(c) {
  const shown = [...c.tags].sort((a, b) => b.score - a.score);
  const shownSet = new Set(shown.map((t) => t.tag));
  const all = (c.extras && c.extras["tags.vibe_scores"]) || {};
  const below = Object.entries(all).filter(([tag]) => !shownSet.has(tag)).map(([tag, score]) => ({ tag, score })).sort((a, b) => b.score - a.score);
  const rowOf = (t, on) => h("li", { class: "vibe-row" + (on ? "" : " off"), style: `--g:${groupOf(t.tag).color}` },
    h("span", { class: "vibe-name", tabindex: "0", "data-tip": vibeHelp(t.tag) }, h("i", { class: "vibe-dot", title: groupOf(t.tag).name }), t.tag),
    h("span", { class: "vibe-bar", role: "img", "aria-label": `Score ${t.score.toFixed(2)} of 1` }, h("i", { style: `width:${t.score * 100}%` })),
    h("span", { class: "vibe-num" }, t.score.toFixed(2)),
    h("span", { class: "vibe-state" }, on ? "Shown" : "Hidden"));
  const list = h("ul", { class: "vibe-list", hidden: "" },
    h("li", { class: "vibe-head", "aria-hidden": "true" }, h("span", {}, "Vibe"), h("span", { class: "vibe-axis" }, h("span", { class: "vibe-min", style: `left:${VIBE_MIN * 100}%` }, "0.20 cut-off")), h("span", {}), h("span", {})),
    ...shown.map((t) => rowOf(t, true)), ...below.map((t) => rowOf(t, false)));
  const btn = h("button", { type: "button", class: "vibe-toggle", "aria-expanded": "false" }, "Show all scores");
  btn.onclick = () => { const open = list.hidden; list.hidden = !open; btn.setAttribute("aria-expanded", String(open)); btn.textContent = open ? "Hide scores" : "Show all scores"; };
  const note = h("p", { class: "vibe-note muted" },
    h("span", { class: "vibe-scale", "aria-hidden": "true" }),
    h("span", { tabindex: "0", "data-tip": `Each vibe gets a score from 0 to 1 from the track's measurements. Chips are grouped by kind of vibe (mood, energy, rhythm, sound, tempo), and each group has its own color. A stronger color means a higher score. Vibes scoring under ${VIBE_MIN.toFixed(2)} are left out, and a vibe the track doesn't qualify for at all gets no score.` }, `Stronger color = higher score. Shown from ${VIBE_MIN.toFixed(2)} up.`));
  return h("div", { class: "vibe" },
    VIBE_GROUPS.map((g) => ({ g, items: shown.filter((t) => groupOf(t.tag) === g) })).filter((x) => x.items.length).map(({ g, items }) =>
      h("div", { class: "vibe-group", style: `--g:${g.color}` }, h("span", { class: "vibe-glabel" }, g.name),
        h("div", { class: "chips" }, items.map((t) =>
          h("span", { class: "chip", tabindex: "0", style: `--t:${isNum(t.score) ? Math.max(0, Math.min(1, (t.score - VIBE_MIN) / (1 - VIBE_MIN))) : 0}`, "data-tip": `${tagHelp(t.tag)}${isNum(t.score) ? ` Score ${t.score.toFixed(2)}.` : ""}`.trim() }, t.tag))))),
    h("div", { class: "vibe-foot" }, note, btn), list);
}

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
const lib = { q: "", sort: "new", vibe: "", key: "", bmin: "", bmax: "" };
const keyRank = (k) => { const m = /^(\d+)([AB])$/.exec(k || ""); return m ? +m[1] * 2 + (m[2] === "B" ? 1 : 0) : 999; };
const SORTS = {
  new: (a, b) => b.id - a.id,
  title: (a, b) => (a.title || "").localeCompare(b.title || ""),
  bpm: (a, b) => (a.bpm ?? 999) - (b.bpm ?? 999),
  bpmd: (a, b) => (b.bpm ?? -1) - (a.bpm ?? -1),
  key: (a, b) => keyRank(a.camelot) - keyRank(b.camelot) || (a.bpm ?? 0) - (b.bpm ?? 0),
  energy: (a, b) => (b.energy ?? -1) - (a.energy ?? -1),
  len: (a, b) => (b.duration ?? 0) - (a.duration ?? 0),
};
const libActive = () => !!(lib.q || lib.vibe || lib.key || lib.bmin || lib.bmax);
// Search, sort and filters run in the browser, which is fine for a few hundred tracks.
function visibleTracks() {
  const q = lib.q.trim().toLowerCase(), lo = lib.bmin === "" ? null : +lib.bmin, hi = lib.bmax === "" ? null : +lib.bmax;
  return tracks.filter((t) =>
    (!q || `${t.title} ${t.artist || ""}`.toLowerCase().includes(q)) &&
    (!lib.vibe || (t.tags || []).includes(lib.vibe)) && (!lib.key || t.camelot === lib.key) &&
    (lo === null || (isNum(t.bpm) && t.bpm >= lo)) && (hi === null || (isNum(t.bpm) && t.bpm <= hi))).sort(SORTS[lib.sort]);
}
function fillFilterOptions() {
  const vibes = [...new Set(tracks.flatMap((t) => t.tags || []))].sort(), keys = [...new Set(tracks.map((t) => t.camelot).filter(Boolean))].sort((a, b) => keyRank(a) - keyRank(b));
  $("lib-vibe").replaceChildren(h("option", { value: "" }, "Any vibe"), ...vibes.map((v) => h("option", { value: v }, v)));
  $("lib-key").replaceChildren(h("option", { value: "" }, "Any key"), ...keys.map((k) => h("option", { value: k }, k)));
  $("lib-vibe").value = lib.vibe; $("lib-key").value = lib.key;
}
function syncTools() {
  $("lib-q").value = lib.q; $("lib-sort").value = lib.sort; $("lib-vibe").value = lib.vibe; $("lib-key").value = lib.key; $("lib-bmin").value = lib.bmin; $("lib-bmax").value = lib.bmax;
}
async function loadTracks() {
  tracks = await api("/api/tracks");
  fillFilterOptions();
}
async function refresh(selectId) {
  await loadTracks();
  // A freshly analyzed track must be visible, so show everything, newest first.
  if (selectId != null) { Object.assign(lib, { q: "", vibe: "", key: "", bmin: "", bmax: "", sort: "new" }); syncTools(); }
  renderList();
  if (selectId != null) openTrack(selectId);
}
function clearFilters() { Object.assign(lib, { q: "", vibe: "", key: "", bmin: "", bmax: "" }); syncTools(); renderList(); }

// Placeholder rows for files being analyzed (or that failed), shown above the saved tracks.
const pending = [];
const cleanName = (n) => String(n || "").replace(/\.[a-z0-9]+$/i, "").replace(/^https?:\/\/(www\.)?/, "").slice(0, 48) || "Untitled";
// The server's error can be a multi-line ffmpeg dump with its own file paths; show a short version and keep the full text in a tooltip.
function shortError(err) {
  if (!err) return "";
  if (/^DecodeError/.test(err)) return "It couldn't be decoded as audio.";
  const line = err.split("\n")[0].replace(/\S*[\\/]uploads[\\/]\w{8}_/g, "");
  return line.length > 120 ? line.slice(0, 117) + "…" : line;
}
function renderList() {
  const ul = $("track-list"), top = ul.scrollTop;
  ul.replaceChildren();
  const shown = visibleTracks();
  $("lib-n").textContent = libActive() ? `${shown.length} of ${tracks.length} tracks` : `${tracks.length} track${tracks.length === 1 ? "" : "s"}`;
  $("lib-clear").hidden = !libActive();
  $("lib-tools").hidden = !tracks.length;
  if (!tracks.length && !pending.length) ul.append(h("li", { class: "muted empty" }, "No tracks yet."));
  else if (!shown.length && !pending.length) ul.append(h("li", { class: "muted empty" }, "No tracks match. ", h("button", { type: "button", class: "linklike", onclick: clearFilters }, "Clear filters")));
  for (const p of pending) {
    ul.append(p.failed
      ? h("li", { class: "pending failed", "aria-label": `${cleanName(p.name)}: could not be analyzed` },
          h("div", { class: "t" }, cleanName(p.name)),
          h("div", { class: "s" }, "Couldn't analyze this file. ", h("button", { type: "button", class: "linklike", onclick: () => { pending.splice(pending.indexOf(p), 1); renderList(); } }, "Dismiss")),
          p.error ? h("div", { class: "s why", title: p.error }, shortError(p.error)) : null)
      : h("li", { class: "pending", "aria-busy": "true" },
          h("div", { class: "t" }, h("span", { class: "spin" + (p.active ? "" : " idle"), "aria-hidden": "true" }), cleanName(p.name)),
          h("div", { class: "s" }, p.active ? "Analyzing…" : "Waiting…")));
  }
  for (const t of shown) {
    ul.append(h("li", { class: "trk" + (t.id === currentId ? " active" : ""), "data-id": t.id, tabindex: "0", "aria-current": t.id === currentId ? "true" : null, onclick: () => openTrack(t.id) },
      h("div", { class: "t", title: t.title }, t.title || `Track ${t.id}`),
      h("div", { class: "s" }, h("span", { class: "s-txt" }, [t.artist, isNum(t.bpm) ? `${t.bpm.toFixed(0)} BPM` : null, t.camelot, mmss(t.duration)].filter(Boolean).join(" · ")),
        h("span", { class: "e-bar", "aria-hidden": "true", title: isNum(t.energy) ? `Energy ${t.energy.toFixed(2)}` : "" }, h("i", { style: `width:${isNum(t.energy) ? Math.round(t.energy * 100) : 0}%` })))));
  }
  ul.scrollTop = top;
}
$("lib-q").addEventListener("input", (e) => { lib.q = e.target.value; renderList(); });
$("lib-sort").addEventListener("change", (e) => { lib.sort = e.target.value; renderList(); });
$("lib-vibe").addEventListener("change", (e) => { lib.vibe = e.target.value; renderList(); });
$("lib-key").addEventListener("change", (e) => { lib.key = e.target.value; renderList(); });
$("lib-bmin").addEventListener("input", (e) => { lib.bmin = e.target.value; renderList(); });
$("lib-bmax").addEventListener("input", (e) => { lib.bmax = e.target.value; renderList(); });
$("lib-clear").addEventListener("click", clearFilters);
$("lib-fbtn").addEventListener("click", () => { const f = $("lib-filters"), open = f.hidden; f.hidden = !open; $("lib-fbtn").setAttribute("aria-expanded", String(open)); });
$("track-list").addEventListener("keydown", (e) => {
  const li = e.target.closest("li.trk"); if (!li) return;
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openTrack(+li.dataset.id); return; }
  const dir = { ArrowDown: "nextElementSibling", ArrowUp: "previousElementSibling" }[e.key];
  if (!dir) return;
  e.preventDefault();
  let n = li[dir]; while (n && !n.classList.contains("trk")) n = n[dir];
  if (n) n.focus();
});

async function openTrack(id) {
  currentId = id;
  // Update the highlight in place so the list keeps its scroll position and focus.
  document.querySelectorAll("#track-list li.trk").forEach((li) => {
    const on = +li.dataset.id === id;
    li.classList.toggle("active", on);
    if (on) { li.setAttribute("aria-current", "true"); li.scrollIntoView({ block: "nearest" }); } else li.removeAttribute("aria-current");
  });
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
    h("div", { class: "title-row" }, h("h2", {}, (c.source && (c.source.title || c.source.uri)) || `Track ${c.id}`), trackMenu(c)),
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

  if (c.metadata) root.append(metadataSection(c.metadata));

  const energy = c.series && c.series.energy;
  if (energy && energy.times && energy.times.length > 1 || (c.sections || []).length) {
    root.append(section("energy", "Energy & sections", [energyChart(energy, c.sections || [], c.duration, c)]));
  }

  if ((c.tags || []).length) {
    root.append(section("vibe", "Vibe", [vibeBody(c)]));
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
    if (DAWS[daw].code) body.push(strudelView(c));
    else for (const [cat, items] of Object.entries(groups)) {
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
// Ableton Live is the only glossary so far; Strudel shows generated code instead. Other DAWs show in the picker but can't be selected.
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
  // Not a DAW: shows the track as Strudel live-code (strudel.js) instead of DAW tips.
  strudel: { name: "Strudel", code: true },
};
let daw = "ableton";
try { const v = localStorage.getItem("tastr-daw"); if (DAWS[v] && !DAWS[v].soon) daw = v; } catch (_) {}

function dawText(s) {
  const tpl = s.data && s.data.template;
  if (!tpl) return s.body;
  const tokens = DAWS[daw].tokens || {};
  return tpl.replace(/\{(\w+)\}/g, (m, k) => (tokens[k] ? tokens[k](s.data) : m));
}

let strudelAudio = true; // "Include the original audio", remembered for the session
function strudelView(c) {
  const say = h("span", { class: "muted st-say", role: "status", "aria-live": "polite" });
  const pre = h("pre", { class: "st-code", tabindex: "0", "aria-label": "Generated Strudel code" });
  const open = h("a", { class: "btn-link", target: "_blank", rel: "noopener" }, "Open in Strudel ↗");
  const note = h("p", { class: "muted st-note" });
  let code = "";
  const paint = () => {
    const audio = c.has_audio && strudelAudio;
    code = strudelCode(c, audio ? `${location.origin}/api/tracks/${c.id}/audio` : null);
    pre.textContent = code;
    open.href = strudelLink(code);
    note.textContent = "Opens strudel.cc in a new tab with this code loaded. Drum sounds load from Strudel's sample library the first time you play." +
      (audio ? " The original audio streams from your Tastr server, so keep it running; the first time, your browser may ask to allow strudel.cc to reach your local network." : "");
  };
  const box = h("input", { type: "checkbox", id: "st-audio", ...(strudelAudio ? { checked: "" } : {}) });
  box.addEventListener("change", () => { strudelAudio = box.checked; paint(); });
  const copy = h("button", { type: "button", onclick: async () => {
    try { await navigator.clipboard.writeText(code); say.textContent = "Copied"; } catch (_) { say.textContent = "Couldn't copy here"; }
    setTimeout(() => (say.textContent = ""), 2500);
  } }, "Copy code");
  paint();
  return h("div", { class: "strudel" },
    h("p", { class: "st-intro" }, "A sketch of this track as ", h("a", { href: "https://strudel.cc", target: "_blank", rel: "noopener" }, "Strudel"), " live-code: each section's drum pattern for its length, plus the chord loop, at this track's tempo.",
      c.has_audio ? " Add the original audio, cut into bars, to remix the real thing." : " It won't sound like the original, but it follows its structure, so it's a starting point for remixes."),
    c.has_audio ? h("label", { class: "st-opt", for: "st-audio" }, box, " Include the original audio, cut into bars") : null,
    h("div", { class: "st-actions" }, open, copy, say), pre, note);
}

const NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
function keyName(k) {
  return isNum(k.tonic) ? `${NOTES[k.tonic % 12]} ${k.mode || ""}`.trim() : "–";
}

// ---------- track audio: the original file for playback/scrubbing, and waveform/spectrogram picture data ----------
const SPEC_ROWS = 120, SPEC_HOP = 0.1, SPEC_FMIN = 40, SPEC_FMAX = 10000;
const SPEC_RAMP = [[8, 8, 24], [60, 20, 120], [170, 40, 120], [240, 120, 40], [255, 235, 160]];
let chartMode = "energy"; // Energy | Waveform | Spectrogram, remembered across cards for the session
let audioCache = null, viewsCache = null;
const txt = (s) => document.createTextNode(s);
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
// The decoded original file (plus a mono reversed copy, made on first backwards scrub). Fetched on first need, not on card open.
function trackAudio(c) {
  if (!audioCache || audioCache.id !== c.id) {
    const promise = fetch(`/api/tracks/${c.id}/audio`)
      .then((r) => { if (!r.ok) throw new Error(r.status === 404 ? "The original file is not on disk." : r.statusText); return r.arrayBuffer(); })
      .then((bytes) => new Promise((ok, fail) => audioCtx().decodeAudioData(bytes, ok, () => fail(new Error("This browser can't decode the file's format.")))))
      .then((buf) => ({ buf, rev: null }));
    audioCache = { id: c.id, promise };
    promise.catch(() => { if (audioCache && audioCache.promise === promise) audioCache = null; });
  }
  return audioCache.promise;
}
// Gray levels (0..252, row 0 = top/highest band) -> a dark-to-yellow image as a data URL.
function rampImage(level, frames, rows) {
  const cv = document.createElement("canvas"); cv.width = frames; cv.height = rows;
  const g = cv.getContext("2d"), img = g.createImageData(frames, rows), n = SPEC_RAMP.length - 1;
  for (let i = 0; i < frames * rows; i++) {
    const u = Math.max(0, Math.min(1, level[i] / 252)) * n, i0 = Math.min(n - 1, Math.floor(u)), q = u - i0;
    for (let ch = 0; ch < 3; ch++) img.data[i * 4 + ch] = SPEC_RAMP[i0][ch] + (SPEC_RAMP[i0 + 1][ch] - SPEC_RAMP[i0][ch]) * q;
    img.data[i * 4 + 3] = 255;
  }
  g.putImageData(img, 0, 0);
  return cv.toDataURL("image/png");
}
function loadImage(src) {
  return new Promise((ok, fail) => { const im = new Image(); im.onload = () => ok(im); im.onerror = () => fail(new Error("bad spectrogram image")); im.src = src; });
}
// Stored by the engine's `views` stage: int8 min/max per block, and a grayscale log-frequency PNG to color here.
async function viewsFromCard(c) {
  const v = await api(`/api/tracks/${c.id}/views`);
  const bin = atob(v.waveform.data), raw = new Int8Array(bin.length);
  for (let i = 0; i < bin.length; i++) raw[i] = (bin.charCodeAt(i) << 24) >> 24;
  const n = raw.length >> 1, mn = new Float32Array(n), mx = new Float32Array(n);
  for (let b = 0; b < n; b++) { mn[b] = raw[2 * b] / 127; mx[b] = raw[2 * b + 1] / 127; }
  const s = v.spectrogram, im = await loadImage(s.png);
  const cv = document.createElement("canvas"); cv.width = im.width; cv.height = im.height;
  const g = cv.getContext("2d"); g.drawImage(im, 0, 0);
  const px = g.getImageData(0, 0, im.width, im.height).data, level = new Uint8Array(im.width * im.height);
  for (let i = 0; i < level.length; i++) level[i] = px[i * 4];
  return { mn, mx, peak: 1, perSec: v.waveform.sr / v.waveform.block, spec: rampImage(level, im.width, im.height), specEnd: s.frames * s.hop };
}
// Cards analyzed before the `views` stage existed: compute both from the decoded file in the browser.
async function viewsFromAudio(c) {
  const { buf } = await trackAudio(c);
  const sr = buf.sampleRate, chans = [...Array(buf.numberOfChannels)].map((_, k) => buf.getChannelData(k));
  const d = new Float32Array(buf.length);
  for (const ch of chans) for (let i = 0; i < d.length; i++) d[i] += ch[i] / chans.length;
  await delay(0);
  const BLK = Math.round(sr / 86), nBlk = Math.ceil(d.length / BLK), mn = new Float32Array(nBlk), mx = new Float32Array(nBlk);
  let peak = 1e-6;
  for (let b = 0; b < nBlk; b++) {
    let lo = 1, hi = -1;
    for (let i = b * BLK, e = Math.min(d.length, i + BLK); i < e; i++) { const v = d[i]; if (v < lo) lo = v; if (v > hi) hi = v; }
    mn[b] = lo; mx[b] = hi; peak = Math.max(peak, -lo, hi);
  }
  await delay(0);
  const s = spectrogramLevels(d, sr);
  return { mn, mx, peak, perSec: sr / BLK, spec: rampImage(s.level, s.frames, SPEC_ROWS), specEnd: s.frames * SPEC_HOP };
}
function trackViews(c) {
  if (!viewsCache || viewsCache.id !== c.id) {
    const promise = c.has_views ? viewsFromCard(c) : viewsFromAudio(c);
    viewsCache = { id: c.id, promise };
    promise.catch(() => { if (viewsCache && viewsCache.promise === promise) viewsCache = null; });
  }
  return viewsCache.promise;
}
// Log-frequency magnitude (dB) per 0.1 s, same layout as the engine's: 0..252 over the top 70 dB, row 0 = top.
function spectrogramLevels(d, sr) {
  const N = sr > 32000 ? 4096 : 2048, bits = Math.log2(N), hop = Math.round(SPEC_HOP * sr), frames = Math.max(1, Math.floor((d.length - N) / hop));
  const win = new Float32Array(N), cosT = new Float32Array(N / 2), sinT = new Float32Array(N / 2), rev = new Uint16Array(N);
  for (let i = 0; i < N; i++) { win[i] = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (N - 1)); let r = 0; for (let b = 0; b < bits; b++) if (i & (1 << b)) r |= 1 << (bits - 1 - b); rev[i] = r; }
  for (let i = 0; i < N / 2; i++) { cosT[i] = Math.cos((2 * Math.PI * i) / N); sinT[i] = -Math.sin((2 * Math.PI * i) / N); }
  const re = new Float32Array(N), im = new Float32Array(N);
  const rowBin = [...Array(SPEC_ROWS + 1)].map((_, r) => (SPEC_FMIN * Math.pow(SPEC_FMAX / SPEC_FMIN, r / SPEC_ROWS)) / (sr / N));
  const mag = new Float32Array(N / 2), db = new Float32Array(frames * SPEC_ROWS);
  let top = -1e9;
  for (let f = 0; f < frames; f++) {
    for (let i = 0; i < N; i++) { const v = f * hop + i < d.length ? d[f * hop + i] : 0; re[rev[i]] = v * win[i]; im[rev[i]] = 0; }
    for (let size = 2; size <= N; size <<= 1) {
      const half = size >> 1, stepT = N / size;
      for (let i = 0; i < N; i += size) for (let j = 0, k = 0; j < half; j++, k += stepT) {
        const a = i + j, b = a + half, tr = re[b] * cosT[k] - im[b] * sinT[k], ti = re[b] * sinT[k] + im[b] * cosT[k];
        re[b] = re[a] - tr; im[b] = im[a] - ti; re[a] += tr; im[a] += ti;
      }
    }
    for (let i = 0; i < N / 2; i++) mag[i] = Math.hypot(re[i], im[i]);
    for (let r = 0; r < SPEC_ROWS; r++) {
      const a = Math.ceil(rowBin[r]), b = Math.max(a + 1, Math.ceil(rowBin[r + 1]));
      let m = 0; for (let k = a; k < b && k < N / 2; k++) m = Math.max(m, mag[k]);
      const v = 20 * Math.log10(m + 1e-6); db[f * SPEC_ROWS + r] = v; if (v > top) top = v;
    }
  }
  const level = new Uint8Array(frames * SPEC_ROWS);
  for (let f = 0; f < frames; f++) for (let r = 0; r < SPEC_ROWS; r++)
    level[(SPEC_ROWS - 1 - r) * frames + f] = Math.round(Math.max(0, Math.min(1, (db[f * SPEC_ROWS + r] - (top - 70)) / 70)) * 252);
  return { level, frames };
}

function energyChart(series, sections, duration, card) {
  const W = 700, H = 220, L = 40, R = 8, T = 28, B = 22;
  const times = (series && series.times) || [], vals = (series && series.values) || [];
  const end = Math.max(duration || 0, times.length ? times[times.length - 1] : 0, ...sections.map((s) => s.end || 0), 1);
  const letters = [...new Set(sections.map((x) => x.letter || x.label))];
  const pts = times.map((t, i) => [t, vals[i]]).filter(([t, v]) => isNum(t) && isNum(v));
  const view = { a: 0, b: end };
  const minSpan = Math.min(end, 12);
  const canPlay = !!card.has_audio, hasViews = canPlay || !!card.has_views;
  const wrap = h("div", { class: "chart-wrap" });
  const bar = h("div", { class: "chart-bar" });
  const zoomLabel = h("span", { class: "muted" });
  const btn = (label, title, fn) => h("button", { type: "button", title, "aria-label": title, onclick: fn }, label);
  bar.append(btn("−", "Zoom out", () => zoom(1.6)), btn("+", "Zoom in", () => zoom(1 / 1.6)), btn("Reset", "Show whole track", () => { view.a = 0; view.b = end; draw(); }), zoomLabel,
    h("span", { class: "muted hint" }, canPlay
      ? "Click the chart to pick a section and play from there. Drag the playhead to scrub; you hear the track as you drag. Scroll to zoom, drag to pan"
      : "Click a section to pick it. Scroll to zoom, drag to pan"));
  const tip = h("div", { class: "chart-tip", hidden: "" });
  const host = h("div", { class: "chart-host" });
  const MODES = [["energy", "Energy"], ["wave", "Waveform"], ["spec", "Spectrogram"]];
  let mode = hasViews ? chartMode : "energy", pics = null;
  const KEYS = {
    energy: [h("span", { class: "key-line" }), " Energy: how loud and dense the track is at each moment, sampled every ~3 s. ", h("span", { class: "key-box" }), " Shaded blocks are detected sections; same color means the same part repeats."],
    wave: ["Waveform: the shape of the sound over time. Taller means louder. Faint lines are beats, darker lines are bars, numbered from the start."],
    spec: ["Spectrogram: which pitches are present over time. Low sounds at the bottom, high at the top, brighter means stronger."],
  };
  const keyP = h("p", { class: "muted chart-key" });
  const modeBtns = MODES.map(([k, label]) => h("button", { type: "button", role: "tab", class: "mode-tab", "aria-selected": String(k === mode), onclick: () => setMode(k) }, label));
  const modes = h("div", { class: "chart-modes", role: "tablist", "aria-label": "Visualization" }, modeBtns);
  const busy = h("div", { class: "chart-busy", hidden: "", role: "status" }, h("span", { class: "spin", "aria-hidden": "true" }), "Preparing audio preview…");
  const playBtn = h("button", { type: "button", class: "tp-play", "aria-label": "Play" }, "▶");
  const timeL = h("span", { class: "tp-time" }, `0:00 / ${mmss(duration)}`);
  const seekR = h("input", { type: "range", class: "tp-seek", min: "0", max: String(end), step: "0.1", value: "0", "aria-label": "Seek" });
  const transport = h("div", { class: "transport" }, playBtn, timeL, seekR);
  const note = h("p", { class: "muted chart-key" });
  if (!canPlay) note.textContent = hasViews
    ? "The original file is no longer on disk, so playback is off."
    : "The original file isn't on disk (or this track came from a link that wasn't kept), so playback and the waveform and spectrogram views are off.";
  const say = (m) => { note.textContent = m; note.classList.toggle("error", !!m); };
  wrap.append(...(hasViews ? [modes] : []), bar, h("div", { class: "chart-hostwrap" }, host, busy), tip, ...(canPlay ? [transport] : []), keyP, note);

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
      // The spectrogram has its own colors, so sections there are dashed boundaries plus a light tint on the selected one.
      plot.append(svg("rect", { x: x(sec.start), y: T, width: Math.max(1, x(sec.end) - x(sec.start)), height: H - T - B, fill: color, opacity: mode === "spec" ? (si === selIdx ? 0.28 : 0) : si === selIdx ? 0.4 : 0.18, stroke: si === selIdx ? color : "none", "stroke-width": 2 }));
      if (mode === "spec") plot.append(svg("line", { x1: x(sec.start), x2: x(sec.start), y1: T, y2: H - B, stroke: "#fff", opacity: 0.3, "stroke-dasharray": "3 3" }));
      // Label band above the grid: solid block per section, text shortens to the letter, then disappears.
      const x0 = Math.max(L, x(sec.start)), x1 = Math.min(W - R, x(sec.end)), room = x1 - x0;
      plot.append(svg("rect", { x: x0, y: 4, width: Math.max(1, room), height: 18, rx: 2, fill: color, stroke: si === selIdx ? "var(--fg)" : "var(--panel)", "stroke-width": si === selIdx ? 2 : 1 }));
      const full = [cap(sec.label), sec.letter].filter(Boolean).join(" ");
      const label = room > full.length * 6.2 + 8 ? full : room > 22 ? (sec.letter || cap(sec.label).slice(0, 3)) : "";
      if (label) plot.append(svg("text", { class: "band", x: x0 + 5, y: 16.5 }, txt(label)));
    });
    if (mode === "energy") {
      [0.25, 0.5, 0.75].forEach((v) => plot.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: "var(--line)", "stroke-dasharray": "2 3" })));
      if (pts.length > 1) {
        const line = pts.map(([t, v]) => `${x(t).toFixed(1)},${y(v).toFixed(1)}`);
        plot.append(svg("polygon", { points: `${x(pts[0][0])},${y(0)} ${line.join(" ")} ${x(pts[pts.length - 1][0])},${y(0)}`, fill: "var(--accent)", opacity: 0.25 }));
        plot.append(svg("polyline", { points: line.join(" "), fill: "none", stroke: "var(--accent)", "stroke-width": 2 }));
      }
    } else if (!pics) {
      plot.append(svg("text", { x: (L + W - R) / 2, y: (T + H - B) / 2, "text-anchor": "middle", fill: "var(--muted)" }, txt("Preparing audio preview…")));
    } else if (mode === "wave") {
      const mid = (T + H - B) / 2, amp = (H - T - B) / 2 - 2, cols = W - L - R;
      plot.append(svg("line", { x1: L, x2: W - R, y1: mid, y2: mid, stroke: "var(--line)" }));
      // Beat grid: bar lines thinned by stride to stay >= 10 px apart, labels >= 30 px apart, beats only when >= 6 px apart.
      const beats = (card.core.beat_times || []).filter((t) => t >= view.a && t <= view.b), bars = card.core.downbeat_times || [];
      const pxPer = (W - L - R) / (view.b - view.a), barGap = bars.length > 1 ? (bars[1] - bars[0]) * pxPer : 0;
      if (beats.length && barGap / 4 >= 6) {
        let bd = ""; for (const t of beats) bd += `M${x(t).toFixed(1)},${T}V${H - B}`;
        plot.append(svg("path", { d: bd, stroke: "var(--muted)", "stroke-width": 0.5, opacity: 0.35 }));
      }
      const stride = [1, 2, 4, 8, 16, 32].find((k) => barGap * k >= 10) || 64;
      let bd2 = "";
      bars.forEach((t, i) => { if (i % stride || t < view.a || t > view.b) return; bd2 += `M${x(t).toFixed(1)},${T}V${H - B}`; });
      if (bd2) plot.append(svg("path", { d: bd2, stroke: "var(--fg)", "stroke-width": 1, opacity: 0.3 }));
      const labelEvery = [1, 2, 4, 8, 16, 32, 64].find((k) => barGap * k >= 30) || 128;
      bars.forEach((t, i) => { if (i % labelEvery || t < view.a || t > view.b) return; plot.append(svg("text", { class: "barnum", x: x(t) + 3, y: T + 11 }, txt(String(i + 1)))); });
      let d = "";
      for (let i = 0; i < cols; i++) {
        const t0 = tAt(L + i), t1 = tAt(L + i + 1);
        const b0 = Math.max(0, Math.floor(t0 * pics.perSec)), b1 = Math.min(pics.mn.length - 1, Math.max(b0, Math.floor(t1 * pics.perSec)));
        let lo = 1, hi = -1; for (let k = b0; k <= b1; k++) { if (pics.mn[k] < lo) lo = pics.mn[k]; if (pics.mx[k] > hi) hi = pics.mx[k]; }
        if (hi < lo) continue;
        d += `M${(L + i + 0.5).toFixed(1)},${(mid - (hi / pics.peak) * amp).toFixed(1)}V${(mid - (lo / pics.peak) * amp).toFixed(1)}`;
      }
      plot.append(svg("path", { d, stroke: "var(--accent)", "stroke-width": 1, fill: "none" }));
    } else {
      plot.append(svg("image", { href: pics.spec, x: x(0), y: T, width: x(pics.specEnd) - x(0), height: H - T - B, preserveAspectRatio: "none" }));
    }
    s.append(plot);
    const yLabel = (text) => s.append(svg("text", { x: 10, y: (H - B + T) / 2, "text-anchor": "middle", transform: `rotate(-90 10 ${(H - B + T) / 2})` }, txt(text)));
    if (mode === "energy") {
      [0, 0.5, 1].forEach((v) => s.append(svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, txt(v))));
      yLabel("energy");
    } else if (mode === "wave") {
      yLabel("level");
    } else {
      [[100, "100"], [1000, "1k"], [10000, "10k"]].forEach(([f, label]) => {
        const yy = T + (1 - Math.log(f / SPEC_FMIN) / Math.log(SPEC_FMAX / SPEC_FMIN)) * (H - T - B);
        s.append(svg("text", { x: L - 6, y: yy + 4, "text-anchor": "end" }, txt(label)));
      });
      yLabel("Hz");
    }
    const span = view.b - view.a;
    const step = [5, 10, 15, 30, 60, 120].find((st) => span / st <= 8) || 120;
    for (let t = Math.ceil(view.a / step) * step; t <= view.b; t += step) s.append(svg("text", { x: x(t), y: H - 6, "text-anchor": "middle" }, txt(mmss(t))));
    s.append(svg("line", { class: "cursor", x1: 0, x2: 0, y1: T, y2: H - B, stroke: "var(--fg)", "stroke-width": 1, opacity: 0, "pointer-events": "none" }));
    s.append(svg("circle", { class: "cursor-dot", r: 4, fill: "var(--accent)", stroke: "var(--bg)", "stroke-width": 2, opacity: 0, "pointer-events": "none" }));
    if (canPlay) {
      s.append(svg("line", { class: "playhead", y1: T - 4, y2: H - B, stroke: "var(--fg)", "stroke-width": 2, "pointer-events": "none" }));
      s.append(svg("path", { class: "playhead-knob", d: "M-6,0 L6,0 L0,8 Z", fill: "var(--fg)", "pointer-events": "none" }));
      paintHead();
    }
    zoomLabel.textContent = view.b - view.a >= end - 0.01 ? "Whole track" : `${mmss(view.a)}–${mmss(view.b)}`;
    if (!drag) s.style.cursor = view.b - view.a < end - 0.01 ? "grab" : "crosshair";
    if (cursorT != null) showTip(cursorT);
  }
  const toVB = (e) => { const r = svgEl.getBoundingClientRect(); return { px: ((e.clientX - r.left) / r.width) * W }; };
  function showTip(t) {
    const cur = svgEl.querySelector(".cursor"), dot = svgEl.querySelector(".cursor-dot");
    if (!cur) return;
    if (t < view.a || t > view.b) { hideTip(); return; }
    const v = mode === "energy" ? energyAt(t) : null, sec = sections.find((q) => t >= q.start && t < q.end);
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
    if (svgEl) for (const q of [".cursor", ".cursor-dot"]) { const el = svgEl.querySelector(q); if (el) el.setAttribute("opacity", 0); }
  }
  let drag = null;
  const atEdge = () => view.b - view.a < end - 0.01;
  const nearHead = (px) => { if (!canPlay) return false; const t = tNow(); return t >= view.a && t <= view.b && Math.abs(px - x(t)) <= 8; };
  const setHot = (on) => svgEl.classList.toggle("head-hot", on);
  svgEl.addEventListener("pointerdown", (e) => {
    svgEl.setPointerCapture(e.pointerId);
    const px = toVB(e).px;
    if (nearHead(px)) { const was = playing; if (was) me.stop(); drag = { scrub: true, was, px }; setHot(true); svgEl.style.cursor = "grabbing"; hideTip(); scrubStart(); return; }
    drag = { px, a: view.a, b: view.b };
  });
  svgEl.addEventListener("pointermove", (e) => {
    const { px } = toVB(e);
    if (drag && drag.scrub) { if (scrub) scrub.target = Math.max(0, Math.min(end, tAt(Math.max(L, Math.min(W - R, px))))); return; }
    if (!drag) { const hot = nearHead(px); setHot(hot); svgEl.style.cursor = hot || atEdge() ? "grab" : "crosshair"; if (hot) { hideTip(); return; } }
    if (drag && atEdge()) {
      const dt = ((px - drag.px) / (W - L - R)) * (drag.b - drag.a);
      view.a = drag.a - dt; view.b = drag.b - dt; clampView();
      svgEl.style.cursor = "grabbing"; hideTip(); draw(); return;
    }
    cursorT = tAt(Math.max(L, Math.min(W - R, px))); showTip(cursorT);
  });
  const up = (e) => {
    if (drag && drag.scrub) { const was = drag.was; drag = null; setHot(false); svgEl.style.cursor = "grab"; scrubEnd(); if (was) play(); else paintHead(); return; }
    if (e && e.type === "pointerup" && drag && Math.abs(toVB(e).px - drag.px) < 3) {
      const t0 = tAt(toVB(e).px), si = sections.findIndex((q) => t0 >= q.start && t0 < q.end);
      if (canPlay) seek(t0);
      if (si >= 0) { selIdx = si; draw(); bus.dispatchEvent(new CustomEvent("section", { detail: { idx: si, from: "chart" } })); }
    }
    drag = null; svgEl.style.cursor = atEdge() ? "grab" : "crosshair";
  };
  svgEl.addEventListener("pointerup", up);
  svgEl.addEventListener("pointercancel", up);
  svgEl.addEventListener("pointerleave", () => { if (!drag) { hideTip(); setHot(false); } });
  svgEl.addEventListener("wheel", (e) => {
    e.preventDefault();
    zoom(Math.exp(Math.sign(e.deltaY) * 0.25), tAt(Math.max(L, Math.min(W - R, toVB(e).px))));
  }, { passive: false });

  // ---- mode switching ----
  function setMode(k) {
    mode = chartMode = k;
    modeBtns.forEach((b, i) => b.setAttribute("aria-selected", String(MODES[i][0] === k)));
    keyP.replaceChildren(...KEYS[k]);
    if (k !== "energy" && !pics) ensurePics(); else draw();
  }
  async function withBusy(work) {
    busy.hidden = false;
    try { return await work(); }
    catch (err) { say(`Couldn't load the audio: ${err.message}`); return null; }
    finally { busy.hidden = true; }
  }
  async function ensurePics() {
    if (!pics) pics = await withBusy(() => trackViews(card));
    draw();
  }
  let audio = null;
  async function ensureAudio() {
    if (!audio) audio = await withBusy(() => trackAudio(card));
    return audio;
  }

  // ---- transport ----
  let pos = 0, src = null, startedAt = 0, playing = false, raf = 0;
  const tNow = () => (playing ? Math.min(end, actx.currentTime - startedAt) : pos);
  function paintHead() {
    const ph = svgEl.querySelector(".playhead"); if (!ph) return;
    const t = tNow(), vis = t >= view.a && t <= view.b;
    ph.setAttribute("x1", x(t)); ph.setAttribute("x2", x(t)); ph.setAttribute("opacity", vis ? 0.9 : 0);
    const kn = svgEl.querySelector(".playhead-knob"); kn.setAttribute("transform", `translate(${x(t)},${T - 9})`); kn.setAttribute("opacity", vis ? 1 : 0);
    seekR.value = String(t); timeL.textContent = `${mmss(t)} / ${mmss(duration)}`;
  }
  function startSrc() {
    const ctx = audioCtx(); if (ctx.state === "suspended") ctx.resume();
    if (src) { src.onended = null; try { src.stop(); } catch (_) {} }
    src = ctx.createBufferSource(); src.buffer = audio.buf; src.connect(ctx.destination);
    src.onended = () => { if (playing) { me.stop(); pos = 0; paintHead(); } };
    src.start(0, pos); startedAt = ctx.currentTime - pos;
  }
  // Registered as the shared `player`, so the drum sequencer and track playback stop each other.
  const me = { stop() {
    if (!playing) return;
    pos = tNow(); playing = false; cancelAnimationFrame(raf);
    if (src) { src.onended = null; try { src.stop(); } catch (_) {} src = null; }
    if (player === me) player = null;
    playBtn.textContent = "▶"; playBtn.setAttribute("aria-label", "Play"); paintHead();
  } };
  function loop() {
    if (!playing) return;
    if (!svgEl.isConnected) { me.stop(); return; }
    const t = tNow();
    // When zoomed in and the playhead leaves the view, page so it sits 10% from the left.
    if (t > view.b || t < view.a) { const span = view.b - view.a; view.a = Math.max(0, Math.min(end - span, t - span * 0.1)); view.b = view.a + span; draw(); }
    else paintHead();
    raf = requestAnimationFrame(loop);
  }
  async function play() {
    if (!(await ensureAudio()) || !svgEl.isConnected) return;
    stopAllPlayback();
    if (pos >= end - 0.2) pos = 0;
    startSrc(); playing = true; player = me;
    playBtn.textContent = "❚❚"; playBtn.setAttribute("aria-label", "Pause"); loop();
  }
  playBtn.onclick = () => { if (playing) me.stop(); else play(); };
  function seek(t) {
    pos = Math.max(0, Math.min(end, t));
    if (playing) startSrc();
    paintHead();
  }
  seekR.addEventListener("input", () => seek(+seekR.value));

  // ---- audible scrubbing: the play position chases the pointer, speed follows the drag (backwards uses a reversed copy) ----
  let scrub = null;
  function scrubStart() {
    scrub = { pos, target: pos, dir: 0, src: null, g: null, last: performance.now(), raf: 0 };
    const s = scrub;
    if (audio) scrubAudio(s);
    else ensureAudio().then(() => { if (scrub === s && audio) scrubAudio(s); });
    scrubLoop();
  }
  function scrubAudio(s) {
    const ctx = audioCtx(); if (ctx.state === "suspended") ctx.resume();
    if (!audio.rev) {
      // Mono reversed copy: half the memory of a stereo one, and fine for hearing where you are.
      const b = audio.buf, n = b.length, chans = [...Array(b.numberOfChannels)].map((_, k) => b.getChannelData(k)), r = new Float32Array(n);
      for (const ch of chans) for (let i = 0; i < n; i++) r[i] += ch[n - 1 - i] / chans.length;
      audio.rev = ctx.createBuffer(1, n, b.sampleRate); audio.rev.copyToChannel(r, 0);
    }
    s.g = ctx.createGain(); s.g.gain.value = 0; s.g.connect(ctx.destination);
  }
  function scrubSrc(dir) {
    const ctx = audioCtx();
    if (scrub.src) { try { scrub.src.stop(); } catch (_) {} }
    const sn = ctx.createBufferSource(); sn.buffer = dir > 0 ? audio.buf : audio.rev; sn.playbackRate.value = 0.0001; sn.connect(scrub.g);
    sn.start(0, Math.max(0, dir > 0 ? scrub.pos : audio.buf.duration - scrub.pos)); scrub.src = sn; scrub.dir = dir;
  }
  function scrubLoop() {
    if (!scrub) return;
    const now = performance.now(), dt = Math.min(0.05, (now - scrub.last) / 1000); scrub.last = now;
    const gap = scrub.target - scrub.pos;
    if (Math.abs(gap) > 1.5) { scrub.pos = scrub.target - Math.sign(gap) * 0.1; if (scrub.g) scrubSrc(Math.sign(gap)); }
    let rate = Math.max(-8, Math.min(8, (scrub.target - scrub.pos) / 0.08));
    if (Math.abs(rate) < 0.04) rate = 0;
    if (scrub.g) {
      const ctx = audioCtx(), dir = rate > 0 ? 1 : rate < 0 ? -1 : scrub.dir;
      if (dir && dir !== scrub.dir) scrubSrc(dir);
      if (scrub.src) scrub.src.playbackRate.setTargetAtTime(Math.max(0.0001, Math.abs(rate)), ctx.currentTime, 0.015);
      scrub.g.gain.setTargetAtTime(rate ? 0.9 : 0, ctx.currentTime, 0.02);
    }
    scrub.pos = Math.max(0, Math.min(end, scrub.pos + rate * dt)); pos = scrub.pos; paintHead();
    scrub.raf = requestAnimationFrame(scrubLoop);
  }
  function scrubEnd() {
    if (!scrub) return;
    cancelAnimationFrame(scrub.raf);
    if (scrub.src) { try { scrub.src.stop(audioCtx().currentTime + 0.03); } catch (_) {} }
    if (scrub.g) scrub.g.gain.setTargetAtTime(0, audioCtx().currentTime, 0.01);
    pos = scrub.pos; scrub = null;
  }

  keyP.replaceChildren(...KEYS[mode]);
  if (mode !== "energy") ensurePics();
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

// ---------- track menu ----------
function trackSummary(c) {
  const core = c.core || {}, k = core.key, src = c.source || {};
  const facts = [isNum(core.bpm) ? `${core.bpm.toFixed(1)} BPM` : null, k ? `${keyName(k)}${k.camelot ? ` (${k.camelot})` : ""}` : null, mmss(c.duration)];
  const vibes = [...(c.tags || [])].sort((a, b) => b.score - a.score).map((t) => t.tag);
  return [`${src.title || src.uri || `Track ${c.id}`}${src.artist ? " by " + src.artist : ""}`, facts.filter(Boolean).join(" · "), vibes.length ? `Vibe: ${vibes.join(", ")}` : null].filter(Boolean).join("\n");
}
// A status the menu shows once its card is drawn again (reloading metadata re-renders the card).
let menuNote = null;
function trackMenu(c) {
  const btn = h("button", { type: "button", class: "menu-btn", "aria-haspopup": "menu", "aria-expanded": "false", "aria-label": "Track actions" }, "⋯");
  const pop = h("div", { class: "menu-pop", role: "menu", hidden: "" });
  const say = h("span", { class: "menu-say muted", role: "status", "aria-live": "polite" });
  const wrap = h("div", { class: "menu-wrap" }, say, btn, pop);
  let sayTimer;
  const tell = (msg, isErr, busy) => {
    clearTimeout(sayTimer);
    say.className = "menu-say " + (isErr ? "error" : "muted");
    say.replaceChildren(...(busy ? [h("span", { class: "spin", "aria-hidden": "true" })] : []), msg);
    if (!busy) sayTimer = setTimeout(() => say.replaceChildren(), isErr ? 6000 : 3000);
  };
  if (menuNote && menuNote.id === c.id) { const n = menuNote; menuNote = null; tell(n.msg, n.isErr); setTimeout(() => btn.focus()); }
  const close = (refocus) => { pop.hidden = true; btn.setAttribute("aria-expanded", "false"); document.removeEventListener("pointerdown", outside, true); if (refocus) btn.focus(); };
  const outside = (e) => { if (!wrap.contains(e.target)) close(false); };
  const item = (label, o = {}) => h("button", { type: "button", role: "menuitem", class: "menu-item" + (o.danger ? " danger" : ""), onclick: o.onclick }, label);
  const items = () => [...pop.querySelectorAll("[role=menuitem], .menu-confirm button")];
  const copy = async () => {
    close(true);
    try { await navigator.clipboard.writeText(trackSummary(c)); tell("Copied"); } catch (e) { tell("Couldn't copy here"); }
  };
  const reload = async () => {
    close(true);
    tell("Reloading metadata…", false, true);
    try {
      const r = await api(`/api/tracks/${c.id}/retag`, { method: "POST" });
      menuNote = { id: c.id, msg: !r.tags_found ? "No tags found in the file" : r.changed ? "Metadata reloaded" : "Metadata already up to date" };
      if (currentId === c.id) renderCard(r.card); else menuNote = null;
      try { await loadTracks(); renderList(); } catch (_) {}
    } catch (e) { tell(e.message, true); }
  };
  const remove = async () => {
    try {
      await api(`/api/tracks/${c.id}`, { method: "DELETE" });
    } catch (e) { close(true); tell(e.message, true); return; }
    currentId = null;
    $("card").replaceChildren(h("p", { class: "muted" }, "Select a track."));
    await refresh();
  };
  const showMain = () => {
    const isFile = !c.source || c.source.kind !== "url"; // links have no file to re-read
    pop.replaceChildren(
      item("Copy track summary", { onclick: copy }),
      isFile ? item("Reload metadata", { onclick: reload }) : null,
      h("hr", { class: "menu-sep" }),
      item("Delete track…", { danger: true, onclick: showConfirm }));
  };
  const showConfirm = () => {
    const name = (c.source && (c.source.title || c.source.uri)) || `Track ${c.id}`;
    pop.replaceChildren(h("div", { class: "menu-confirm", role: "group", "aria-label": "Confirm delete" },
      h("p", {}, h("b", {}, `Delete “${name}”?`), h("br"), h("span", { class: "muted" }, "This removes the track and its analysis from your library.")),
      h("div", { class: "menu-confirm-btns" },
        h("button", { type: "button", onclick: () => { showMain(); items()[0].focus(); } }, "Cancel"),
        h("button", { type: "button", class: "danger-solid", onclick: remove }, "Delete"))));
    pop.querySelector("button").focus();
  };
  btn.onclick = () => {
    if (!pop.hidden) { close(true); return; }
    showMain(); pop.hidden = false; btn.setAttribute("aria-expanded", "true"); items()[0].focus();
    document.addEventListener("pointerdown", outside, true);
  };
  pop.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(true); return; }
    const list = items(), i = list.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); list[(i + 1) % list.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); list[(i - 1 + list.length) % list.length].focus(); }
    else if (e.key === "Tab") close(false);
  });
  return wrap;
}

// ---------- upload ----------
function setStatus(msg, isErr, busy) {
  const el = $("status");
  el.replaceChildren(...(busy ? [h("span", { class: "spin", "aria-hidden": "true" })] : []), msg);
  el.className = isErr ? "error" : "muted";
}
// The server analyzes one request's files one after another, so send one request per file:
// each placeholder can then turn into a track (or a failure) as soon as its own file is done.
let queue = Promise.resolve();
function analyzeBatch(items) {
  if (!items.length) return;
  const rows = items.map((it) => ({ name: it.name, active: false }));
  pending.unshift(...rows);
  renderList();
  queue = queue.then(() => runBatch(items, rows));
}
async function runBatch(items, rows) {
  let done = 0, failed = 0, lastId = null;
  for (let i = 0; i < items.length; i++) {
    const row = rows[i];
    row.active = true;
    setStatus(`Analyzing ${items.length} file(s)… this can take a while.`, false, true);
    renderList();
    let results;
    try { results = (await items[i].send()).results || []; }
    catch (e) { results = [{ track_id: null, title: row.name, error: e.message }]; }
    const bad = results.filter((r) => r.error);
    const ok = results.filter((r) => r.track_id != null);
    done += ok.length; failed += bad.length;
    if (ok.length) lastId = ok[ok.length - 1].track_id;
    // Keep a failed row only for what failed; a single result keeps the name the user gave (the server's title carries a hash prefix).
    pending.splice(pending.indexOf(row), 1, ...bad.map((r) => ({ name: results.length === 1 ? row.name : r.title, failed: true, error: r.error })));
    try { await loadTracks(); } catch (_) {}
    renderList();
  }
  setStatus(`Analyzed ${done} track(s)` + (failed ? `, ${failed} failed` : "") + ".", failed > 0);
  if (lastId != null) await refresh(lastId);
}
function uploadFiles(files) {
  analyzeBatch(files.map((f) => ({
    name: f.name,
    send: () => { const fd = new FormData(); fd.append("files", f); return api("/api/analyze", { method: "POST", body: fd }); },
  })));
}
const drop = $("drop");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
// Dropped folders are walked recursively; only audio files inside them are analyzed (files dropped directly are always sent).
const AUDIO_EXT = /\.(mp3|wav|flac|ogg|oga|opus|m4a|aac|aif|aiff|wma)$/i;
const isAudio = (f) => (f.type || "").startsWith("audio/") || AUDIO_EXT.test(f.name);
const readBatch = (reader) => new Promise((res, rej) => reader.readEntries(res, rej));
const entryFile = (entry) => new Promise((res, rej) => entry.file(res, rej));
async function walkEntry(entry, out, nested) {
  if (entry.isFile) {
    if (entry.name.startsWith(".")) return;
    const f = await entryFile(entry);
    if (!nested || isAudio(f)) out.push(f);
  } else if (entry.isDirectory) {
    const reader = entry.createReader();
    // readEntries returns at most ~100 entries per call, so keep reading until it comes back empty.
    for (let batch = await readBatch(reader); batch.length; batch = await readBatch(reader)) {
      for (const child of batch) await walkEntry(child, out, true);
    }
  }
}
async function collectDropped(dt) {
  // The entries must be taken synchronously; the DataTransfer is emptied once the event handler returns.
  const entries = [...(dt.items || [])].map((i) => (i.kind === "file" && i.webkitGetAsEntry ? i.webkitGetAsEntry() : null));
  if (!entries.length || entries.some((en) => !en)) return [...dt.files];
  const files = [];
  for (const en of entries) await walkEntry(en, files, false);
  return files;
}
async function uploadDropped(dt) {
  const hadFolder = [...(dt.items || [])].some((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()?.isDirectory);
  let files;
  try { files = await collectDropped(dt); } catch (_) { files = [...dt.files]; }
  if (!files.length && hadFolder) setStatus("No audio files found in the dropped folder(s).", true);
  else uploadFiles(files);
}
drop.addEventListener("drop", (e) => { uploadDropped(e.dataTransfer); });
$("file-input").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });
$("folder-input").addEventListener("change", (e) => {
  const files = [...e.target.files].filter((f) => isAudio(f) && !f.name.startsWith("."));
  if (files.length) uploadFiles(files); else setStatus("No audio files found in that folder.", true);
  e.target.value = "";
});
$("url-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const url = $("url-input").value.trim();
  if (!url) return;
  analyzeBatch([{ name: url, send: () => api("/api/analyze-url", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) }) }]);
  $("url-input").value = "";
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
  await loadTracks();
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
