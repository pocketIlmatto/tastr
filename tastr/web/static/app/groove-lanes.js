// Drum lanes as rows of steps: the shared grid for the groove view, the glossary and the bass rhythm row.

import { cap, h } from "./core.js";

// The engine sends what each measured lane means (data.lane_help); this covers cards analyzed before it did.
const LANE_HELP = {
  kick: "Kick: low hits whose energy sits below about 120 Hz.",
  snare: "Snare: noisy mid hits (400 Hz to 5 kHz), usually with some body around 200 Hz.",
  hat: "Hi-hat: short hits above 6 kHz.",
  cymbal: "Cymbal: crash and ride hits, found by the optional drum model.",
};
export const laneName = (lane) => lane.replace(/_/g, " ");
const laneHelp = (lane, measured, help) => {
  if (!measured) return "Part of a template pattern, not measured from the audio.";
  const text = help && help[lane];
  return text ? `${cap(laneName(lane))}: ${text.charAt(0).toLowerCase()}${text.slice(1)}` : LANE_HELP[lane] || "Measured from the audio.";
};

// One lane per instrument; the steps (16 in 4/4, 12 in 3/4) sit in tinted beat groups of 4, with beat numbers above the first lane.
// `orig` is what the user's edits are measured against (the detected pattern or the picked template): steps that differ are "mine".
// `laneCtl` ({ quiet(lane), el(lane) }) adds solo/mute buttons after each lane name and fades lanes that won't play.
const STEP_NAMES = { x: "strong", o: "softer", ".": "empty" };
export function laneRows(pattern, prev, measured, onToggle, help, orig, laneCtl) {
  const rows = [];
  let nBeats = 4;
  for (const [lane, pat] of Object.entries(pattern)) {
    if (typeof pat !== "string") continue;
    nBeats = pat.length === 12 ? 3 : 4;
    const cells = [...pat.slice(0, nBeats * 4)].map((ch, i) => {
      const changed = prev && prev[lane] && prev[lane][i] !== ch;
      const was = orig ? (orig[lane] || "")[i] || "." : ch, mine = was !== ch;
      return h("i", { class: [ch === "x" ? "full" : ch === "o" ? "half" : "", changed ? "chg" : "", mine ? "mine" : ""].join(" ").trim(),
        title: mine ? `Your edit (was ${STEP_NAMES[was] || "empty"})` : null,
        "data-step": i, "data-lane": lane, tabindex: onToggle ? "0" : null, role: onToggle ? "button" : null,
        "aria-label": onToggle ? `${laneName(lane)}, step ${i + 1}: ${STEP_NAMES[ch] || "empty"}. Press to change.` : null,
        onclick: onToggle ? () => onToggle(lane, i) : null,
        onkeydown: onToggle ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onToggle(lane, i); } } : null });
    });
    const beats = [...Array(nBeats).keys()].map((b) => h("div", { class: "beat-group" }, cells.slice(b * 4, b * 4 + 4)));
    rows.push(h("div", { class: "drum-lane" + (laneCtl && laneCtl.quiet(lane) ? " quiet" : "") }, h("span", { class: "name", tabindex: "0", "data-tip": laneHelp(lane, measured, help) }, laneName(lane)),
      laneCtl ? laneCtl.el(lane) : null, h("div", { class: "drum-grid" }, beats)));
  }
  if (rows.length) rows.unshift(h("div", { class: "drum-lane beat-nums", "aria-hidden": "true" }, h("span", { class: "name" }), laneCtl ? h("span", { class: "lane-sm" }) : null, h("div", { class: "drum-grid" }, [...Array(nBeats).keys()].map((n) => h("span", {}, n + 1)))));
  return rows;
}

// Edits per card, part ("drums", "bass", "chords", "drumsrc" = picked template) and section idx ("all" without sections).
