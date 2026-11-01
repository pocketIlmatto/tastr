// The Energy & sections chart (Energy, Waveform, Spectrogram and Live tabs): its controls, tabs, pointer handling and Expand.
// Drawing is in chart-draw.js, the Live canvas in chart-live.js, playback and scrubbing in chart-play.js. They share one
// state object per chart (c, made by chartState).

import { h, isNum, mmss, svg } from "./core.js";
import { charts, markHook, snapBar } from "./labels.js";
import { sectionBus } from "./card.js";
import { chartMode, setChartMode, trackAudio, trackViews } from "./audio.js";
import { B, L, R, T, draw, hideTip, showTip, specTiles } from "./chart-draw.js";
import { liveOpts, liveView } from "./chart-live.js";
import { transport } from "./chart-play.js";

function chartState(series, sections, duration, card) {
  const times = (series && series.times) || [], vals = (series && series.values) || [];
  const end = Math.max(duration || 0, times.length ? times[times.length - 1] : 0, ...sections.map((s) => s.end || 0), 1);
  const canPlay = !!card.has_audio, hasViews = canPlay || !!card.has_views;
  let mode = hasViews ? chartMode : "energy";
  if (mode === "live" && !canPlay) mode = "spec"; // Live streams the playing audio, so it needs the file
  const c = {
    card, sections, duration, end, canPlay, hasViews, mode,
    W: 700, H: 220, // grow to the real pixel size when the chart is expanded
    view: { a: 0, b: end }, minSpan: Math.min(end, 12),
    letters: [...new Set(sections.map((x) => x.letter || x.label))],
    pts: times.map((t, i) => [t, vals[i]]).filter(([t, v]) => isNum(t) && isNum(v)),
    pics: null, audio: null, selIdx: -1, cursorT: null, drag: null, savedView: null,
    MODES: [["energy", "Energy"], ["wave", "Waveform"], ["spec", "Spectrogram"], ...(canPlay ? [["live", "Live"]] : [])],
  };
  c.x = (t) => L + ((t - c.view.a) / (c.view.b - c.view.a)) * (c.W - L - R);
  c.tAt = (px) => c.view.a + ((px - L) / (c.W - L - R)) * (c.view.b - c.view.a);
  c.y = (v) => T + (1 - Math.max(0, Math.min(1, v))) * (c.H - T - B);
  c.markList = () => (markHook && markHook.cardId === card.id ? markHook.get() : null);
  c.draw = () => draw(c);
  return c;
}

function clampView(c) {
  const v = c.view, span = Math.max(c.minSpan, Math.min(c.end, v.b - v.a));
  v.a = Math.max(0, Math.min(c.end - span, v.a)); v.b = v.a + span;
}
function zoom(c, f, center) {
  const v = c.view, mid = center == null ? (v.a + v.b) / 2 : center;
  const span = Math.max(c.minSpan, Math.min(c.end, (v.b - v.a) * f));
  const r = (mid - v.a) / (v.b - v.a);
  v.a = mid - r * span; v.b = v.a + span; clampView(c); c.draw();
}

const chartKeys = () => ({
  energy: [h("span", { class: "key-line" }), " Energy: how loud and dense the track is at each moment, sampled every ~3 s. ", h("span", { class: "key-box" }), " Shaded blocks are detected sections; same color means the same part repeats."],
  wave: ["Waveform: the shape of the sound over time. Taller means louder. Faint lines are beats, darker lines are bars, numbered from the start."],
  spec: ["Spectrogram: which pitches are present over time. Low sounds at the bottom, high at the top, brighter means stronger."],
  get live() { return [liveOpts.style === "ridge"
    ? "Live 3D: each line is one moment of sound, low notes on the left and highs on the right; taller and brighter means louder. The newest line is at the front and the last 5 seconds roll back toward the top. A white line marks where a new section starts. Click the chart to play or pause."
    : "Live: the spectrogram draws itself as the track plays, with the newest sound at the right edge and the last 10 seconds scrolling left. Low at the bottom, high at the top, brighter means louder. Click the chart to play or pause."]; },
});

// The zoom bar: −, +, Reset, the visible range and a hint.
function zoomBar(c) {
  const btn = (label, title, fn) => h("button", { type: "button", title, "aria-label": title, onclick: fn }, label);
  c.zoomLabel = h("span", { class: "muted" });
  return h("div", { class: "chart-bar" }, btn("−", "Zoom out", () => zoom(c, 1.6)), btn("+", "Zoom in", () => zoom(c, 1 / 1.6)),
    btn("Reset", "Show whole track", () => { c.view.a = 0; c.view.b = c.end; c.draw(); }), c.zoomLabel,
    h("span", { class: "muted hint" }, c.canPlay
      ? "Click the chart to pick a section and play from there. Drag the playhead to scrub; you hear the track as you drag. Scroll to zoom, drag to pan"
      : "Click a section to pick it. Scroll to zoom, drag to pan"));
}

