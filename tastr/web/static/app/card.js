// The track card: opening a track, its header and stats, sections and their order, Hear a key and the metadata section.

import { $, ML_REPLACES, VIBE_GROUPS, VIBE_MIN, api, currentId, groupOf, h, isNum, mlState, mlTip, mmss, nap, setCurrentId, tagHelp, vibeHelp, vibeTags } from "./core.js";
import { bpmFilter, camelotNear, keyFilter, lib, loadTracks, renderList, syncCardFilters, vibeFilter, visibleTracks } from "./library.js";
import { setDrumModelOn, setLookupProviders } from "./bulk.js";
import { camPc, charts, labelOpen, setLabelOpen, setMarkHook } from "./labels.js";
import { labelBar } from "./label-bar.js";
import { DAWS, camelotParallel, daw, dawText, keyName, liveView, modeOther, setDaw } from "./daw.js";
import { delay, trackAudio } from "./audio.js";
import { energyChart } from "./chart.js";
import { grooveView } from "./groove.js";
import { loadEdits, savedEdits } from "./groove-edits.js";
import { setRoute } from "./route.js";
import { audioCtx, player, setPlayer, stopAllPlayback } from "./sequencer.js";
import { pollStems, stemStatus, stemsBusy, trackMenu } from "./menu.js";
import { setStatus } from "./upload.js";
import { openGlossary } from "./glossary.js";

// A chip's color strength: the engine's from its 0.20 cut-off; ML moods and voice from 0.1; ML styles score lower (0.10-0.5), so double them.
const chipStrength = (t) => !isNum(t.score) ? 0 : Math.max(0, Math.min(1,
  t.src === "ml" ? (t.mlGroup === "style" ? t.score * 2 : (t.score - 0.1) / 0.9) : (t.score - VIBE_MIN) / (1 - VIBE_MIN)));
