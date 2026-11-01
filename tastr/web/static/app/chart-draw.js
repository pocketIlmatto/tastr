// Drawing the Energy & sections chart: the SVG for each tab, sharp spectrogram tiles, the hover tip and the marking bar.

import { SEC_COLORS, cap, h, isNum, mmss, svg } from "./core.js";
import { fmtMark, markHook } from "./labels.js";
import { beatsPerBar } from "./card.js";
import { SPEC_FMAX, SPEC_FMIN, specImage, specTopOf, txt } from "./audio.js";
import { RIDGE_FRONT, liveOpts } from "./chart-live.js";

export const L = 40, R = 8, T = 28, B = 22; // plot margins inside the viewBox (c.W x c.H)
const spanAll = (c) => c.view.b - c.view.a >= c.end - 0.01;

function drawSection(c, plot, sec, si) {
  const { W, H, view, mode, x } = c, sel = si === c.selIdx;
  if (!isNum(sec.start) || !isNum(sec.end) || sec.end < view.a || sec.start > view.b) return;
  const color = SEC_COLORS[c.letters.indexOf(sec.letter || sec.label) % SEC_COLORS.length];
  // The spectrogram has its own colors, so sections there are dashed boundaries plus a light tint on the selected one.
  plot.append(svg("rect", { x: x(sec.start), y: T, width: Math.max(1, x(sec.end) - x(sec.start)), height: H - T - B, fill: color, opacity: mode === "spec" ? (sel ? 0.28 : 0) : sel ? 0.4 : 0.18, stroke: sel ? color : "none", "stroke-width": 2 }));
  if (mode === "spec") plot.append(svg("line", { x1: x(sec.start), x2: x(sec.start), y1: T, y2: H - B, stroke: "#fff", opacity: 0.3, "stroke-dasharray": "3 3" }));
  // Label band above the grid: solid block per section, text shortens to the letter, then disappears.
  const x0 = Math.max(L, x(sec.start)), x1 = Math.min(W - R, x(sec.end)), room = x1 - x0;
  plot.append(svg("rect", { x: x0, y: 4, width: Math.max(1, room), height: 18, rx: 2, fill: color, stroke: sel ? "var(--fg)" : "var(--panel)", "stroke-width": sel ? 2 : 1 }));
  const full = [cap(sec.label), sec.letter].filter(Boolean).join(" ");
  const label = room > full.length * 6.2 + 8 ? full : room > 22 ? (sec.letter || cap(sec.label).slice(0, 3)) : "";
  if (label) plot.append(svg("text", { class: "band", x: x0 + 5, y: 16.5 }, txt(label)));
}

function drawEnergy(c, plot) {
  const { W, x, y, pts } = c;
  [0.25, 0.5, 0.75].forEach((v) => plot.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: "var(--line)", "stroke-dasharray": "2 3" })));
  if (pts.length < 2) return;
  const line = pts.map(([t, v]) => `${x(t).toFixed(1)},${y(v).toFixed(1)}`);
  plot.append(svg("polygon", { points: `${x(pts[0][0])},${y(0)} ${line.join(" ")} ${x(pts[pts.length - 1][0])},${y(0)}`, fill: "var(--accent)", opacity: 0.25 }));
  plot.append(svg("polyline", { points: line.join(" "), fill: "none", stroke: "var(--accent)", "stroke-width": 2 }));
}

