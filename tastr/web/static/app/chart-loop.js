// The chart's section loop and cue points (GitHub #3): the ⟲ Loop and ＋ Cue buttons, the line under the transport
// that says what's looping, the row of cue chips, and the keys (Space, L, C, 1-9) for the open track's chart.
// Cues are saved per person per track (/api/tracks/{id}/cues); the loop is not saved.

import { api, cap, currentId, h, mmss } from "./core.js";
import { charts, markHook } from "./labels.js";
import { sectionBus } from "./card.js";
import { CUE_COLORS, addCueTo, barAt, barLen, beatSnap, chartKey } from "./chart-math.js";

// The loop and cue parts of one chart (c is the chart's state, see chart.js): adds c.loopBtn, c.cueBtn, c.loopSay,
// c.cueRow and c.keysHint, and c.toggleLoop, c.addCue, c.jumpCue, c.loopToSection, c.paintLoopSay.
export function loopAndCues(c) {
  const { card, sections } = c;
  const bars = () => card.core.downbeat_times || [];
  c.loopBtn = h("button", { type: "button", class: "tp-btn tp-loop", "aria-pressed": "false", title: "Loop the picked section (L). Drag the loop's edges on the chart to change it. Click another section while looping to move the loop there." }, "⟲ Loop");
  c.cueBtn = h("button", { type: "button", class: "tp-btn", title: "Add a cue point at the playhead (C). It snaps to the nearest beat; hold Alt for the exact spot." }, "＋ Cue");
  c.loopSay = h("p", { class: "tp-loopsay", hidden: "" });
  c.cueRow = h("div", { class: "cue-row", "aria-label": "Cue points" });
  c.keysHint = h("p", { class: "muted tp-keys" }, h("kbd", {}, "Space"), " play/pause · ", h("kbd", {}, "L"), " loop · ", h("kbd", {}, "C"), " cue · ", h("kbd", {}, "1"), "–", h("kbd", {}, "9"), " jump to a cue");
  c.loopMin = () => barLen(bars()); // a loop is at least one bar long

  // ---- the loop ----
  c.paintLoopSay = () => {
    const { on, a, b } = c.loop;
    c.loopSay.hidden = !on; if (!on) return;
    const q = sections.find((s) => Math.abs(s.start - a) < 0.05 && Math.abs(s.end - b) < 0.05), b0 = barAt(bars(), a), b1 = Math.max(b0, barAt(bars(), b - 0.1));
    c.loopSay.replaceChildren(h("b", {}, "⟲ Looping "), q ? `${cap(q.label)} ${q.letter || ""} · `.replace("  ", " ") : "", `bars ${b0}–${b1} (${b1 - b0 + 1} bar${b1 > b0 ? "s" : ""}, ${mmss(b - a)}). `,
      h("span", { class: "muted" }, "Drag its edges to change it; they snap to bars (hold Alt for the exact spot). Click a section to loop that instead. "),
      h("button", { type: "button", class: "linklike", onclick: () => c.toggleLoop() }, "Stop looping"));
  };
  const paintLoop = () => { c.loopBtn.setAttribute("aria-pressed", String(c.loop.on)); c.paintLoopSay(); c.draw(); };
  c.toggleLoop = () => {
    if (!sections.length) return;
    const t = c.play.tNow();
    c.loop.on = !c.loop.on;
    if (c.loop.on) {
      // the picked section, or the one under the playhead (which then gets picked)
      const si = c.selIdx >= 0 ? c.selIdx : Math.max(0, sections.findIndex((q) => t >= q.start && t < q.end));
      c.loop.a = sections[si].start; c.loop.b = sections[si].end;
      if (c.selIdx < 0) { c.selIdx = si; sectionBus.dispatchEvent(new CustomEvent("section", { detail: { idx: si, from: "chart" } })); }
    }
    c.play.restart();
    paintLoop();
  };
  // Clicking a section while looping loops that whole section instead.
  c.loopToSection = (si) => { if (!c.loop.on || si < 0) return; c.loop.a = sections[si].start; c.loop.b = sections[si].end; paintLoop(); };
  c.loopBtn.onclick = () => c.toggleLoop();

  // ---- cue points ----
  let cueMsg = "", cueTimer = 0, renaming = -1;
  const save = () => {
    const body = c.cues.map(({ t, name, color }) => ({ t, name, color }));
    api(`/api/tracks/${card.id}/cues`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .catch((e) => say(`Couldn't save the cue points: ${e.message}`));
  };
  const say = (m) => { cueMsg = m; paint(); clearTimeout(cueTimer); cueTimer = setTimeout(() => { cueMsg = ""; paint(); }, 3000); };
  const changed = () => { save(); paint(); c.draw(); };
  c.addCue = (exact) => {
    const now = c.play.tNow(), t = +(exact ? now : beatSnap(card.core.beat_times || [], now)).toFixed(2);
    const r = addCueTo(c.cues, t, sections.find((q) => t >= q.start && t < q.end));
    if (r.error) { say(r.error); return; }
    c.cues = r.cues; changed();
  };
  c.jumpCue = (i) => {
    const q = c.cues[i]; if (!q) return;
    if (c.loop.on && !(q.t >= c.loop.a && q.t < c.loop.b)) { c.loop.on = false; c.play.restart(); paintLoop(); } // a cue outside the loop ends it
    c.play.seek(q.t); paint(i);
  };
  function chip(q, i) {
    const col = CUE_COLORS[q.color % CUE_COLORS.length];
    if (i === renaming) {
      const inp = h("input", { type: "text", class: "cue-name-in", value: q.name, maxlength: "30", "aria-label": `Name for cue ${i + 1}` });
      let over = false;
      const done = (keep) => { if (over) return; over = true; if (keep) q.name = inp.value.trim().slice(0, 30) || q.name; renaming = -1; if (keep) changed(); else paint(); };
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); done(true); } if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done(false); } });
      inp.addEventListener("blur", () => done(true));
      requestAnimationFrame(() => { inp.focus(); inp.select(); });
      return h("span", { class: "cue-chip editing", style: `--c:${col}` }, h("b", { class: "cue-n" }, String(i + 1)), inp);
    }
    return h("span", { class: "cue-chip", style: `--c:${col}` },
      h("button", { type: "button", class: "cue-go", title: `Jump to ${q.name} (${i + 1})`, onclick: () => c.jumpCue(i) }, h("b", { class: "cue-n" }, String(i + 1)), ` ${q.name} `, h("span", { class: "muted" }, mmss(q.t))),
      h("button", { type: "button", class: "cue-x", title: `Rename ${q.name}`, "aria-label": `Rename cue ${i + 1}`, onclick: () => { renaming = i; paint(); } }, "✎"),
      h("button", { type: "button", class: "cue-x", title: `Remove ${q.name}`, "aria-label": `Remove cue ${i + 1}`, onclick: () => { c.cues.splice(i, 1); changed(); } }, "×"));
  }
  function paint(flash = -1) {
    c.cueRow.replaceChildren(...c.cues.map(chip),
      ...(c.cues.length ? [] : [h("span", { class: "muted cue-empty" }, "No cue points yet. Press ＋ Cue (or C) while it plays to mark a spot to jump back to.")]),
      ...(cueMsg ? [h("span", { class: "cue-msg", role: "status" }, cueMsg)] : []));
    if (flash >= 0) { const el = c.cueRow.children[flash]; if (el) { el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); } }
  }
  c.cueBtn.onclick = (e) => c.addCue(e.altKey);
  paint();
  if (c.canPlay) api(`/api/tracks/${card.id}/cues`).then((list) => { if (Array.isArray(list) && !c.cues.length) { c.cues = list; paint(); c.draw(); } }, () => {});
}

// Space plays or pauses the open track's chart; L loops, C adds a cue, 1-9 jump to cues. Only while that chart is on
// screen, not while typing, and Space not when focus is on a button, link or tab outside the chart (it presses those).
export function initChartKeys() {
  document.addEventListener("keydown", (e) => {
    if (e.defaultPrevented) return;
    const ch = charts.get(currentId);
    if (!ch || !ch.keys || !ch.el.isConnected || !ch.el.offsetParent) return;
    const a = document.activeElement;
    const act = chartKey(e, { tag: a?.tagName || "", editable: !!a?.isContentEditable, role: a?.getAttribute?.("role") || null, inChart: !!a && ch.el.contains(a) }, !!markHook);
    if (!act) return;
    e.preventDefault();
    ch.keys(act);
  });
}