// The note under the chips while a track's ML tags aren't in: queued, running, or failed with Try again.
function mlNote(c) {
  const st = mlState(c);
  if (!st || st === "done") return null;
  if (st === "error") return h("p", { class: "ml-state err", role: "status" }, h("b", {}, "ML tags failed"),
    ` for this track${c.ml_tags_error ? ` (${c.ml_tags_error.replace(/\.$/, "")})` : ""}. These are Tastr's own vibes. `,
    h("button", { type: "button", class: "linklike", onclick: () => mlRetry(c) }, "Try again"));
  return h("p", { class: "ml-state", role: "status" }, h("span", { class: "spin", "aria-hidden": "true" }),
    st === "running" ? "Adding ML tags: style, mood and vocals…" : "ML tags are queued for this track.",
    h("span", { class: "muted" }, " Until they're in, these are Tastr's own vibes."));
}
// Swap the open card's Vibe section in place (the rest of the card, and what's playing, stay as they are).
function redrawVibe(c) {
  if (currentId !== c.id || shownCard !== c) return;
  const box = document.querySelector('#card .sec[data-sec="vibe"] .sec-content');
  if (box) { box.replaceChildren(vibeBody(c)); syncCardFilters(visibleTracks().length); }
}
// Polls GET /api/tracks/{id}/ml-tags while a run is queued or running, then shows the tags (or the error).
const mlPolling = new Set();
async function pollMl(c) {
  if (mlPolling.has(c.id)) return;
  mlPolling.add(c.id);
  try {
    for (;;) {
      await nap(4000);
      let st;
      try { st = await api(`/api/tracks/${c.id}/ml-tags`); } catch (_) { continue; }
      const was = mlState(c);
      Object.assign(c, { ml_tags_state: st.state, ml_tags_error: st.error || null });
      if (st.result) c.ml_tags = st.result;
      if (mlState(c) !== was) redrawVibe(c);
      if (st.state !== "queued" && st.state !== "running") break;
      if (currentId !== c.id) break; // another track is open; its card polls for itself
    }
    if (mlState(c) === "done") { await loadTracks(); renderList(); }
  } finally { mlPolling.delete(c.id); }
}
// Try again: POST /api/tracks/{id}/ml-tags starts a run (or joins one), then poll it.
async function mlRetry(c) {
  try {
    const st = await api(`/api/tracks/${c.id}/ml-tags`, { method: "POST" });
    Object.assign(c, { ml_tags_state: st.state, ml_tags_error: st.error || null });
  } catch (e) {
    Object.assign(c, { ml_tags_state: "error", ml_tags_error: e.message });
  }
  redrawVibe(c);
  if (c.ml_tags_state === "queued" || c.ml_tags_state === "running") pollMl(c);
}
function vibeBody(c) {
  const shown = vibeTags(c).sort((a, b) => b.score - a.score), ml = mlState(c) === "done";
  const shownSet = new Set(shown.map((t) => t.tag));
  const all = (c.extras && c.extras["tags.vibe_scores"]) || {};
  // Once ML tags are in, the engine vibes they replace aren't listed, not even as hidden scores.
  const below = Object.entries(all).filter(([tag]) => !shownSet.has(tag) && !(ml && ML_REPLACES[tag])).map(([tag, score]) => ({ tag, score })).sort((a, b) => b.score - a.score);
  const rowOf = (t, on) => h("li", { class: "vibe-row" + (on ? "" : " off"), style: `--g:${groupOf(t.tag).color}` },
    h("span", { class: "vibe-name", tabindex: "0", "data-tip": t.src === "ml" ? `${mlTip(t.tag)} From Essentia's trained models.` : vibeHelp(t.tag) }, h("i", { class: "vibe-dot", title: groupOf(t.tag).name }), t.tag),
    h("span", { class: "vibe-bar", role: "img", "aria-label": `Score ${t.score.toFixed(2)} of 1` }, h("i", { style: `width:${t.score * 100}%` })),
    h("span", { class: "vibe-num" }, t.score.toFixed(2)),
    h("span", { class: "vibe-state" }, on ? (t.src === "ml" ? "ML" : "Shown") : "Hidden"));
  const list = h("ul", { class: "vibe-list", hidden: "" },
    h("li", { class: "vibe-head", "aria-hidden": "true" }, h("span", {}, "Vibe"), h("span", { class: "vibe-axis" }, h("span", { class: "vibe-min", style: `left:${VIBE_MIN * 100}%` }, "0.20 cut-off")), h("span", {}), h("span", {})),
    ...shown.map((t) => rowOf(t, true)), ...below.map((t) => rowOf(t, false)));
  const btn = h("button", { type: "button", class: "vibe-toggle", "aria-expanded": "false" }, "Show all scores");
  btn.onclick = () => { const open = list.hidden; list.hidden = !open; btn.setAttribute("aria-expanded", String(open)); btn.textContent = open ? "Hide scores" : "Show all scores"; };
  const note = h("p", { class: "vibe-note muted" },
    h("span", { class: "vibe-scale", "aria-hidden": "true" }),
    h("span", { tabindex: "0", "data-tip": `Each vibe gets a score from 0 to 1 from the track's measurements. Chips are grouped by kind of vibe (mood, energy, rhythm, sound, tempo), and each group has its own color. A stronger color means a higher score. Vibes scoring under ${VIBE_MIN.toFixed(2)} are left out, and a vibe the track doesn't qualify for at all gets no score.` }, `Stronger color = higher score. Shown from ${VIBE_MIN.toFixed(2)} up.`));
  const st = mlState(c);
  if ((st === "queued" || st === "running") && currentId === c.id) pollMl(c);
  return h("div", { class: "vibe" },
    VIBE_GROUPS.map((g) => ({ g, items: shown.filter((t) => groupOf(t.tag) === g) })).filter((x) => x.items.length).map(({ g, items }) =>
      h("div", { class: "vibe-group", style: `--g:${g.color}` }, h("span", { class: "vibe-glabel" }, g.name),
        h("div", { class: "chips" }, items.map((t) =>
          h("span", { class: "chip" + (lib.vibes.includes(t.tag) ? " on" : ""), role: "button", tabindex: "0", "data-tag": t.tag, "aria-pressed": String(lib.vibes.includes(t.tag)),
            onclick: () => vibeFilter(t.tag), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); vibeFilter(t.tag); } },
            style: `--t:${chipStrength(t)}`,
            "data-tip": `${t.src === "ml" ? `${mlTip(t.tag)} ML tag, score ${t.score.toFixed(2)}.` : `${tagHelp(t.tag)}${isNum(t.score) ? ` Score ${t.score.toFixed(2)}.` : ""}`} Click to filter the track list; click more vibes to narrow it.`.trim() },
            t.tag, t.src === "ml" ? h("span", { class: "ml-mk", "aria-label": "(ML)" }, "✦") : null))))),
    mlNote(c),
    h("div", { class: "card-filter", "data-kind": "vibe", hidden: "" }),
    ml && shown.some((t) => t.src === "ml") ? h("p", { class: "ml-src muted" }, "✦ Tagged by Essentia's trained models (ML). The rest are measured by Tastr.") : null,
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
  setRoute(`track/${id}`);  // a refresh reopens this track
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
export const LOOKUP_MARK = { agrees: ["✓", "matches ours"], double: ["×2", "double ours"], half: ["½", "half ours"],
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

// Metadata › Listed elsewhere: every source, filled in or not (sandbox v201, listed-elsewhere-spec.md). The server
// lists the sources (lookup_sources, with `configured`); a source with no stored row reads "none".
const LOOKUP_HOW = { rekordbox: "Your Rekordbox export, matched by file name or artist and title", acousticbrainz: "Open data, by artist and title" };
const LOOKUP_SAY = {
  none: "Not looked up yet.", not_found: "Not found: it has no listing for this artist and title.",
  pending: "Asked: the catalog is adding this song. Check back later.", no_tags: "Can't look up: the file has no artist and title tags.",
  limited: "Paused: the catalog's limit was reached. It will be asked again later.",
  rb_none: "Not in your Rekordbox export.", rb_new: "Not imported yet.", off: "Not set up on this server." };
const fmtDay = (iso) => new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
export function lookupsFor(c) {
  const stored = Object.fromEntries((c.lookups || []).map((x) => [x.provider, x]));
  return (c.lookup_sources || []).map((src) => ({ ...src, status: "none", ...(stored[src.provider] || {}),
    label: src.label, homepage: src.homepage, configured: src.configured,
    how: LOOKUP_HOW[src.provider] || "Online catalog, by artist and title" }));
}
function lookupTable(c) {
  const core = c.core || {};
  const mark = (what, cmp) => { const m = cmp && LOOKUP_MARK[cmp]; return m ? h("span", { class: `lk-mark lk-${cmp}`, title: `${what} ${m[1]}` }, m[0]) : null; };
  const row = (name, how, val, cls) => h("div", { class: "lk-row" + (cls ? " " + cls : ""), role: "row" },
    h("div", { class: "lk-name", role: "rowheader" }, name, how ? h("small", { class: "muted" }, how) : null),
    h("div", { class: "lk-val", role: "cell" }, ...val));
  const ours = [isNum(core.bpm) ? `${core.bpm.toFixed(1)} BPM` : "no BPM",
    core.key ? `${keyName(core.key)}${core.key.camelot ? ` (${core.key.camelot})` : ""}` : "no key"].join(" · ");
  const rows = lookupsFor(c);
  return h("div", { class: "meta-lookups" }, h("h4", {}, "Listed elsewhere"),
    h("p", { class: "muted lk-intro" }, "What other sources list for this track's BPM and key, next to what Tastr measured. They never change Tastr's values."),
    h("div", { class: "lk-table", role: "table", "aria-label": "BPM and key from other sources" },
      row(h("b", {}, "Tastr"), "Measured from the audio", [h("span", { class: "lk-v" }, ours)], "lk-ours"),
      ...rows.map((x) => {
        const name = h("a", { href: x.homepage, target: "_blank", rel: "noopener", class: "lk-src" }, x.label);
        if (x.status === "found") return row(name, x.how, [
          h("span", { class: "lk-v" }, isNum(x.bpm) ? h("span", {}, `${Math.round(x.bpm * 10) / 10} BPM`, mark("BPM", x.compare && x.compare.bpm)) : "no BPM", " · ",
            x.key ? h("span", {}, `${x.key}${x.camelot ? ` (${x.camelot})` : ""}`, mark("Key", x.compare && x.compare.key)) : "no key"),
          x.url && x.url !== x.homepage ? h("a", { class: "lk-open", href: x.url, target: "_blank", rel: "noopener" }, "See the listing ↗") : null].filter(Boolean));
        if (!x.configured) return row(name, x.how, [h("span", { class: "muted" }, LOOKUP_SAY.off), h("small", { class: "muted" }, "An admin turns it on with TASTR_LOOKUP (docs/lookup.md).")], "lk-off");
        if (x.status === "error") return row(name, x.how, [h("span", { class: "lk-err" }, `Lookup failed: ${(x.error || "the service didn't answer").replace(/\.$/, "")}.`)], "lk-off");
        if (x.provider === "rekordbox" && x.status === "none" && x.imported === false) return row(name, x.how, [h("span", { class: "muted" }, LOOKUP_SAY.rb_new), h("small", { class: "muted" }, "An admin loads it from your export (docs/rekordbox.md).")], "lk-off");
        // A stored "not found" that expires (FreqBlog's terms): say when it was checked; it's asked again later.
        if (x.status === "not_found" && x.checked_at) return row(name, x.how, [h("span", { class: "muted" }, "Not listed yet."), h("small", { class: "muted" }, `Checked ${fmtDay(x.checked_at)}. Tastr asks again after a few days.`)], "lk-off");
        const say = x.provider === "rekordbox" && x.status === "none" ? LOOKUP_SAY.rb_none : LOOKUP_SAY[x.status] || x.status;
        return row(name, x.how, [h("span", { class: "muted" }, say)], "lk-off");
      })),
    h("p", { class: "muted lk-key" }, "✓ matches Tastr · ×2 / ½ double or half · ≈ a related key (relative, fifth or parallel) · ≠ differs"),
    // FreqBlog's terms ask for "Audio analysis by FreqBlog" where people can find it; the full list is Glossary › Credits.
    rows.some((x) => x.provider === "freqblog" && x.configured) ? h("p", { class: "muted lk-credit" }, "Audio analysis by ",
      h("a", { href: "https://freqblog.com", target: "_blank", rel: "noopener", class: "link" }, "FreqBlog"), ". ",
      h("a", { href: "#g-credits", class: "link", onclick: (e) => { e.preventDefault(); openGlossary("g-credits"); } }, "All credits")) : null);
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
// key.mode_uncertain (the thirds barely sound and the key model barely prefers this mode): major or minor is a guess.
// Missing on older cards, which then show the key as before.
const MODE_Q_TIP = "Major or minor is a guess for this track: its third barely sounds, so Tastr can't hear which it is. Tracks like this get the mode wrong about 1 time in 3 (about half for rock). If you know, label the key.";
function keyStat(key) {
  const sure = key && isNum(key.likely_correct) ? sureness(key.likely_correct) : null;
  if (!key || !key.camelot) return stat(key ? `${keyName(key)}` : "–", key ? [key.scale, sure && sure.text].filter(Boolean).join(" · ") || "key" : "key", sure && sure.title);
  if (key.mode_uncertain && isNum(key.tonic) && camelotParallel(key.camelot)) return keyUnsureStat(key, sure);
  return statBtn("key", keyName(key), [key.camelot, key.scale, sure && sure.text].filter(Boolean).join(" · "),
    [sure && sure.title, `Click to show tracks in ${key.camelot} and the keys that mix with it (${camelotNear(key.camelot).slice(1).join(", ")}).`].filter(Boolean).join(" "), () => keyFilter(key.camelot));
}
// "C major?" with the mode muted, and the other mode named under it: "8B · or C minor (5A)".
function keyUnsureStat(key, sure) {
  const o = modeOther(key), oCam = camelotParallel(key.camelot), tonic = keyName(key).replace(/ (major|minor)$/, "");
  const click = `Click to show tracks in ${key.camelot} and the keys that mix with it.`;
  const b = statBtn("key", h("span", {}, tonic, " ", h("span", { class: "mode-q" }, `${key.mode}?`)), [key.camelot, `or ${keyName(o)} (${oCam})`, sure && sure.text].filter(Boolean).join(" · "),
    `${[MODE_Q_TIP, sure && sure.title].filter(Boolean).join(" ")}\n${click}`, () => keyFilter(key.camelot));
  b.dataset.unsure = "";
  b.setAttribute("aria-label", `Key: ${keyName(key)} or ${keyName(o)}, the mode is a guess. Show tracks in ${key.camelot} and keys that mix with it.`);
  return b;
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
  if (DAWS[daw].code) body.push(liveView(c));
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
  if (c.metadata) root.append(metadataSection(c.metadata, c));
  const energy = c.series && c.series.energy;
  if (energy && energy.times && energy.times.length > 1 || (c.sections || []).length) {
    root.append(section("energy", "Energy & sections", [energyChart(energy, c.sections || [], c.duration, c)]));
  }
  if (vibeTags(c).length || mlState(c) === "error") root.append(section("vibe", "Vibe", [vibeBody(c)]));
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

function metadataSection(meta, c) {
  const body = [];
  for (const [group, kv] of Object.entries(meta)) {
    const rows = Object.entries(kv).filter(([, v]) => v != null && v !== "");
    body.push(h("div", { class: "meta-group" }, h("h4", {}, group), rows.length
      ? h("dl", { class: "kv" }, rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, String(v))]))
      : h("p", { class: "muted meta-empty" }, "None found. The title above came from the file name.")));
  }
  return section("meta", "Metadata", [h("div", { class: "meta-cols" }, body), lookupTable(c)], false);
}

export function setHearDrums(v) { return (hearDrums = v); }
export function setHearChordDb(v) { return (hearChordDb = v); }
