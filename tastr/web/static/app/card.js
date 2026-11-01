// The track card: opening a track, its header and stats, sections and their order, Hear a key and the metadata section.

import { $, VIBE_GROUPS, VIBE_MIN, api, currentId, groupOf, h, isNum, mmss, setCurrentId, tagHelp, vibeHelp } from "./core.js";
import { bpmFilter, camelotNear, keyFilter, lib, syncCardFilters, vibeFilter, visibleTracks } from "./library.js";
import { setDrumModelOn, setLookupProviders } from "./bulk.js";
import { camPc, charts, labelOpen, setLabelOpen, setMarkHook } from "./labels.js";
import { labelBar } from "./label-bar.js";
import { DAWS, daw, dawText, keyName, setDaw, strudelView } from "./daw.js";
import { delay, trackAudio } from "./audio.js";
import { energyChart } from "./chart.js";
import { grooveView } from "./groove.js";
import { loadEdits, savedEdits } from "./groove-edits.js";
import { audioCtx, player, setPlayer, stopAllPlayback } from "./sequencer.js";
import { pollStems, stemStatus, stemsBusy, trackMenu } from "./menu.js";
import { setStatus } from "./upload.js";
import { openGlossary } from "./glossary.js";

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
          h("span", { class: "chip" + (lib.vibes.includes(t.tag) ? " on" : ""), role: "button", tabindex: "0", "data-tag": t.tag, "aria-pressed": String(lib.vibes.includes(t.tag)),
            onclick: () => vibeFilter(t.tag), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); vibeFilter(t.tag); } },
            style: `--t:${isNum(t.score) ? Math.max(0, Math.min(1, (t.score - VIBE_MIN) / (1 - VIBE_MIN))) : 0}`,
            "data-tip": `${tagHelp(t.tag)}${isNum(t.score) ? ` Score ${t.score.toFixed(2)}.` : ""} Click to filter the track list; click more vibes to narrow it.`.trim() }, t.tag))))),
    h("div", { class: "card-filter", "data-kind": "vibe", hidden: "" }),
    h("div", { class: "vibe-foot" }, note, h("a", { class: "link gl-link", href: "#g-vibes", onclick: (e) => { e.preventDefault(); openGlossary("g-vibes"); } }, "What the vibes mean"), btn), list);
}
// Hear a key: that key's tonic chord (low root, root, third, fifth, octave) alone for 2 s, then ~6 s of the track's drop or
// chorus fades in under it. With the drums off, the track part is its harmonic part (HPSS, static/hpss.js), what the key detector hears.
export let hearing = null, hearDrums = true, hearChordDb = 0; // chord level against the track, in dB
const harmCache = { key: null, buf: null, offset: 0 };
function harmonicClip(audio, c, t0, len) {
  const key = `${c.id}|${t0.toFixed(2)}|${len}`;
  if (harmCache.key !== key) {
    const b = audio.buf, sr = b.sampleRate, pad = 0.5, a = Math.max(0, Math.floor((t0 - pad) * sr)), e = Math.min(b.length, Math.ceil((t0 + len + pad) * sr));
    const y = new Float32Array(Math.max(0, e - a));
    for (let ch = 0; ch < b.numberOfChannels; ch++) { const d = b.getChannelData(ch); for (let i = 0; i < y.length; i++) y[i] += d[a + i] / b.numberOfChannels; }
    const hh = hpssHarmonic(y);
    if (!hh) return null;
    const buf = audioCtx().createBuffer(1, hh.length, sr); buf.copyToChannel(hh, 0);
    Object.assign(harmCache, { key, buf, offset: a / sr });
  }
  return harmCache;
}
export async function hearKey(c, cam, btn) {
  const was = hearing; stopAllPlayback(); if (was && was.btn === btn) return;
  const pc = camPc(cam); if (pc == null) return;
  btn.textContent = "…";
  const sec = (c.sections || []).find((q) => /drop|chorus/i.test(q.label || "")) || (c.sections || [])[1];
  // the chord alone first (SOLO s), then the track fades in under it, so you hear what it adds
  const SOLO = 2, TRACK = 6, t0 = sec ? sec.start + 0.05 : c.duration * 0.3, len = SOLO + TRACK;
  let audio, part;
  try {
    audio = await trackAudio(c);
    if (hearDrums) part = { buf: audio.buf, offset: 0 };
    else { await delay(0); part = harmonicClip(audio, c, t0, TRACK) || { buf: audio.buf, offset: 0 }; }
  } catch (e) { btn.textContent = "🔊"; setStatus(`Couldn't load the audio: ${e.message}`, true); return; }
  if (!btn.isConnected) { btn.textContent = "🔊"; return; }
  const ctx = audioCtx(); if (ctx.state === "suspended") ctx.resume();
  const now = ctx.currentTime + 0.05;
  const out = ctx.createGain(); out.gain.value = 1; out.connect(ctx.destination);
  const src = ctx.createBufferSource(); src.buffer = part.buf; const tg = ctx.createGain(); src.connect(tg).connect(out);
  tg.gain.setValueAtTime(0, now + SOLO); tg.gain.linearRampToValueAtTime(0.7, now + SOLO + 0.6); src.start(now + SOLO, Math.max(0, t0 - part.offset), TRACK);
  const minor = cam.endsWith("A"), root = 48 + pc, notes = [root - 12, root, root + (minor ? 3 : 4), root + 7, root + 12];
  const lp = ctx.createBiquadFilter(); lp.type = "lowpass"; lp.frequency.value = 1800;
  const pad = ctx.createGain(), lvl = ctx.createGain(); lvl.gain.value = Math.pow(10, hearChordDb / 20); lp.connect(pad).connect(lvl).connect(out);
  pad.gain.setValueAtTime(0, now); pad.gain.linearRampToValueAtTime(0.16, now + 0.4); pad.gain.setValueAtTime(0.16, now + len - 0.6); pad.gain.linearRampToValueAtTime(0, now + len);
  const oscs = notes.map((n, i) => { const o = ctx.createOscillator(); o.type = i ? "triangle" : "sine"; o.frequency.value = 440 * Math.pow(2, (n - 69) / 12); const g = ctx.createGain(); g.gain.value = i ? 0.22 : 0.4; o.connect(g).connect(lp); o.start(now); o.stop(now + len); return o; });
  btn.textContent = "■"; btn.classList.add("on");
  // stop() clears onended first, or a restart's late onended would reset the new clip's button
  const me = { btn, c, cam, lvl, stop() { src.onended = null; try { src.stop(); } catch (_) {} oscs.forEach((o) => { try { o.stop(); } catch (_) {} }); out.disconnect(); btn.textContent = "🔊"; btn.classList.remove("on"); if (hearing === me) hearing = null; if (player === me) setPlayer(null); } };
  hearing = me; setPlayer(me);
  src.onended = () => me.stop();
}