// Beat grid: bar lines thinned by stride to stay >= 10 px apart, labels >= 30 px apart, beats only when >= 6 px apart.
function drawBeatGrid(c, plot) {
  const { W, H, view, x, card } = c;
  const beats = (card.core.beat_times || []).filter((t) => t >= view.a && t <= view.b), bars = card.core.downbeat_times || [];
  const pxPer = (W - L - R) / (view.b - view.a), barGap = bars.length > 1 ? (bars[1] - bars[0]) * pxPer : 0;
  if (beats.length && barGap / beatsPerBar(card) >= 6) {
    let bd = ""; for (const t of beats) bd += `M${x(t).toFixed(1)},${T}V${H - B}`;
    plot.append(svg("path", { d: bd, stroke: "var(--muted)", "stroke-width": 0.5, opacity: 0.35 }));
  }
  const inView = (t) => t >= view.a && t <= view.b;
  const stride = [1, 2, 4, 8, 16, 32].find((k) => barGap * k >= 10) || 64;
  let bd2 = "";
  bars.forEach((t, i) => { if (!(i % stride) && inView(t)) bd2 += `M${x(t).toFixed(1)},${T}V${H - B}`; });
  if (bd2) plot.append(svg("path", { d: bd2, stroke: "var(--fg)", "stroke-width": 1, opacity: 0.3 }));
  const labelEvery = [1, 2, 4, 8, 16, 32, 64].find((k) => barGap * k >= 30) || 128;
  bars.forEach((t, i) => { if (!(i % labelEvery) && inView(t)) plot.append(svg("text", { class: "barnum", x: x(t) + 3, y: T + 11 }, txt(String(i + 1)))); });
}

function drawWave(c, plot) {
  const { W, H, pics, tAt } = c;
  const mid = (T + H - B) / 2, amp = (H - T - B) / 2 - 2, cols = W - L - R;
  plot.append(svg("line", { x1: L, x2: W - R, y1: mid, y2: mid, stroke: "var(--line)" }));
  drawBeatGrid(c, plot);
  let d = "";
  for (let i = 0; i < cols; i++) {
    const t0 = tAt(L + i), t1 = tAt(L + i + 1);
    const b0 = Math.max(0, Math.floor(t0 * pics.perSec)), b1 = Math.min(pics.mn.length - 1, Math.max(b0, Math.floor(t1 * pics.perSec)));
    let lo = 1, hi = -1; for (let k = b0; k <= b1; k++) { if (pics.mn[k] < lo) lo = pics.mn[k]; if (pics.mx[k] > hi) hi = pics.mx[k]; }
    if (hi < lo) continue;
    d += `M${(L + i + 0.5).toFixed(1)},${(mid - (hi / pics.peak) * amp).toFixed(1)}V${(mid - (lo / pics.peak) * amp).toFixed(1)}`;
  }
  plot.append(svg("path", { d, stroke: "var(--accent)", "stroke-width": 1, fill: "none" }));
}

function drawSpec(c, plot) {
  const { H, pics, x } = c;
  plot.append(svg("image", { href: pics.spec, x: x(0), y: T, width: x(pics.specEnd) - x(0), height: H - T - B, preserveAspectRatio: "none" }));
  // A sharp tile for exactly what's on screen, at the screen's pixel size, goes over the stored overview once it's ready.
  if (!c.canPlay) return;
  const want = c.tiles.want(), tile = c.tiles.current();
  if (tile && tile.key === want.key) plot.append(svg("image", { href: tile.url, x: x(tile.a), y: T, width: x(tile.b) - x(tile.a), height: H - T - B, preserveAspectRatio: "none" }));
  else c.tiles.queue(want);
}

function drawPlot(c) {
  const { W, H, mode } = c;
  const plot = svg("g", { "clip-path": `url(#${c.clipId})` });
  c.sections.forEach((sec, si) => drawSection(c, plot, sec, si));
  if (mode === "energy") drawEnergy(c, plot);
  else if (mode === "live") { /* the canvas over the plot area draws the live view */ }
  else if (!c.pics) plot.append(svg("text", { x: (L + W - R) / 2, y: (T + H - B) / 2, "text-anchor": "middle", fill: "var(--muted)" }, txt("Preparing audio preview…")));
  else if (mode === "wave") drawWave(c, plot);
  else drawSpec(c, plot);
  return plot;
}

// your section starts, while marking them from the label panel
function drawMarks(c, s) {
  const { H, view, x } = c, mk = c.markList();
  paintMarkBar(c);
  if (!mk || c.mode === "live") return;
  const g = svg("g", { class: "marks", "clip-path": `url(#${c.clipId})` });
  mk.forEach((t, i) => {
    if (t < view.a || t > view.b) return;
    const xx = x(t);
    g.append(svg("line", { x1: xx, x2: xx, y1: T, y2: H - B, stroke: "var(--accent)", "stroke-width": 2, "stroke-dasharray": "5 3" }));
    g.append(svg("rect", { x: xx - 9, y: H - B - 15, width: 18, height: 15, rx: 3, fill: "var(--accent)" }));
    g.append(svg("text", { x: xx, y: H - B - 4, "text-anchor": "middle", fill: "#fff", style: "font-size:10px;font-weight:700" }, txt(String(i + 1))));
  });
  s.append(g);
}