// The tab row: Energy / Waveform / Spectrogram / Live, the Live 2D/3D switch, Full screen and Expand.
function modeRow(c) {
  c.modeBtns = c.MODES.map(([k, label]) => h("button", { type: "button", role: "tab", class: "mode-tab", "aria-selected": String(k === c.mode), onclick: () => setMode(c, k) }, label));
  c.styleBtns = [["flat", "2D"], ["ridge", "3D"]].map(([k, label]) => h("button", { type: "button", class: "mode-tab ls-btn", "aria-pressed": String(liveOpts.style === k), onclick: () => setLiveStyle(c, k) }, label));
  c.styleRow = h("span", { class: "chart-side", role: "group", "aria-label": "Live view style", hidden: "" }, c.styleBtns);
  // Expand: the chart pops out over the page, bigger; full screen from there where the browser allows it
  c.expandBtn = h("button", { type: "button", class: "mode-tab exp-btn", "aria-pressed": "false", title: "Show the chart bigger (Esc to close)" }, "⤢ Expand");
  c.fsBtn = h("button", { type: "button", class: "mode-tab exp-btn", hidden: "", title: "Fill the whole screen" }, "Full screen");
  return h("div", { class: "chart-modes" }, c.hasViews ? h("span", { class: "mode-tabs", role: "tablist", "aria-label": "Visualization" }, c.modeBtns) : h("span"),
    h("span", { class: "chart-side" }, c.styleRow, c.fsBtn, c.expandBtn));
}

function transportRow(c) {
  c.playBtn = h("button", { type: "button", class: "tp-play", "aria-label": "Play" }, "▶");
  c.timeL = h("span", { class: "tp-time" }, `0:00 / ${mmss(c.duration)}`);
  c.seekR = h("input", { type: "range", class: "tp-seek", min: "0", max: String(c.end), step: "0.1", value: "0", "aria-label": "Seek" });
  return h("div", { class: "transport" }, c.playBtn, c.timeL, c.seekR);
}

// Every element of the chart, in place in c.wrap; the SVG itself stays empty until the first draw.
function buildChrome(c) {
  c.wrap = h("div", { class: "chart-wrap" });
  c.bar = zoomBar(c);
  c.tip = h("div", { class: "chart-tip", hidden: "" });
  c.host = h("div", { class: "chart-host" });
  c.keys = chartKeys();
  c.keyP = h("p", { class: "muted chart-key" });
  c.modes = modeRow(c);
  c.busy = h("div", { class: "chart-busy", hidden: "", role: "status" }, h("span", { class: "spin", "aria-hidden": "true" }), "Preparing audio preview…");
  c.transport = transportRow(c);
  c.note = h("p", { class: "muted chart-key" });
  if (!c.canPlay) c.note.textContent = c.hasViews
    ? "The original file is no longer on disk, so playback is off."
    : "The original file isn't on disk (or this track came from a link that wasn't kept), so playback and the waveform and spectrogram views are off.";
  c.say = (m) => { c.note.textContent = m; c.note.classList.toggle("error", !!m); };
  c.liveHint = h("div", { class: "live-hint", hidden: "" }, "Press ▶ to stream the spectrogram as the track plays");
  c.markBar = h("div", { class: "mark-bar", hidden: "" }); // while you mark section starts from the label panel
  c.wrap.append(c.modes, c.bar, c.markBar, h("div", { class: "chart-hostwrap" }, c.host, c.liveHint, c.busy), c.tip, ...(c.canPlay ? [c.transport] : []), c.keyP, c.note);
  c.svgEl = svg("svg", { class: "chart", viewBox: `0 0 ${c.W} ${c.H}`, role: "img", "aria-label": "Energy over time with sections" });
  c.host.append(c.svgEl);
  c.clipId = "clip" + Math.random().toString(36).slice(2, 8);
}

export function energyChart(series, sections, duration, card) {
  const c = chartState(series, sections, duration, card);
  buildChrome(c);
  sectionBus.addEventListener("section", (e) => { if (e.detail.from !== "chart") { c.selIdx = e.detail.idx; c.draw(); } });
  wirePointer(c);
  charts.set(card.id, { now: () => c.play.tNow(), draw: () => c.draw(), mode: () => c.mode, setMode: (k) => setMode(c, k), el: c.wrap, big: (on) => { if (on !== c.wrap.classList.contains("big")) c.setBig(on); } });
  c.live = liveView(c);
  c.host.append(c.live.cv);
  c.tiles = specTiles(c);
  c.ensureAudio = async () => { if (!c.audio) c.audio = await withBusy(c, () => trackAudio(card)); return c.audio; };
  c.play = transport(c);
  c.setBig = expander(c);
  setMode(c, c.mode); // a new card opens in the last-used tab, with that tab's full setup (the Live canvas included)
  return c.wrap;
}