export async function openTrack(id) {
  setCurrentId(id);
  // Update the highlight in place so the list keeps its scroll position and focus.
  document.querySelectorAll("#track-list li.trk").forEach((li) => {
    const on = +li.dataset.id === id;
    li.classList.toggle("active", on);
    if (on) { li.setAttribute("aria-current", "true"); li.scrollIntoView({ block: "nearest" }); } else li.removeAttribute("aria-current");
  });
  document.querySelectorAll("#review-list > li[data-id]").forEach((li) => li.classList.toggle("active", +li.dataset.id === id)); // both review tabs
  switchTo("library");
  try {
    const [card] = await Promise.all([api(`/api/tracks/${id}`), loadEdits(id)]);
    if (currentId === id) renderCard(card);
  } catch (e) {
    $("card").replaceChildren(h("p", { class: "error" }, e.message));
  }
}
export function switchTo(view) {
  document.querySelector(`nav button[data-view=${view}]`).click();
}

// "Listed elsewhere": what online catalogs say (tastr.lookup), next to ours, never instead of it.
const LOOKUP_MARK = { agrees: ["✓", "matches ours"], double: ["×2", "double ours"], half: ["½", "half ours"],
  relative: ["≈", "relative key of ours"], fifth: ["≈", "a fifth from ours"], parallel: ["≈", "parallel key of ours"],
  differs: ["≠", "differs from ours"] };