function drawYAxis(c, s) {
  const { W, H, mode, y } = c;
  const yLabel = (text) => s.append(svg("text", { x: 10, y: (H - B + T) / 2, "text-anchor": "middle", transform: `rotate(-90 10 ${(H - B + T) / 2})` }, txt(text)));
  if (mode === "energy") {
    [0, 0.5, 1].forEach((v) => s.append(svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, txt(v))));
    yLabel("energy");
  } else if (mode === "wave") {
    yLabel("level");
  } else if (mode === "live" && liveOpts.style === "ridge") {
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
}

function drawTimeAxis(c, s) {
  const { W, H, view, mode, x } = c;
  if (mode === "live" && liveOpts.style === "ridge") return; // frequency runs along the bottom instead
  if (mode === "live") {
    [10, 8, 6, 4, 2].forEach((ago) => s.append(svg("text", { x: x(view.b - ago), y: H - 6, "text-anchor": "middle" }, txt(`−${ago} s`))));
    s.append(svg("text", { x: W - R, y: H - 6, "text-anchor": "end", fill: "var(--accent)" }, txt("now")));
    return;
  }
  const span = view.b - view.a;
  const step = [5, 10, 15, 30, 60, 120].find((st) => span / st <= 8) || 120;
  for (let t = Math.ceil(view.a / step) * step; t <= view.b; t += step) s.append(svg("text", { x: x(t), y: H - 6, "text-anchor": "middle" }, txt(mmss(t))));
}

function drawCursor(c, s) {
  const { H } = c;
  s.append(svg("line", { class: "cursor", x1: 0, x2: 0, y1: T, y2: H - B, stroke: "var(--fg)", "stroke-width": 1, opacity: 0, "pointer-events": "none" }));
  s.append(svg("circle", { class: "cursor-dot", r: 4, fill: "var(--accent)", stroke: "var(--bg)", "stroke-width": 2, opacity: 0, "pointer-events": "none" }));
  if (!c.canPlay) return;
  s.append(svg("line", { class: "playhead", y1: T - 4, y2: H - B, stroke: "var(--fg)", "stroke-width": 2, "pointer-events": "none" }));
  s.append(svg("path", { class: "playhead-knob", d: "M-6,0 L6,0 L0,8 Z", fill: "var(--fg)", "pointer-events": "none" }));
  c.play.paintHead();
}

// Redraw the whole chart for its current tab, view and size.
export function draw(c) {
  const s = c.svgEl, { W, H, view } = c;
  s.replaceChildren();
  s.append(svg("defs", {}, svg("clipPath", { id: c.clipId }, svg("rect", { x: L, y: 0, width: W - L - R, height: H }))));
  s.append(drawPlot(c));
  drawMarks(c, s);
  drawYAxis(c, s);
  drawTimeAxis(c, s);
  drawCursor(c, s);
  c.zoomLabel.textContent = spanAll(c) ? "Whole track" : `${mmss(view.a)}–${mmss(view.b)}`;
  if (c.mode === "live") s.style.cursor = "pointer";
  else if (!c.drag) s.style.cursor = spanAll(c) ? "crosshair" : "grab";
  if (c.cursorT != null) showTip(c, c.cursorT);
}

// The energy curve's value at time t, interpolated between samples.
function energyAt(pts, t) {
  if (!pts.length) return null;
  let lo = 0, hi = pts.length - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (pts[m][0] <= t) lo = m; else hi = m; }
  const [t0, v0] = pts[lo], [t1, v1] = pts[hi];
  return t1 === t0 ? v0 : v0 + ((Math.max(t0, Math.min(t1, t)) - t0) / (t1 - t0)) * (v1 - v0);
}

const energyWord = (v) => (v > .75 ? " (loud, full)" : v > .45 ? " (medium)" : " (quiet, sparse)");