// ---- pointer: click to pick a section and seek, drag to pan, drag the playhead to scrub, and add, move or remove marks ----
function wirePointer(c) {
  const el = c.svgEl;
  const toVB = (e) => { const r = el.getBoundingClientRect(); return { px: ((e.clientX - r.left) / r.width) * c.W }; };
  const inPlot = (px) => Math.max(L, Math.min(c.W - R, px));
  const nearMark = (px) => { const mk = c.markList(); if (!mk) return -1; let bi = -1, bd = 7; mk.forEach((t, i) => { const d = Math.abs(c.x(t) - px); if (d <= bd) { bd = d; bi = i; } }); return bi; };
  const atEdge = () => c.view.b - c.view.a < c.end - 0.01;
  const nearHead = (px) => { if (!c.canPlay) return false; const t = c.play.tNow(); return t >= c.view.a && t <= c.view.b && Math.abs(px - c.x(t)) <= 8; };
  const setHot = (on) => el.classList.toggle("head-hot", on);
  el.addEventListener("pointerdown", (e) => {
    if (c.mode === "live") { c.play.toggle(); return; } // Live: the chart is one big play/pause button
    el.setPointerCapture(e.pointerId);
    const px = toVB(e).px, mi = nearMark(px);
    if (mi >= 0) { c.drag = { mark: mi, list: c.markList(), px, moved: false }; el.style.cursor = "ew-resize"; hideTip(c); return; }
    if (nearHead(px)) { const was = c.play.isPlaying(); if (was) c.play.stop(); c.drag = { scrub: true, was, px }; setHot(true); el.style.cursor = "grabbing"; hideTip(c); c.play.scrubStart(); return; }
    c.drag = { px, a: c.view.a, b: c.view.b };
  });
  // Dragging a mark moves it (snapped to a bar line unless Alt).
  const moveMark = (e, px) => {
    const d = c.drag;
    if (Math.abs(px - d.px) < 3 && !d.moved) return;
    d.moved = true;
    const t = c.tAt(inPlot(px)); d.list[d.mark] = +(e.altKey ? t : snapBar(c.card, t)).toFixed(2); markHook.set(d.list, false); c.draw();
  };
  // Hovering: the cursor shows what a press would do; returns true when the tip should stay hidden.
  const hover = (px) => {
    if (c.markList() && nearMark(px) >= 0) { el.style.cursor = "ew-resize"; setHot(false); hideTip(c); return true; }
    const hot = nearHead(px); setHot(hot); el.style.cursor = hot || atEdge() ? "grab" : "crosshair";
    if (hot) hideTip(c);
    return hot;
  };
  const pan = (px) => {
    const d = c.drag, dt = ((px - d.px) / (c.W - L - R)) * (d.b - d.a);
    c.view.a = d.a - dt; c.view.b = d.b - dt; clampView(c);
    el.style.cursor = "grabbing"; hideTip(c); c.draw();
  };
  el.addEventListener("pointermove", (e) => {
    if (c.mode === "live") return;
    const { px } = toVB(e), d = c.drag;
    if (d && d.scrub) { c.play.scrubTo(Math.max(0, Math.min(c.end, c.tAt(inPlot(px))))); return; }
    if (d && d.mark != null) { moveMark(e, px); return; }
    if (!d && hover(px)) return;
    if (d && atEdge()) { pan(px); return; }
    c.cursorT = c.tAt(inPlot(px)); showTip(c, c.cursorT);
  });
  // A click (no move): while marking it adds a start; otherwise it seeks there and picks the section under it.
  const click = (e) => {
    const t0 = c.tAt(toVB(e).px);
    if (c.markList()) { if (c.canPlay) c.play.seek(t0); markHook.add(t0, !e.altKey); c.drag = null; return; }
    const si = c.sections.findIndex((q) => t0 >= q.start && t0 < q.end);
    if (c.canPlay) c.play.seek(t0);
    if (si >= 0) { c.selIdx = si; c.draw(); sectionBus.dispatchEvent(new CustomEvent("section", { detail: { idx: si, from: "chart" } })); }
  };
  const up = (e) => {
    if (c.mode === "live") return;
    const d = c.drag;
    if (d && d.scrub) { c.drag = null; setHot(false); el.style.cursor = "grab"; c.play.scrubEnd(); if (d.was) c.play.play(); else c.play.paintHead(); return; }
    // marking: a click without a move removes the mark it's on
    if (d && d.mark != null) { c.drag = null; if (!d.moved) d.list.splice(d.mark, 1); if (markHook) markHook.set(d.list); c.draw(); el.style.cursor = "crosshair"; return; }
    if (e && e.type === "pointerup" && d && Math.abs(toVB(e).px - d.px) < 3) { click(e); if (!c.drag) return; }
    c.drag = null; el.style.cursor = atEdge() ? "grab" : "crosshair";
  };
  el.addEventListener("pointerup", up);
  el.addEventListener("pointercancel", up);
  el.addEventListener("pointerleave", () => { if (!c.drag) { hideTip(c); setHot(false); } });
  el.addEventListener("wheel", (e) => {
    if (c.mode === "live") return;
    e.preventDefault();
    zoom(c, Math.exp(Math.sign(e.deltaY) * 0.25), c.tAt(inPlot(toVB(e).px)));
  }, { passive: false });
}