function lookupLine(c) {
  const rows = (c.lookups || []).filter((x) => x.status === "found" && (isNum(x.bpm) || x.key));
  if (!rows.length) return null;
  const mark = (what, cmp) => {
    const m = cmp && LOOKUP_MARK[cmp];
    return m ? h("span", { class: `lk-mark lk-${cmp}`, title: `${what} ${m[1]}` }, m[0]) : null;
  };
  return h("p", { class: "lookup-line" }, h("span", { class: "muted" }, "Listed elsewhere: "),
    ...rows.flatMap((x, i) => [
      i ? h("span", { class: "muted" }, " · ") : null,
      h("a", { href: x.url || x.homepage, target: "_blank", rel: "noopener", class: "lk-src",
        title: [x.matched_artist, x.matched_title].filter(Boolean).join(" – ") || x.label }, x.label),
      ": ",
      isNum(x.bpm) ? h("span", {}, `${Math.round(x.bpm * 10) / 10} BPM`, mark("BPM", x.compare && x.compare.bpm)) : null,
      isNum(x.bpm) && x.key ? ", " : null,
      x.key ? h("span", {}, x.key, mark("Key", x.compare && x.compare.key)) : null].filter((n) => n !== null)));
}

export function stat(value, label, title) {
  return h("div", { class: "stat", ...(title ? { title } : {}) }, h("b", {}, value), h("span", {}, label));
}
// A stat that filters the track list when clicked (the card's BPM and key); pressed while its filter is on.
const statBtn = (f, value, label, tip, onclick) => h("button", { type: "button", class: "stat stat-btn", "data-f": f, "aria-pressed": "false", title: tip, onclick }, h("b", {}, value), h("span", {}, label));

// "BPM · 85% likely right" from the benchmark-calibrated chance; cards analyzed before calibration show the raw score.
function sureness(likely, raw) {
  if (isNum(likely)) return { text: `${Math.round(likely * 100)}% likely right`,
    title: `About ${Math.round(likely * 100)} in 100 benchmark tracks with this confidence score were right.` };
  if (isNum(raw)) return { text: `${Math.round(raw * 100)}% conf`,
    title: "How clear the evidence was in this track, not how often it's right. Re-analyze to get a calibrated figure." };
  return null;
}

export let sectionBus = new EventTarget();
// Beats per bar (3 or 4; cards analyzed before meter detection are 4/4) and 16th-note steps per bar.
export const beatsPerBar = (c) => ((c && c.core && c.core.beats_per_bar) === 3 ? 3 : 4);
export const barSteps = (c) => 4 * beatsPerBar(c);
const METER_TIP = { detected: "Time signature, detected from how the beats group: in 3s (3/4, like a waltz) or 4s (4/4). Most tracks are 4/4. If it's wrong, change it in the ⋯ menu.",
  set: "Time signature you picked in the ⋯ menu. Re-analyzing keeps it; pick “Detect the time signature again” to go back." };

export let shownCard = null;

// A fresh card: stop what's playing, close an expanded chart, start the card's own section bus, and load its saved edits.
function resetForCard(c) {
  setLookupProviders(c.lookup_providers || []);
  setDrumModelOn(!!c.drum_model_available);
  shownCard = c;
  stopAllPlayback();
  document.querySelectorAll(".chart-backdrop").forEach((b) => b.remove()); document.body.classList.remove("chart-open");
  if (!savedEdits.has(c.id)) loadEdits(c.id).then(() => { if (savedEdits.has(c.id) && currentId === c.id && $("card").dataset.id === String(c.id)) renderCard(c); });
  if (c.stems_state && !stemsBusy(stemStatus.get(c.id) || {})) { stemStatus.set(c.id, { state: c.stems_state }); pollStems(); } // a fresh card knows best
  sectionBus = new EventTarget();
  setMarkHook(null); charts.clear();
}