export function showTip(c, t) {
  const { svgEl, tip, wrap, W, view, x } = c;
  const cur = svgEl.querySelector(".cursor"), dot = svgEl.querySelector(".cursor-dot");
  if (!cur) return;
  if (t < view.a || t > view.b) { hideTip(c); return; }
  const v = c.mode === "energy" ? energyAt(c.pts, t) : null, sec = c.sections.find((q) => t >= q.start && t < q.end);
  cur.setAttribute("x1", x(t)); cur.setAttribute("x2", x(t)); cur.setAttribute("opacity", 0.5);
  if (v != null) { dot.setAttribute("cx", x(t)); dot.setAttribute("cy", c.y(v)); dot.setAttribute("opacity", 1); }
  tip.replaceChildren(
    h("b", {}, mmss(t)),
    v != null ? h("div", {}, `Energy ${v.toFixed(2)}`, h("span", { class: "muted" }, energyWord(v))) : null,
    sec ? h("div", { class: "muted" }, `${cap(sec.label)} ${sec.letter || ""} · bars ${sec.start_bar}–${sec.end_bar}`.replace("  ", " ")) : null);
  tip.hidden = false;
  const r = svgEl.getBoundingClientRect(), hr = wrap.getBoundingClientRect();
  const left = r.left - hr.left + (x(t) / W) * r.width;
  tip.style.left = Math.min(Math.max(left + 12, 0), hr.width - tip.offsetWidth) + "px";
  tip.style.top = (r.top - hr.top + 8) + "px";
}

export function hideTip(c) {
  c.cursorT = null; c.tip.hidden = true;
  for (const q of [".cursor", ".cursor-dot"]) { const el = c.svgEl.querySelector(q); if (el) el.setAttribute("opacity", 0); }
}

// Shown while you mark section starts from the label panel (mirrors its controls, so marking works in the expanded view too).
function paintMarkBar(c) {
  const mk = c.markList(), { markBar, bar } = c;
  markBar.hidden = !mk; bar.lastChild.hidden = !!mk; if (!mk) return;
  markBar.replaceChildren(h("b", {}, "Marking section starts"),
    h("span", { class: "muted" }, mk.length ? `${mk.length}: ${mk.map(fmtMark).join(" ")}` : "none yet"),
    c.canPlay ? h("button", { type: "button", title: "Add a start where the playhead is (or press M)", onclick: () => markHook.add(c.play.tNow(), true) }, "＋ At playhead (M)") : null,
    mk.length ? h("button", { type: "button", title: "Remove the last start you added", onclick: () => markHook.undo() }, "↶ Undo") : null,
    h("button", { type: "button", class: "primary", onclick: () => markHook.done() }, "✓ Done marking"),
    h("span", { class: "muted hint" }, "Click to add (snaps to bars, Alt = exact), drag a mark to move it, click a mark to remove it."));
}

// ---- sharp spectrogram tiles: 140 ms after the view settles, render exactly the visible range ----
export function specTiles(c) {
  let tile = null, timer = 0, busy = null;
  function want() {
    const { W, H, view, x } = c;
    const px = ((c.host.clientWidth || W) * (window.devicePixelRatio || 1)) / W, a = Math.max(0, view.a), b = Math.min(c.end, view.b);
    const cols = Math.max(50, Math.min(2400, Math.round((x(b) - x(a)) * px))), rows = Math.max(80, Math.min(600, Math.round((H - T - B) * px)));
    return { a, b, cols, rows, key: `${a.toFixed(2)}|${b.toFixed(2)}|${cols}|${rows}` };
  }
  function queue(w) {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const stale = () => !c.audio || c.mode !== "spec" || !c.wrap.isConnected || want().key !== w.key;
      if (stale() || busy === w.key) return;
      busy = w.key;
      try {
        const url = await specImage(c.audio.buf, w.a, w.b, w.cols, w.rows, specTopOf(c.audio), stale);
        if (url) { tile = { ...w, url }; c.draw(); }
      } finally { if (busy === w.key) busy = null; }
    }, 140);
  }
  return { want, queue, current: () => tile };
}
