// "Try this in": the DAW glossaries and the Strudel view.

import { api, cap, h, isNum, progBar } from "./core.js";
import { sectionBus } from "./card.js";
import { allEdits, editedKeys, laneName } from "./groove.js";
import { onStems, startStems, stemsEta, stemsLabel, stemsOf } from "./menu.js";

// Suggestion text marks DAW-specific parts as {tokens}; each DAW supplies a glossary for them.
// Ableton Live is the only glossary so far; Strudel shows generated code instead. Other DAWs show in the picker but can't be selected.
export const DAWS = {
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
export let daw = "ableton";

export function dawText(s) {
  const tpl = s.data && s.data.template;
  if (!tpl) return s.body;
  const tokens = DAWS[daw].tokens || {};
  return tpl.replace(/\{(\w+)\}/g, (m, k) => (tokens[k] ? tokens[k](s.data) : m));
}

let strudelAudio = true; // "Include the original audio", remembered for the session
let strudelMix = "full"; // "full" or "stems"
const strudelStems = new Set(["vocals", "bass", "other"]); // Drums off: the sketch drums replace the original ones
let strudelMine = true; // "Patterns: Your edits" (vs Detected) when the track has edits, remembered for the session
export let serverCfg = {}; // /api/config, filled in at startup
const strudelLinks = new Map(); // track id -> signed audio URL on a shared server (null while it loads)
const strudelStarts = new Map(); // track id -> the bar the sketch plays from ("Start at"), for the session
// Where strudel.cc fetches the original audio (stems sit next to it). A shared server is behind a login strudel.cc
// can't pass, so it hands out a signed link that works for a week; until that arrives the sketch has no audio.
function strudelAudioUrl(c, repaint) {
  if (!serverCfg.hosted) return `${location.origin}/api/tracks/${c.id}/audio`;
  if (!strudelLinks.has(c.id)) {
    strudelLinks.set(c.id, null);
    api(`/api/tracks/${c.id}/strudel-link`).then((r) => { strudelLinks.set(c.id, location.origin + r.audio); repaint(); })
      .catch(() => strudelLinks.delete(c.id));
  }
  return strudelLinks.get(c.id);
}
export function strudelView(c) {
  const say = h("span", { class: "muted st-say", role: "status", "aria-live": "polite" });
  const pre = h("pre", { class: "st-code", tabindex: "0", "aria-label": "Generated Strudel code" });
  const open = h("a", { class: "btn-link", target: "_blank", rel: "noopener" }, "Open in Strudel ↗");
  const note = h("p", { class: "muted st-note" });
  const stemsRow = h("div", { class: "st-stems" });
  const srcRow = h("div", { class: "st-src", role: "radiogroup", "aria-label": "Which patterns to use" });
  // "Start at": the sketch's `const start`, so Ctrl+Enter plays from that section instead of the top.
  const startSel = h("select", { id: "st-start" }, h("option", { value: "1" }, "The top"),
    ...(c.sections || []).filter((x) => x.start_bar > 1).map((x) =>
      h("option", { value: String(x.start_bar) }, `${[cap(x.label), x.letter].filter(Boolean).join(" ")} (bar ${x.start_bar})`)));
  startSel.value = String(strudelStarts.get(c.id) || 1);
  startSel.addEventListener("change", () => { strudelStarts.set(c.id, +startSel.value); paint(); });
  const startRow = h("label", { class: "st-opt st-start", for: "st-start" }, "Start at ", startSel);
  let code = "";
  const paint = () => {
    const audioUrl = c.has_audio && strudelAudio ? strudelAudioUrl(c, paint) : null;
    const audio = !!audioUrl, st = stemsOf(c);
    const useStems = audio && strudelMix === "stems" && st.state === "ready";
    const n = editedKeys(allEdits(c)).length;
    srcRow.hidden = !n;
    srcRow.replaceChildren(h("span", { class: "muted" }, "Patterns:"), ...[["detected", "Detected"], ["mine", `Your edits (${n} section${n === 1 ? "" : "s"})`]].map(([val, label]) => {
      const r = h("input", { type: "radio", name: "st-src", value: val, ...((val === "mine") === strudelMine ? { checked: "" } : {}) });
      r.addEventListener("change", () => { strudelMine = val === "mine"; paint(); });
      return h("label", { class: "st-opt" }, r, " " + label);
    }));
    code = strudelCode(c, audioUrl, useStems ? STRUDEL_STEMS.filter((n) => strudelStems.has(n)) : null,
      n && strudelMine ? allEdits(c) : null, +startSel.value || 1);
    pre.textContent = code;
    open.href = strudelLink(code);
    paintStems(audio, st);
    note.textContent = "Opens strudel.cc in a new tab with this code loaded. Drum sounds load from Strudel's sample library the first time you play." +
      (!audio ? "" : serverCfg.hosted ? ` The original ${useStems ? "stems stream" : "audio streams"} from this Tastr server through a private link that works for a week.`
        : ` The original ${useStems ? "stems stream" : "audio streams"} from your Tastr server, so keep it running; the first time, your browser may ask to allow strudel.cc to reach your local network.`);
  };
  // Full mix / Stems, under the audio checkbox. Stems need a separation first; the code uses the full mix until it is done.
  const paintStems = (audio, st) => {
    stemsRow.hidden = !audio;
    if (!audio) return;
    const radio = (val, label) => {
      const r = h("input", { type: "radio", name: "st-mix", value: val, ...(strudelMix === val ? { checked: "" } : {}) });
      r.addEventListener("change", () => { strudelMix = val; paint(); });
      return h("label", { class: "st-opt" }, r, " " + label);
    };
    const kids = [radio("full", "Full mix"), radio("stems", "Stems")];
    const busy = st.state === "running" || st.state === "queued";
    if (strudelMix === "stems" && st.state === "ready") {
      kids.push(h("div", { class: "st-stem-picks", role: "group", "aria-label": "Stems to play" }, STRUDEL_STEMS.map((n) => {
        const cb = h("input", { type: "checkbox", ...(strudelStems.has(n) ? { checked: "" } : {}) });
        cb.addEventListener("change", () => { if (cb.checked) strudelStems.add(n); else strudelStems.delete(n); paint(); });
        return h("label", { class: "st-chip" }, cb, " " + cap(n));
      })));
    } else if (strudelMix === "stems" && !c.stems_available && !busy) {
      kids.push(h("div", { class: "st-stem-go" }, h("span", { class: "muted st-say" }, "Stem separation needs Demucs, an optional install (about 2 GB): ",
        h("code", {}, "pip install 'tastr[stems]'"), ", then restart ", h("code", {}, "tastr serve"), ". The code uses the full mix.")));
    } else if (strudelMix === "stems") {
      const go = h("button", { type: "button", ...(busy ? { disabled: "" } : {}) },
        ...(busy ? [h("span", { class: "spin" + (st.state === "queued" ? " idle" : ""), "aria-hidden": "true" }), stemsLabel(st)] : ["Separate stems"]));
      // While it runs, a progress bar takes the explainer's place
      const say = st.state === "running" ? progBar(stemsEta(st).frac, "Stem separation progress")
        : h("span", { class: "muted st-say", role: "status" }, busy ? "The code uses the full mix until the stems are ready."
          : "Splits this track into vocals, bass, other and drums, once (a few minutes). The code uses the full mix until then.");
      go.onclick = async () => {
        go.disabled = true;
        try { await startStems(c); } catch (e) { go.disabled = false; say.className = "error st-say"; say.textContent = e.message; }
      };
      kids.push(h("div", { class: "st-stem-go" }, go, say));
      if (st.state === "error") kids.push(h("div", { class: "st-stem-go error st-say" }, st.error ? `The last separation failed: ${st.error}` : "The last separation failed."));
    }
    stemsRow.replaceChildren(...kids);
  };
  onStems(stemsRow, c.id, paint);
  // Follows the edits live; the first edit on a track switches the sketch to them.
  let hadEdits = editedKeys(allEdits(c)).length > 0;
  sectionBus.addEventListener("edits", () => {
    const has = editedKeys(allEdits(c)).length > 0;
    if (has && !hadEdits) strudelMine = true;
    hadEdits = has;
    paint();
  });
  const box = h("input", { type: "checkbox", id: "st-audio", ...(strudelAudio ? { checked: "" } : {}) });
  box.addEventListener("change", () => { strudelAudio = box.checked; paint(); });
  const copy = h("button", { type: "button", onclick: async () => {
    try { await navigator.clipboard.writeText(code); say.textContent = "Copied"; } catch (_) { say.textContent = "Couldn't copy here"; }
    setTimeout(() => (say.textContent = ""), 2500);
  } }, "Copy code");
  paint();
  return h("div", { class: "strudel" },
    h("p", { class: "st-intro" }, "A sketch of this track as ", h("a", { href: "https://strudel.cc", target: "_blank", rel: "noopener" }, "Strudel"), " live-code: each section's drums (and bass and chords, when detected) for its length, at this track's tempo. Your edits from Drums, bass & chords can go in too.",
      c.has_audio ? " Add the original audio, cut into bars, to remix the real thing." : " It won't sound like the original, but it follows its structure, so it's a starting point for remixes."),
    srcRow,
    c.has_audio ? h("label", { class: "st-opt", for: "st-audio" }, box, " Include the original audio, cut into bars") : null,
    stemsRow,
    startSel.options.length > 1 ? startRow : null,
    h("div", { class: "st-actions" }, open, copy, say), pre, note);
}

// Key names as musicians spell them (the Camelot wheel's spellings: B♭ not A#), with real ♭ and ♯ signs. Display only;
// stored values stay ASCII ("Bb major").
const KEY_SPELL = ["C", "D♭", "D", "E♭", "E", "F", "F♯", "G", "A♭", "A", "B♭", "B"];
export const accid = (s) => String(s).replace(/([A-G])b(?![a-z])/g, "$1♭").replace(/([A-G])#/g, "$1♯");
export function keyName(k) {
  return isNum(k.tonic) ? `${k.mode === "minor" && k.tonic % 12 === 1 ? "C♯" : KEY_SPELL[k.tonic % 12]} ${k.mode || ""}`.trim() : "–";
}

export function setDaw(v) { return (daw = v); }
export function setServerCfg(v) { return (serverCfg = v); }

// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  try { const v = localStorage.getItem("tastr-daw"); if (DAWS[v] && !DAWS[v].soon) daw = v; } catch (_) {}
}