// BPM and key stats; a click filters the library to tracks that mix with this one.
function bpmStat(core) {
  if (!isNum(core.bpm)) return stat("–", "BPM");
  const sure = sureness(core.bpm_likely_correct, core.bpm_confidence);
  return statBtn("bpm", core.bpm.toFixed(1), sure ? `BPM · ${sure.text}` : "BPM",
    [sure && sure.title, `Click to show tracks from ${Math.round(core.bpm - 5)} to ${Math.round(core.bpm + 5)} BPM.`].filter(Boolean).join(" "), () => bpmFilter(core.bpm));
}
function keyStat(key) {
  const sure = key && isNum(key.likely_correct) ? sureness(key.likely_correct) : null;
  if (!key || !key.camelot) return stat(key ? `${keyName(key)}` : "–", key ? [key.scale, sure && sure.text].filter(Boolean).join(" · ") || "key" : "key", sure && sure.title);
  return statBtn("key", keyName(key), [key.camelot, key.scale, sure && sure.text].filter(Boolean).join(" · "),
    [sure && sure.title, `Click to show tracks in ${key.camelot} and the keys that mix with it (${camelotNear(key.camelot).slice(1).join(", ")}).`].filter(Boolean).join(" "), () => keyFilter(key.camelot));
}
const energyStat = (core) => h("div", { class: "stat" },
  h("b", {}, isNum(core.energy) ? core.energy.toFixed(2) : "–"),
  h("span", {}, "energy"),
  h("div", { class: "meter" }, h("i", { style: `width:${isNum(core.energy) ? Math.max(0, Math.min(1, core.energy)) * 100 : 0}%` })));

// The card head: title and menu, artist, the stats, the catalog line, and the label bar.
function cardHead(c) {
  const core = c.core || {};
  const head = h("div", { class: "card-head" },
    h("div", { class: "title-row" }, h("h2", {}, (c.source && (c.source.title || c.source.uri)) || `Track ${c.id}`), trackMenu(c)),
    h("p", { class: "card-sub" + (c.source.artist ? "" : " muted") }, [c.source.artist, c.source.album].filter(Boolean).join(" · ") || "No artist or album in the file tags"),
    stat(mmss(c.duration), "duration"),
    bpmStat(core),
    keyStat(core.key),
    isNum(core.bpm) ? stat(`${beatsPerBar(c)}/4`, core.meter_set ? "time · set by you" : "time", core.meter_set ? METER_TIP.set : METER_TIP.detected) : null
  );
  head.append(energyStat(core));
  const listed = lookupLine(c);
  if (listed) head.append(listed);
  head.append(h("div", { class: "card-filter", "data-kind": "head", hidden: "" }));
  if (labelOpen !== null && labelOpen !== c.id) setLabelOpen(null);
  head.append(labelBar(c));
  return head;
}

// Drums, bass & chords, when the card has any of them (or a template drum pattern to start from).
function grooveSection(c, sugs, templates) {
  const key = (c.core || {}).key;
  const detGroove = sugs.find((s) => s.data && s.data.origin === "detected" && s.data.pattern && typeof s.data.pattern === "object");
  const bassSug = sugs.find((s) => s.category === "bass" && s.data && Array.isArray(s.data.loop));
  const chordSug = sugs.find((s) => s.category === "harmony" && s.data && Array.isArray(s.data.progression));
  const hasKey = key && isNum(key.tonic);
  if (!(detGroove || templates.length || (hasKey && (bassSug || chordSug)))) return null;
  return section("groove", "Drums, bass & chords", [grooveView(c, detGroove, bassSug, chordSug, templates)]);
}

