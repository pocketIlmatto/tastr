// The chart's Live tab: analysers on the playing audio drive a canvas over the plot area, as a scrolling 2D spectrogram or a 3D ridgeline.

import { h } from "./core.js";
import { SPEC_FMAX, SPEC_FMIN, SPEC_RAMP } from "./audio.js";
import { audioCtx } from "./sequencer.js";

export const liveOpts = { style: "flat" }; // "flat" scrolling spectrogram or "ridge" 3D landscape, remembered across cards for the session
export const RIDGE_FRONT = [0.04, 0.96]; // the front line spans this share of the plot width
const LIVE_SPAN = 10; // seconds of history across the 2D view
const LIVE_DB = [-100, -28]; // dB range of the 2D colors
// 3D ridgeline: each line is one moment of sound (low notes left, highs right); older lines step back and shrink toward the horizon.
const RIDGE_BINS = 120, RIDGE_LINES = 48, RIDGE_DT = 0.11; // ~5 s of history
const BG = "rgb(8 8 24)";

// dB for a row: narrower than a bin, interpolated between bins (no stair steps); wider, its loudest bin.
function binDb(buf, [p0, p1]) {
  if (p1 - p0 < 1) { const p = (p0 + p1) / 2, a = Math.min(buf.length - 2, Math.floor(p)), q = p - a; return buf[a] * (1 - q) + buf[a + 1] * q; }
  let m = -Infinity; for (let k = Math.floor(p0), e = Math.min(buf.length, Math.ceil(p1)); k < e; k++) if (buf[k] > m) m = buf[k];
  return m;
}

// The strongest bin between two frequencies, 0..255.
function binMax(freq, f0, f1, binHz) {
  const a = Math.floor(f0 / binHz), b = Math.max(a + 1, Math.ceil(f1 / binHz));
  let m = 0; for (let k = a; k < b && k < freq.length; k++) if (freq[k] > m) m = freq[k];
  return m;
}

// The new 2D strip, dx columns wide, blending from the last column to this one, so frames don't show as blocks.
function blendStrip(g, prev, cur, dx, hh) {
  const img = g.createImageData(dx, hh), Rp = SPEC_RAMP, n = Rp.length - 1;
  for (let i = 0; i < dx; i++) {
    const wt = (i + 1) / dx;
    for (let r = 0; r < hh; r++) {
      const u = (prev[r] + (cur[r] - prev[r]) * wt) * n, i0 = Math.min(n - 1, Math.floor(u)), q = u - i0, o = (r * dx + i) * 4;
      img.data[o] = Rp[i0][0] + (Rp[i0 + 1][0] - Rp[i0][0]) * q; img.data[o + 1] = Rp[i0][1] + (Rp[i0 + 1][1] - Rp[i0][1]) * q;
      img.data[o + 2] = Rp[i0][2] + (Rp[i0 + 1][2] - Rp[i0][2]) * q; img.data[o + 3] = 255;
    }
  }
  return img;
}

