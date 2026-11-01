// The Energy & sections chart (Energy, Waveform, Spectrogram and Live tabs).

import { SEC_COLORS, cap, h, isNum, mmss, svg } from "./core.js";
import { charts, fmtMark, markHook, snapBar } from "./labels.js";
import { beatsPerBar, sectionBus } from "./card.js";
import { SPEC_FMAX, SPEC_FMIN, SPEC_RAMP, chartMode, setChartMode, specImage, specTopOf, trackAudio, trackViews, txt } from "./audio.js";
import { actx, audioCtx, player, setPlayer, stopAllPlayback } from "./sequencer.js";

let liveStyle = "flat"; // Live tab: "flat" scrolling spectrogram or "ridge" 3D landscape, remembered across cards for the session
export function energyChart(series, sections, duration, card) {
  let W = 700, H = 220; const L = 40, R = 8, T = 28, B = 22; // W/H grow to the real pixel size when the chart is expanded
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
  const MODES = [["energy", "Energy"], ["wave", "Waveform"], ["spec", "Spectrogram"], ...(canPlay ? [["live", "Live"]] : [])];
  let mode = hasViews ? chartMode : "energy", pics = null;
  if (mode === "live" && !canPlay) mode = "spec"; // Live streams the playing audio, so it needs the file
  const KEYS = {
    energy: [h("span", { class: "key-line" }), " Energy: how loud and dense the track is at each moment, sampled every ~3 s. ", h("span", { class: "key-box" }), " Shaded blocks are detected sections; same color means the same part repeats."],
    wave: ["Waveform: the shape of the sound over time. Taller means louder. Faint lines are beats, darker lines are bars, numbered from the start."],
    spec: ["Spectrogram: which pitches are present over time. Low sounds at the bottom, high at the top, brighter means stronger."],
    get live() { return [liveStyle === "ridge"
      ? "Live 3D: each line is one moment of sound, low notes on the left and highs on the right; taller and brighter means louder. The newest line is at the front and the last 5 seconds roll back toward the top. A white line marks where a new section starts. Click the chart to play or pause."
      : "Live: the spectrogram draws itself as the track plays, with the newest sound at the right edge and the last 10 seconds scrolling left. Low at the bottom, high at the top, brighter means louder. Click the chart to play or pause."]; },
  };
  const keyP = h("p", { class: "muted chart-key" });
  const modeBtns = MODES.map(([k, label]) => h("button", { type: "button", role: "tab", class: "mode-tab", "aria-selected": String(k === mode), onclick: () => setMode(k) }, label));
  const styleBtns = [["flat", "2D"], ["ridge", "3D"]].map(([k, label]) => h("button", { type: "button", class: "mode-tab ls-btn", "aria-pressed": String(liveStyle === k), onclick: () => setLiveStyle(k) }, label));
  const styleRow = h("span", { class: "chart-side", role: "group", "aria-label": "Live view style", hidden: "" }, styleBtns);
  // Expand: the chart pops out over the page, bigger; full screen from there where the browser allows it
  const expandBtn = h("button", { type: "button", class: "mode-tab exp-btn", "aria-pressed": "false", title: "Show the chart bigger (Esc to close)" }, "⤢ Expand");
  const fsBtn = h("button", { type: "button", class: "mode-tab exp-btn", hidden: "", title: "Fill the whole screen" }, "Full screen");
  const modes = h("div", { class: "chart-modes" }, hasViews ? h("span", { class: "mode-tabs", role: "tablist", "aria-label": "Visualization" }, modeBtns) : h("span"),
    h("span", { class: "chart-side" }, styleRow, fsBtn, expandBtn));
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
  const liveHint = h("div", { class: "live-hint", hidden: "" }, "Press ▶ to stream the spectrogram as the track plays");
  let savedView = null;
  // shown while you mark section starts from the label panel (mirrors its controls, so marking works in the expanded view too)
  const markBar = h("div", { class: "mark-bar", hidden: "" });
  function paintMarkBar() {
    const mk = markList(); markBar.hidden = !mk; bar.lastChild.hidden = !!mk; if (!mk) return;
    markBar.replaceChildren(h("b", {}, "Marking section starts"),
      h("span", { class: "muted" }, mk.length ? `${mk.length}: ${mk.map(fmtMark).join(" ")}` : "none yet"),
      canPlay ? h("button", { type: "button", title: "Add a start where the playhead is (or press M)", onclick: () => markHook.add(tNow(), true) }, "＋ At playhead (M)") : null,
      mk.length ? h("button", { type: "button", title: "Remove the last start you added", onclick: () => markHook.undo() }, "↶ Undo") : null,
      h("button", { type: "button", class: "primary", onclick: () => markHook.done() }, "✓ Done marking"),
      h("span", { class: "muted hint" }, "Click to add (snaps to bars, Alt = exact), drag a mark to move it, click a mark to remove it."));
  }
  wrap.append(modes, bar, markBar, h("div", { class: "chart-hostwrap" }, host, liveHint, busy), tip, ...(canPlay ? [transport] : []), keyP, note);

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
    } else if (mode === "live") {
      // the canvas over the plot area draws the live view
    } else if (!pics) {
      plot.append(svg("text", { x: (L + W - R) / 2, y: (T + H - B) / 2, "text-anchor": "middle", fill: "var(--muted)" }, txt("Preparing audio preview…")));
    } else if (mode === "wave") {
      const mid = (T + H - B) / 2, amp = (H - T - B) / 2 - 2, cols = W - L - R;
      plot.append(svg("line", { x1: L, x2: W - R, y1: mid, y2: mid, stroke: "var(--line)" }));
      // Beat grid: bar lines thinned by stride to stay >= 10 px apart, labels >= 30 px apart, beats only when >= 6 px apart.
      const beats = (card.core.beat_times || []).filter((t) => t >= view.a && t <= view.b), bars = card.core.downbeat_times || [];
      const pxPer = (W - L - R) / (view.b - view.a), barGap = bars.length > 1 ? (bars[1] - bars[0]) * pxPer : 0;
      if (beats.length && barGap / beatsPerBar(card) >= 6) {
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
      // A sharp tile for exactly what's on screen, at the screen's pixel size, goes over the stored overview once it's ready.
      if (canPlay) {
        const want = specWant();
        if (specTile && specTile.key === want.key) plot.append(svg("image", { href: specTile.url, x: x(specTile.a), y: T, width: x(specTile.b) - x(specTile.a), height: H - T - B, preserveAspectRatio: "none" }));
        else queueSpecTile(want);
      }
    }
    s.append(plot);
    // your section starts, while marking them from the label panel
    const mk = markList();
    paintMarkBar();
    if (mk && mode !== "live") {
      const g = svg("g", { class: "marks", "clip-path": `url(#${clipId})` });
      mk.forEach((t, i) => {
        if (t < view.a || t > view.b) return;
        const xx = x(t);
        g.append(svg("line", { x1: xx, x2: xx, y1: T, y2: H - B, stroke: "var(--accent)", "stroke-width": 2, "stroke-dasharray": "5 3" }));
        g.append(svg("rect", { x: xx - 9, y: H - B - 15, width: 18, height: 15, rx: 3, fill: "var(--accent)" }));
        g.append(svg("text", { x: xx, y: H - B - 4, "text-anchor": "middle", fill: "#fff", style: "font-size:10px;font-weight:700" }, txt(String(i + 1))));
      });
      s.append(g);
    }
    const yLabel = (text) => s.append(svg("text", { x: 10, y: (H - B + T) / 2, "text-anchor": "middle", transform: `rotate(-90 10 ${(H - B + T) / 2})` }, txt(text)));
    if (mode === "energy") {
      [0, 0.5, 1].forEach((v) => s.append(svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, txt(v))));
      yLabel("energy");
    } else if (mode === "wave") {
      yLabel("level");
    } else if (mode === "live" && liveStyle === "ridge") {
      // frequency runs along the bottom of the 3D view, under the front line
      const fx = (f) => L + (W - L - R) * (RIDGE_FRONT[0] + (RIDGE_FRONT[1] - RIDGE_FRONT[0]) * Math.log(f / SPEC_FMIN) / Math.log(SPEC_FMAX / SPEC_FMIN));
      [[100, "100 Hz"], [1000, "1k"], [10000, "10k"]].forEach(([f, label]) => s.append(svg("text", { x: fx(f), y: H - 6, "text-anchor": "middle" }, txt(label))));
    } else {
      [[100, "100"], [1000, "1k"], [10000, "10k"]].forEach(([f, label]) => {
        const yy = T + (1 - Math.log(f / SPEC_FMIN) / Math.log(SPEC_FMAX / SPEC_FMIN)) * (H - T - B);
        s.append(svg("text", { x: L - 6, y: yy + 4, "text-anchor": "end" }, txt(label)));
      });
      yLabel("Hz");
    }
    const span = view.b - view.a;
    const step = [5, 10, 15, 30, 60, 120].find((st) => span / st <= 8) || 120;
    if (mode === "live" && liveStyle === "ridge") { /* frequency runs along the bottom instead */ }
    else if (mode === "live") [10, 8, 6, 4, 2].forEach((ago) => s.append(svg("text", { x: x(view.b - ago), y: H - 6, "text-anchor": "middle" }, txt(`−${ago} s`))));
    else for (let t = Math.ceil(view.a / step) * step; t <= view.b; t += step) s.append(svg("text", { x: x(t), y: H - 6, "text-anchor": "middle" }, txt(mmss(t))));
    if (mode === "live" && liveStyle !== "ridge") s.append(svg("text", { x: W - R, y: H - 6, "text-anchor": "end", fill: "var(--accent)" }, txt("now")));
    s.append(svg("line", { class: "cursor", x1: 0, x2: 0, y1: T, y2: H - B, stroke: "var(--fg)", "stroke-width": 1, opacity: 0, "pointer-events": "none" }));
    s.append(svg("circle", { class: "cursor-dot", r: 4, fill: "var(--accent)", stroke: "var(--bg)", "stroke-width": 2, opacity: 0, "pointer-events": "none" }));
    if (canPlay) {
      s.append(svg("line", { class: "playhead", y1: T - 4, y2: H - B, stroke: "var(--fg)", "stroke-width": 2, "pointer-events": "none" }));
      s.append(svg("path", { class: "playhead-knob", d: "M-6,0 L6,0 L0,8 Z", fill: "var(--fg)", "pointer-events": "none" }));
      paintHead();
    }
    zoomLabel.textContent = view.b - view.a >= end - 0.01 ? "Whole track" : `${mmss(view.a)}–${mmss(view.b)}`;
    if (mode === "live") s.style.cursor = "pointer";
    else if (!drag) s.style.cursor = view.b - view.a < end - 0.01 ? "grab" : "crosshair";
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
  const markList = () => (markHook && markHook.cardId === card.id ? markHook.get() : null);
  const nearMark = (px) => { const mk = markList(); if (!mk) return -1; let bi = -1, bd = 7; mk.forEach((t, i) => { const d = Math.abs(x(t) - px); if (d <= bd) { bd = d; bi = i; } }); return bi; };
  const atEdge = () => view.b - view.a < end - 0.01;
  const nearHead = (px) => { if (!canPlay) return false; const t = tNow(); return t >= view.a && t <= view.b && Math.abs(px - x(t)) <= 8; };
  const setHot = (on) => svgEl.classList.toggle("head-hot", on);
  svgEl.addEventListener("pointerdown", (e) => {
    if (mode === "live") { if (playing) me.stop(); else play(); return; } // Live: the chart is one big play/pause button
    svgEl.setPointerCapture(e.pointerId);
    const px = toVB(e).px;
    const mi = nearMark(px);
    if (mi >= 0) { drag = { mark: mi, list: markList(), px, moved: false }; svgEl.style.cursor = "ew-resize"; hideTip(); return; }
    if (nearHead(px)) { const was = playing; if (was) me.stop(); drag = { scrub: true, was, px }; setHot(true); svgEl.style.cursor = "grabbing"; hideTip(); scrubStart(); return; }
    drag = { px, a: view.a, b: view.b };
  });
  svgEl.addEventListener("pointermove", (e) => {
    if (mode === "live") return;
    const { px } = toVB(e);
    if (drag && drag.scrub) { if (scrub) scrub.target = Math.max(0, Math.min(end, tAt(Math.max(L, Math.min(W - R, px))))); return; }
    if (drag && drag.mark != null) {
      if (Math.abs(px - drag.px) < 3 && !drag.moved) return;
      drag.moved = true;
      const t = tAt(Math.max(L, Math.min(W - R, px))); drag.list[drag.mark] = +(e.altKey ? t : snapBar(card, t)).toFixed(2); markHook.set(drag.list, false); draw(); return;
    }
    if (!drag && markList() && nearMark(px) >= 0) { svgEl.style.cursor = "ew-resize"; setHot(false); hideTip(); return; }
    if (!drag) { const hot = nearHead(px); setHot(hot); svgEl.style.cursor = hot || atEdge() ? "grab" : "crosshair"; if (hot) { hideTip(); return; } }
    if (drag && atEdge()) {
      const dt = ((px - drag.px) / (W - L - R)) * (drag.b - drag.a);
      view.a = drag.a - dt; view.b = drag.b - dt; clampView();
      svgEl.style.cursor = "grabbing"; hideTip(); draw(); return;
    }
    cursorT = tAt(Math.max(L, Math.min(W - R, px))); showTip(cursorT);
  });
  const up = (e) => {
    if (mode === "live") return;
    if (drag && drag.scrub) { const was = drag.was; drag = null; setHot(false); svgEl.style.cursor = "grab"; scrubEnd(); if (was) play(); else paintHead(); return; }
    // marking: a click without a move removes the mark it's on; a click elsewhere adds one (snapped to a bar line unless Alt)
    if (drag && drag.mark != null) { const d = drag; drag = null; if (!d.moved) d.list.splice(d.mark, 1); if (markHook) markHook.set(d.list); draw(); svgEl.style.cursor = "crosshair"; return; }
    if (e && e.type === "pointerup" && drag && Math.abs(toVB(e).px - drag.px) < 3 && markList()) {
      const t0 = tAt(toVB(e).px); if (canPlay) seek(t0); markHook.add(t0, !e.altKey); drag = null; return;
    }
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
    if (mode === "live") return;
    e.preventDefault();
    zoom(Math.exp(Math.sign(e.deltaY) * 0.25), tAt(Math.max(L, Math.min(W - R, toVB(e).px))));
  }, { passive: false });

  charts.set(card.id, { now: () => tNow(), draw: () => draw(), mode: () => mode, setMode: (k) => setMode(k), el: wrap, big: (on) => { if (on !== wrap.classList.contains("big")) setBig(on); } });

  // ---- Live: an AnalyserNode on the playing audio drives a canvas over the plot area ----
  const LIVE_SPAN = 10; // seconds of history across the 2D view
  const liveCv = h("canvas", { class: "live-spec", hidden: "", "aria-hidden": "true" });
  host.append(liveCv);
  // Three analysers in a chain on the way to the speakers: a short FFT (crisp timing up top) and a long one (fine bass detail),
  // crossfaded between 150 and 900 Hz for the 2D view, and a middle one for the 3D view. Sources connect to the first.
  let analyser = null, aHi = null, aLo = null, freq = null, fHi = null, fLo = null, rowMap = null, prevCol = null, liveT = null;
  const LIVE_DB = [-100, -28]; // dB range of the 2D colors
  function getAnalyser() {
    if (!analyser) {
      const ctx = audioCtx(), mk = (n, sm) => { const a = ctx.createAnalyser(); a.fftSize = n; a.smoothingTimeConstant = sm; a.minDecibels = -95; a.maxDecibels = -25; return a; };
      aHi = mk(2048, 0.05); aLo = mk(16384, 0.1); analyser = mk(8192, 0.15);
      aHi.connect(aLo); aLo.connect(analyser); analyser.connect(ctx.destination);
      freq = new Uint8Array(analyser.frequencyBinCount); fHi = new Float32Array(aHi.frequencyBinCount); fLo = new Float32Array(aLo.frequencyBinCount);
    }
    return aHi;
  }
  // Per canvas row: where its frequency band falls in each analyser's bins, and how much of the short FFT to use.
  function liveRows(hh) {
    const sr = audioCtx().sampleRate;
    if (rowMap && rowMap.hh === hh && rowMap.sr === sr) return rowMap;
    const rows = [];
    for (let r = 0; r < hh; r++) {
      const f0 = SPEC_FMIN * Math.pow(SPEC_FMAX / SPEC_FMIN, (hh - 1 - r) / hh), f1 = SPEC_FMIN * Math.pow(SPEC_FMAX / SPEC_FMIN, (hh - r) / hh), fc = Math.sqrt(f0 * f1);
      const wHi = Math.max(0, Math.min(1, Math.log(fc / 150) / Math.log(900 / 150)));
      rows.push({ wHi, lo: [f0 / (sr / aLo.fftSize), f1 / (sr / aLo.fftSize)], hi: [f0 / (sr / aHi.fftSize), f1 / (sr / aHi.fftSize)] });
    }
    return (rowMap = { hh, sr, rows });
  }
  // dB for a row: narrower than a bin, interpolated between bins (no stair steps); wider, its loudest bin.
  function binDb(buf, [p0, p1]) {
    if (p1 - p0 < 1) { const p = (p0 + p1) / 2, a = Math.min(buf.length - 2, Math.floor(p)), q = p - a; return buf[a] * (1 - q) + buf[a + 1] * q; }
    let m = -Infinity; for (let k = Math.floor(p0), e = Math.min(buf.length, Math.ceil(p1)); k < e; k++) if (buf[k] > m) m = buf[k];
    return m;
  }
  function liveClear() {
    const r = liveCv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    const w = Math.max(100, Math.round(r.width * dpr)), hh = Math.max(50, Math.round(r.height * dpr));
    if (liveCv.width !== w || liveCv.height !== hh) { liveCv.width = w; liveCv.height = hh; }
    const g = liveCv.getContext("2d"); g.fillStyle = "rgb(8 8 24)"; g.fillRect(0, 0, w, hh);
    liveT = null; ridges = []; ridgeT = null; prevCol = null;
  }
  function setLiveStyle(k) {
    liveStyle = k;
    styleBtns.forEach((b, i) => b.setAttribute("aria-pressed", String(["flat", "ridge"][i] === k)));
    keyP.replaceChildren(...KEYS.live);
    liveClear(); draw();
  }
  // The strongest analyser bin between two frequencies, 0..255.
  const binMax = (f0, f1, binHz) => {
    const a = Math.floor(f0 / binHz), b = Math.max(a + 1, Math.ceil(f1 / binHz));
    let m = 0; for (let k = a; k < b && k < freq.length; k++) if (freq[k] > m) m = freq[k];
    return m;
  };
  // 3D ridgeline: each line is one moment of sound (low notes left, highs right); older lines step back and shrink toward the horizon.
  const RIDGE_BINS = 120, RIDGE_LINES = 48, RIDGE_DT = 0.11; // ~5 s of history
  const RIDGE_FRONT = [0.04, 0.96]; // the front line spans this share of the plot width
  let ridges = [], ridgeT = null;
  function ridgeSample() {
    getAnalyser(); analyser.getByteFrequencyData(freq);
    const binHz = audioCtx().sampleRate / analyser.fftSize, v = new Float32Array(RIDGE_BINS);
    for (let i = 0; i < RIDGE_BINS; i++)
      v[i] = Math.pow(binMax(SPEC_FMIN * Math.pow(SPEC_FMAX / SPEC_FMIN, i / RIDGE_BINS), SPEC_FMIN * Math.pow(SPEC_FMAX / SPEC_FMIN, (i + 1) / RIDGE_BINS), binHz) / 255, 1.6);
    return v;
  }
  function ridgeFrame(t) {
    if (ridgeT == null || t < ridgeT - 0.05 || t - ridgeT > 1) { liveClear(); ridgeT = t - RIDGE_DT; }
    if (t - ridgeT < RIDGE_DT) return;
    while (t - ridgeT >= RIDGE_DT) ridgeT += RIDGE_DT;
    const sec = sections.some((q) => q.start > t - RIDGE_DT && q.start <= t);
    ridges.unshift({ v: ridgeSample(), mark: sec }); if (ridges.length > RIDGE_LINES) ridges.pop();
    const g = liveCv.getContext("2d"), w = liveCv.width, hh = liveCv.height, dpr = w / Math.max(1, liveCv.getBoundingClientRect().width);
    g.fillStyle = "rgb(8 8 24)"; g.fillRect(0, 0, w, hh);
    const frontY = hh * 0.97, horizonY = hh * 0.06, cx = w / 2, fw = w * (RIDGE_FRONT[1] - RIDGE_FRONT[0]);
    for (let a = ridges.length - 1; a >= 0; a--) { // back to front, so each filled line hides the ridges behind it
      const z = a / RIDGE_LINES, sc = 1 / (1 + z * 2.2), y0 = horizonY + (frontY - horizonY) * sc, lw = fw * (0.55 + 0.45 * sc), amp = hh * 0.62 * sc;
      const x0 = cx - lw / 2, step = lw / (RIDGE_BINS - 1), r = ridges[a].v;
      g.beginPath(); g.moveTo(x0, y0);
      for (let i = 0; i < RIDGE_BINS; i++) g.lineTo(x0 + i * step, y0 - r[i] * amp);
      g.lineTo(x0 + lw, y0); g.closePath();
      const grad = g.createLinearGradient(0, y0, 0, y0 - amp);
      SPEC_RAMP.forEach((c, i) => grad.addColorStop(i / (SPEC_RAMP.length - 1), `rgb(${c[0]} ${c[1]} ${c[2]})`));
      g.globalAlpha = 0.35 + 0.65 * sc; g.fillStyle = grad; g.fill();
      g.lineWidth = Math.max(1, 1.2 * dpr * sc); g.strokeStyle = ridges[a].mark ? "rgba(255,255,255,.9)" : "rgba(255,235,160,.55)"; g.stroke();
    }
    g.globalAlpha = 1; g.font = `${11 * dpr}px system-ui, sans-serif`; g.fillStyle = "rgba(255,255,255,.7)";
    g.textAlign = "left"; g.fillText("now", 6 * dpr, frontY - 6 * dpr); g.fillText(`−${Math.round(RIDGE_LINES * RIDGE_DT)} s`, cx - fw * 0.3, horizonY + 10 * dpr);
  }
  // 2D: scroll left by the time since the last frame and paint the new columns at the right edge.
  function liveFrame(t) {
    if (liveStyle === "ridge") { ridgeFrame(t); return; }
    const g = liveCv.getContext("2d"), w = liveCv.width, hh = liveCv.height, pxs = w / LIVE_SPAN;
    if (liveT == null || t < liveT || t - liveT > 1) { liveClear(); liveT = t; } // a seek or a jump starts over
    const dx = Math.round((t - liveT) * pxs);
    if (dx < 1) return;
    liveT += dx / pxs;
    g.drawImage(liveCv, -dx, 0);
    getAnalyser(); aHi.getFloatFrequencyData(fHi); aLo.getFloatFrequencyData(fLo);
    const { rows } = liveRows(hh), cur = new Float32Array(hh);
    for (let r = 0; r < hh; r++) {
      const m = rows[r], v = m.wHi >= 1 ? binDb(fHi, m.hi) : m.wHi <= 0 ? binDb(fLo, m.lo) : binDb(fHi, m.hi) * m.wHi + binDb(fLo, m.lo) * (1 - m.wHi);
      cur[r] = Math.max(0, Math.min(1, (v - LIVE_DB[0]) / (LIVE_DB[1] - LIVE_DB[0]))) || 0;
    }
    // The new strip blends from the last column to this one, so frames don't show as blocks.
    const prev = prevCol && prevCol.length === hh ? prevCol : cur, img = g.createImageData(dx, hh), Rp = SPEC_RAMP, n = Rp.length - 1;
    for (let i = 0; i < dx; i++) {
      const wt = (i + 1) / dx;
      for (let r = 0; r < hh; r++) {
        const u = (prev[r] + (cur[r] - prev[r]) * wt) * n, i0 = Math.min(n - 1, Math.floor(u)), q = u - i0, o = (r * dx + i) * 4;
        img.data[o] = Rp[i0][0] + (Rp[i0 + 1][0] - Rp[i0][0]) * q; img.data[o + 1] = Rp[i0][1] + (Rp[i0 + 1][1] - Rp[i0][1]) * q;
        img.data[o + 2] = Rp[i0][2] + (Rp[i0 + 1][2] - Rp[i0][2]) * q; img.data[o + 3] = 255;
      }
    }
    g.putImageData(img, w - dx, 0); prevCol = cur;
    // a faint dashed line where a section starts, so section changes scroll by with the sound
    if (sections.some((q) => q.start > t - dx / pxs && q.start <= t)) { g.fillStyle = "rgba(255,255,255,.35)"; for (let yy = 0; yy < hh; yy += 6) g.fillRect(w - 1, yy, 1, 3); }
  }
  function liveView(t) { view.a = t - LIVE_SPAN; view.b = t; }

  // ---- sharp spectrogram tiles: 140 ms after the view settles, render exactly the visible range ----
  let specTile = null, specTimer = 0, specBusy = null;
  function specWant() {
    const px = ((host.clientWidth || W) * (window.devicePixelRatio || 1)) / W, a = Math.max(0, view.a), b = Math.min(end, view.b);
    const cols = Math.max(50, Math.min(2400, Math.round((x(b) - x(a)) * px))), rows = Math.max(80, Math.min(600, Math.round((H - T - B) * px)));
    return { a, b, cols, rows, key: `${a.toFixed(2)}|${b.toFixed(2)}|${cols}|${rows}` };
  }
  function queueSpecTile(want) {
    clearTimeout(specTimer);
    specTimer = setTimeout(async () => {
      const stale = () => !audio || mode !== "spec" || !wrap.isConnected || specWant().key !== want.key;
      if (stale() || specBusy === want.key) return;
      specBusy = want.key;
      try {
        const url = await specImage(audio.buf, want.a, want.b, want.cols, want.rows, specTopOf(audio), stale);
        if (url) { specTile = { ...want, url }; draw(); }
      } finally { if (specBusy === want.key) specBusy = null; }
    }, 140);
  }

  // ---- mode switching ----
  function setMode(k) {
    mode = setChartMode(k);
    modeBtns.forEach((b, i) => b.setAttribute("aria-selected", String(MODES[i][0] === k)));
    keyP.replaceChildren(...KEYS[k]);
    const live = k === "live";
    bar.hidden = live; liveCv.hidden = !live; styleRow.hidden = !live; liveHint.hidden = !live || playing;
    if (live) { if (!savedView) savedView = { a: view.a, b: view.b }; liveView(tNow()); requestAnimationFrame(liveClear); }
    else if (savedView) { view.a = savedView.a; view.b = savedView.b; savedView = null; }
    if (k === "spec" && canPlay && !audio) trackAudio(card).then((a) => { audio = audio || a; if (mode === "spec") draw(); }, () => {}); // for the sharp tiles; quietly
    if (k !== "energy" && k !== "live" && !pics) ensurePics(); else draw();
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
    src = ctx.createBufferSource(); src.buffer = audio.buf; src.connect(getAnalyser()); // the analyser passes the sound on to the speakers
    src.onended = () => { if (playing) { me.stop(); pos = 0; paintHead(); } };
    src.start(0, pos); startedAt = ctx.currentTime - pos;
  }
  // Registered as the shared `player`, so the drum sequencer and track playback stop each other.
  const me = { stop() {
    if (!playing) return;
    pos = tNow(); playing = false; cancelAnimationFrame(raf);
    if (src) { src.onended = null; try { src.stop(); } catch (_) {} src = null; }
    if (player === me) setPlayer(null);
    playBtn.textContent = "▶"; playBtn.setAttribute("aria-label", "Play"); paintHead();
  } };
  function loop() {
    if (!playing) return;
    if (!svgEl.isConnected) { me.stop(); return; }
    const t = tNow();
    // When zoomed in and the playhead leaves the view, page so it sits 10% from the left. Live keeps "now" at the right edge.
    if (mode === "live") { liveFrame(t); liveView(t); draw(); }
    else if (t > view.b || t < view.a) { const span = view.b - view.a; view.a = Math.max(0, Math.min(end - span, t - span * 0.1)); view.b = view.a + span; draw(); }
    else paintHead();
    raf = requestAnimationFrame(loop);
  }
  async function play() {
    if (!(await ensureAudio()) || !svgEl.isConnected) return;
    stopAllPlayback();
    if (pos >= end - 0.2) pos = 0;
    startSrc(); playing = true; setPlayer(me); liveHint.hidden = true;
    playBtn.textContent = "❚❚"; playBtn.setAttribute("aria-label", "Pause"); loop();
  }
  playBtn.onclick = () => { if (playing) me.stop(); else play(); };
  function seek(t) {
    pos = Math.max(0, Math.min(end, t));
    if (playing) startSrc();
    if (mode === "live" && !playing) { liveView(pos); draw(); }
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

  // ---- Expand: the chart pops out over the page, bigger; Esc, the backdrop or Close puts it back ----
  let backdrop = null;
  const onEsc = (e) => { if (e.key === "Escape" && !document.fullscreenElement) setBig(false); };
  function setBig(on) {
    wrap.classList.toggle("big", on); document.body.classList.toggle("chart-open", on);
    expandBtn.setAttribute("aria-pressed", String(on)); expandBtn.textContent = on ? "✕ Close" : "⤢ Expand";
    fsBtn.hidden = !on || !document.fullscreenEnabled;
    if (on) { backdrop = h("div", { class: "chart-backdrop", onclick: () => setBig(false) }); document.body.append(backdrop); document.addEventListener("keydown", onEsc); expandBtn.focus(); }
    else {
      backdrop?.remove(); backdrop = null; document.removeEventListener("keydown", onEsc); if (document.fullscreenElement === wrap) document.exitFullscreen();
      if (markList()) markHook.done(); // closing the expanded chart ends marking too
    }
    hideTip();
    requestAnimationFrame(fitSize);
  }
  // Expanded, the viewBox matches the space in pixels, so the chart gets taller and the text stays its normal size.
  function fitSize() {
    const big = wrap.classList.contains("big") || document.fullscreenElement === wrap;
    if (big) {
      const used = [modes, bar, markBar, transport, keyP, note].reduce((n, el) => n + (el.isConnected && !el.hidden ? el.offsetHeight + 10 : 0), 0);
      W = Math.max(320, Math.round(host.clientWidth)); H = Math.max(220, Math.round(wrap.clientHeight - used - 40));
    } else { W = 700; H = 220; }
    svgEl.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const place = { left: `${(L / W) * 100}%`, top: `${(T / H) * 100}%`, width: `${((W - L - R) / W) * 100}%`, height: `${((H - T - B) / H) * 100}%` };
    Object.assign(liveCv.style, place); Object.assign(liveHint.style, place);
    if (mode === "live") liveClear();
    draw();
  }
  const onResize = () => {
    if (!wrap.isConnected) { window.removeEventListener("resize", onResize); document.removeEventListener("fullscreenchange", onFs); return; }
    if (wrap.classList.contains("big") || document.fullscreenElement === wrap) fitSize();
  };
  const onFs = () => {
    if (!wrap.isConnected) { onResize(); return; }
    fsBtn.textContent = document.fullscreenElement === wrap ? "Exit full screen" : "Full screen"; requestAnimationFrame(fitSize);
  };
  window.addEventListener("resize", onResize);
  document.addEventListener("fullscreenchange", onFs);
  expandBtn.onclick = () => setBig(!wrap.classList.contains("big"));
  fsBtn.onclick = () => { if (document.fullscreenElement) document.exitFullscreen(); else wrap.requestFullscreen?.().catch(() => { fsBtn.hidden = true; }); };

  setMode(mode); // a new card opens in the last-used tab, with that tab's full setup (the Live canvas included)
  return wrap;
}