// "Try this in [DAW ▾]": the Strudel view, or the ideas grouped by kind in the picked DAW's terms.
function trySection(c, sugs) {
  if (!sugs.length) return null;
  const pick = h("select", { class: "daw-pick", "aria-label": "Your DAW", onchange: (e) => {
    setDaw(e.target.value);
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
  return section("try", ["Try this in ", pick], body);
}

export function renderCard(c) {
  resetForCard(c);
  const root = $("card");
  root.replaceChildren();
  root.dataset.id = c.id;
  root.append(cardHead(c));
  if (c.metadata) root.append(metadataSection(c.metadata));
  const energy = c.series && c.series.energy;
  if (energy && energy.times && energy.times.length > 1 || (c.sections || []).length) {
    root.append(section("energy", "Energy & sections", [energyChart(energy, c.sections || [], c.duration, c)]));
  }
  if ((c.tags || []).length) root.append(section("vibe", "Vibe", [vibeBody(c)]));
  // Template drum patterns are tempo-based guesses: they live in the Drums part's Pattern picker, not as grids or in the Try list.
  const isTemplate = (s) => s.data && s.data.origin === "template";
  const templates = (c.suggestions || []).filter(isTemplate), sugs = (c.suggestions || []).filter((s) => !isTemplate(s));
  const groove = grooveSection(c, sugs, templates), tryIt = trySection(c, sugs);
  if (groove) root.append(groove);
  if (tryIt) root.append(tryIt);
  const errs = Object.entries(c.errors || {});
  if (errs.length) root.append(h("div", { class: "errors" }, "Some stages failed: ", errs.map(([k, v]) => h("div", {}, `${k}: ${v}`))));
  placeSections();
  syncCardFilters(visibleTracks().length);
}

// Card sections are <details>; which are open is remembered across tracks (Metadata starts closed). Each header has a drag
// grip and up/down buttons that move the section; the order is the reader's own (see secOrder).
const openState = {};
function section(key, title, body, defaultOpen = true) {
  const open = key in openState ? openState[key] : defaultOpen;
  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
  const name = typeof title === "string" ? title : (SEC_NAMES[key] || key);
  const handle = h("span", { class: "sec-grip", draggable: "true", title: "Drag to move this section. The order sticks for every track.", "aria-hidden": "true", onclick: stop }, "⠿");
  const up = h("button", { type: "button", class: "sec-mv", "data-dir": "-1", title: `Move ${name} up`, "aria-label": `Move ${name} up`, onclick: (e) => { stop(e); moveSection(key, -1, e.currentTarget); } }, "↑");
  const down = h("button", { type: "button", class: "sec-mv", "data-dir": "1", title: `Move ${name} down`, "aria-label": `Move ${name} down`, onclick: (e) => { stop(e); moveSection(key, 1, e.currentTarget); } }, "↓");
  const det = h("details", { class: "sec", "data-sec": key, ...(open ? { open: "" } : {}) },
    h("summary", {}, handle, title, h("span", { class: "sec-move" }, up, down)), h("div", { class: "sec-content" }, body));
  det.addEventListener("toggle", () => { openState[key] = det.open; });
  // drag by the grip onto another section's header
  handle.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/x-tastr-sec", key); e.dataTransfer.effectAllowed = "move"; det.classList.add("sec-dragging"); });
  handle.addEventListener("dragend", () => { det.classList.remove("sec-dragging"); document.querySelectorAll(".sec-drop-above,.sec-drop-below").forEach((x) => x.classList.remove("sec-drop-above", "sec-drop-below")); });
  const summ = det.firstChild;
  const below = (e) => { const r = summ.getBoundingClientRect(); return e.clientY > r.top + r.height / 2; };
  summ.addEventListener("dragover", (e) => { if (!e.dataTransfer.types.includes("text/x-tastr-sec")) return; e.preventDefault(); const b = below(e); det.classList.toggle("sec-drop-below", b); det.classList.toggle("sec-drop-above", !b); });
  summ.addEventListener("dragleave", () => det.classList.remove("sec-drop-above", "sec-drop-below"));
  summ.addEventListener("drop", (e) => { const from = e.dataTransfer.getData("text/x-tastr-sec"); det.classList.remove("sec-drop-above", "sec-drop-below"); if (!from || from === key) return; e.preventDefault(); dropSection(from, key, below(e)); });
  return det;
}
// Card section order: the reader's own, kept in this browser (localStorage tastr-sec-order) and used for every track.
// Unknown keys are dropped and new ones appended, so a section added later just shows up at the end.
const SEC_DEFAULT = ["meta", "energy", "vibe", "groove", "try"];
const SEC_NAMES = { meta: "Metadata", energy: "Energy & sections", vibe: "Vibe", groove: "Drums, bass & chords", try: "Try this in" };
let secOrder = (() => {
  try { const v = JSON.parse(localStorage.getItem("tastr-sec-order") || "null"); if (Array.isArray(v)) return [...v.filter((k) => SEC_DEFAULT.includes(k)), ...SEC_DEFAULT.filter((k) => !v.includes(k))]; } catch (_) {}
  return [...SEC_DEFAULT];
})();
const secCustom = () => secOrder.join() !== SEC_DEFAULT.join();
function saveSecOrder() { try { if (secCustom()) localStorage.setItem("tastr-sec-order", JSON.stringify(secOrder)); else localStorage.removeItem("tastr-sec-order"); } catch (_) {} }
// Put the card's sections in secOrder, in place (charts and players keep their state).
function placeSections() {
  const root = $("card"), els = {}; root.querySelectorAll(":scope > details.sec[data-sec]").forEach((d) => (els[d.dataset.sec] = d));
  const tail = root.querySelector(":scope > .errors, :scope > .sec-order-note");
  for (const k of secOrder) if (els[k]) root.insertBefore(els[k], tail);
  const shown = secOrder.filter((k) => els[k]);
  shown.forEach((k, i) => { els[k].querySelector('.sec-mv[data-dir="-1"]').disabled = i === 0; els[k].querySelector('.sec-mv[data-dir="1"]').disabled = i === shown.length - 1; });
  let note = root.querySelector(":scope > .sec-order-note");
  if (secCustom()) {
    if (!note) { note = h("p", { class: "sec-order-note muted" }, "You've changed the section order. ", h("button", { type: "button", class: "linklike", onclick: () => { secOrder = [...SEC_DEFAULT]; saveSecOrder(); placeSections(); } }, "Reset order")); root.append(note); }
  } else note?.remove();
}
const secFlash = (k) => { const d = $("card").querySelector(`:scope > details[data-sec="${k}"]`); if (!d) return; d.classList.remove("sec-moved"); void d.offsetWidth; d.classList.add("sec-moved"); };
// Swap with the neighbour among the sections shown; focus stays on the button, or the other arrow once this one is disabled.
function moveSection(key, dir, btn) {
  const shown = secOrder.filter((k) => $("card").querySelector(`:scope > details[data-sec="${k}"]`)), i = shown.indexOf(key), j = i + dir;
  if (i < 0 || j < 0 || j >= shown.length) return;
  [shown[i], shown[j]] = [shown[j], shown[i]];
  secOrder = [...shown, ...secOrder.filter((k) => !shown.includes(k))]; saveSecOrder(); placeSections(); secFlash(key);
  if (btn && btn.disabled) btn.parentElement.querySelector(".sec-mv:not(:disabled)")?.focus(); else btn?.focus();
}
function dropSection(from, onto, after) {
  const o = secOrder.filter((k) => k !== from), at = o.indexOf(onto) + (after ? 1 : 0);
  o.splice(at, 0, from); secOrder = o; saveSecOrder(); placeSections(); secFlash(from);
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

export function setHearDrums(v) { return (hearDrums = v); }
export function setHearChordDb(v) { return (hearChordDb = v); }