// ---- tabs ----
function setLiveStyle(c, k) {
  liveOpts.style = k;
  c.styleBtns.forEach((b, i) => b.setAttribute("aria-pressed", String(["flat", "ridge"][i] === k)));
  c.keyP.replaceChildren(...c.keys.live);
  c.live.clear(); c.draw();
}
function setMode(c, k) {
  c.mode = setChartMode(k);
  c.modeBtns.forEach((b, i) => b.setAttribute("aria-selected", String(c.MODES[i][0] === k)));
  c.keyP.replaceChildren(...c.keys[k]);
  const live = k === "live", v = c.view;
  c.bar.hidden = live; c.live.cv.hidden = !live; c.styleRow.hidden = !live; c.liveHint.hidden = !live || c.play.isPlaying();
  if (live) { if (!c.savedView) c.savedView = { a: v.a, b: v.b }; c.live.follow(c.play.tNow()); requestAnimationFrame(c.live.clear); }
  else if (c.savedView) { v.a = c.savedView.a; v.b = c.savedView.b; c.savedView = null; }
  if (k === "spec" && c.canPlay && !c.audio) trackAudio(c.card).then((a) => { c.audio = c.audio || a; if (c.mode === "spec") c.draw(); }, () => {}); // for the sharp tiles; quietly
  if (k !== "energy" && k !== "live" && !c.pics) ensurePics(c); else c.draw();
}
async function withBusy(c, work) {
  c.busy.hidden = false;
  try { return await work(); }
  catch (err) { c.say(`Couldn't load the audio: ${err.message}`); return null; }
  finally { c.busy.hidden = true; }
}
async function ensurePics(c) {
  if (!c.pics) c.pics = await withBusy(c, () => trackViews(c.card));
  c.draw();
}

// ---- Expand: the chart pops out over the page, bigger; Esc, the backdrop or Close puts it back ----
function expander(c) {
  const { wrap, expandBtn, fsBtn } = c;
  let backdrop = null;
  const onEsc = (e) => { if (e.key === "Escape" && !document.fullscreenElement) setBig(false); };
  function setBig(on) {
    wrap.classList.toggle("big", on); document.body.classList.toggle("chart-open", on);
    expandBtn.setAttribute("aria-pressed", String(on)); expandBtn.textContent = on ? "✕ Close" : "⤢ Expand";
    fsBtn.hidden = !on || !document.fullscreenEnabled;
    if (on) { backdrop = h("div", { class: "chart-backdrop", onclick: () => setBig(false) }); document.body.append(backdrop); document.addEventListener("keydown", onEsc); expandBtn.focus(); }
    else {
      backdrop?.remove(); backdrop = null; document.removeEventListener("keydown", onEsc); if (document.fullscreenElement === wrap) document.exitFullscreen();
      if (c.markList()) markHook.done(); // closing the expanded chart ends marking too
    }
    hideTip(c);
    requestAnimationFrame(fitSize);
  }
  // Expanded, the viewBox matches the space in pixels, so the chart gets taller and the text stays its normal size.
  function fitSize() {
    const big = wrap.classList.contains("big") || document.fullscreenElement === wrap;
    if (big) {
      const used = [c.modes, c.bar, c.markBar, c.transport, c.keyP, c.note].reduce((n, el) => n + (el.isConnected && !el.hidden ? el.offsetHeight + 10 : 0), 0);
      c.W = Math.max(320, Math.round(c.host.clientWidth)); c.H = Math.max(220, Math.round(wrap.clientHeight - used - 40));
    } else { c.W = 700; c.H = 220; }
    const { W, H } = c;
    c.svgEl.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const place = { left: `${(L / W) * 100}%`, top: `${(T / H) * 100}%`, width: `${((W - L - R) / W) * 100}%`, height: `${((H - T - B) / H) * 100}%` };
    Object.assign(c.live.cv.style, place); Object.assign(c.liveHint.style, place);
    if (c.mode === "live") c.live.clear();
    c.draw();
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
  return setBig;
}