// Paint the ridgelines back to front, so each filled line hides the ridges behind it.
function paintRidges(g, ridges, w, hh, dpr) {
  g.fillStyle = BG; g.fillRect(0, 0, w, hh);
  const frontY = hh * 0.97, horizonY = hh * 0.06, cx = w / 2, fw = w * (RIDGE_FRONT[1] - RIDGE_FRONT[0]);
  for (let a = ridges.length - 1; a >= 0; a--) {
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

// The Live canvas for one chart (c is the chart's state, see chart.js).
export function liveView(c) {
  const cv = h("canvas", { class: "live-spec", hidden: "", "aria-hidden": "true" });
  // Three analysers in a chain on the way to the speakers: a short FFT (crisp timing up top) and a long one (fine bass detail),
  // crossfaded between 150 and 900 Hz for the 2D view, and a middle one for the 3D view. Sources connect to the first.
  let analyser = null, aHi = null, aLo = null, freq = null, fHi = null, fLo = null, rowMap = null, prevCol = null, liveT = null;
  let ridges = [], ridgeT = null;
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
  function clear() {
    const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    const w = Math.max(100, Math.round(r.width * dpr)), hh = Math.max(50, Math.round(r.height * dpr));
    if (cv.width !== w || cv.height !== hh) { cv.width = w; cv.height = hh; }
    const g = cv.getContext("2d"); g.fillStyle = BG; g.fillRect(0, 0, w, hh);
    liveT = null; ridges = []; ridgeT = null; prevCol = null;
  }
  function ridgeSample() {
    getAnalyser(); analyser.getByteFrequencyData(freq);
    const binHz = audioCtx().sampleRate / analyser.fftSize, v = new Float32Array(RIDGE_BINS);
    for (let i = 0; i < RIDGE_BINS; i++)
      v[i] = Math.pow(binMax(freq, SPEC_FMIN * Math.pow(SPEC_FMAX / SPEC_FMIN, i / RIDGE_BINS), SPEC_FMIN * Math.pow(SPEC_FMAX / SPEC_FMIN, (i + 1) / RIDGE_BINS), binHz) / 255, 1.6);
    return v;
  }
  function ridgeFrame(t) {
    if (ridgeT == null || t < ridgeT - 0.05 || t - ridgeT > 1) { clear(); ridgeT = t - RIDGE_DT; }
    if (t - ridgeT < RIDGE_DT) return;
    while (t - ridgeT >= RIDGE_DT) ridgeT += RIDGE_DT;
    const sec = c.sections.some((q) => q.start > t - RIDGE_DT && q.start <= t);
    ridges.unshift({ v: ridgeSample(), mark: sec }); if (ridges.length > RIDGE_LINES) ridges.pop();
    paintRidges(cv.getContext("2d"), ridges, cv.width, cv.height, cv.width / Math.max(1, cv.getBoundingClientRect().width));
  }
  // This moment's level per canvas row, 0..1.
  function liveColumn(hh) {
    getAnalyser(); aHi.getFloatFrequencyData(fHi); aLo.getFloatFrequencyData(fLo);
    const { rows } = liveRows(hh), cur = new Float32Array(hh);
    for (let r = 0; r < hh; r++) {
      const m = rows[r], v = m.wHi >= 1 ? binDb(fHi, m.hi) : m.wHi <= 0 ? binDb(fLo, m.lo) : binDb(fHi, m.hi) * m.wHi + binDb(fLo, m.lo) * (1 - m.wHi);
      cur[r] = Math.max(0, Math.min(1, (v - LIVE_DB[0]) / (LIVE_DB[1] - LIVE_DB[0]))) || 0;
    }
    return cur;
  }
  // 2D: scroll left by the time since the last frame and paint the new columns at the right edge.
  function frame(t) {
    if (liveOpts.style === "ridge") { ridgeFrame(t); return; }
    const g = cv.getContext("2d"), w = cv.width, hh = cv.height, pxs = w / LIVE_SPAN;
    if (liveT == null || t < liveT || t - liveT > 1) { clear(); liveT = t; } // a seek or a jump starts over
    const dx = Math.round((t - liveT) * pxs);
    if (dx < 1) return;
    liveT += dx / pxs;
    g.drawImage(cv, -dx, 0);
    const cur = liveColumn(hh), prev = prevCol && prevCol.length === hh ? prevCol : cur;
    g.putImageData(blendStrip(g, prev, cur, dx, hh), w - dx, 0); prevCol = cur;
    // a faint dashed line where a section starts, so section changes scroll by with the sound
    if (c.sections.some((q) => q.start > t - dx / pxs && q.start <= t)) { g.fillStyle = "rgba(255,255,255,.35)"; for (let yy = 0; yy < hh; yy += 6) g.fillRect(w - 1, yy, 1, 3); }
  }
  const follow = (t) => { c.view.a = t - LIVE_SPAN; c.view.b = t; };
  return { cv, clear, frame, follow, analyser: getAnalyser };
}
