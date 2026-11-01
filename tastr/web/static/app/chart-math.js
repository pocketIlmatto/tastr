// The chart's arithmetic for the frequency-colored waveform, the section loop, cue points and the chart's keys
// (GitHub #3), with no page code so tests can run it in node (tests/test_chart_math.py).

export const WAVE_BANDS = [[255, 40, 40], [60, 230, 60], [60, 120, 255]]; // red bass, green mids, blue highs; mixes give yellow, cyan, white
export const CUE_COLORS = ["#e5484d", "#f5a524", "#30a46c", "#0091ff", "#8e4ec6", "#e93d82", "#12a594", "#ffb224", "#6e56cf"];
export const CUE_MAX = 9;

// Low, mid and high energy (sum of squares) per block of samples d, split by two one-pole low-passes at 250 Hz and
// 2.5 kHz: low = lp250, mid = lp2500 - lp250, high = x - lp2500. Block b is samples [b * blk, (b + 1) * blk), so
// blk can be fractional (to match the engine's blocks at another sample rate).
export function splitBands(d, sr, blk, nBlk = Math.ceil(d.length / blk)) {
  const lo = new Float32Array(nBlk), mid = new Float32Array(nBlk), hi = new Float32Array(nBlk);
  const k1 = 1 - Math.exp((-2 * Math.PI * 250) / sr), k2 = 1 - Math.exp((-2 * Math.PI * 2500) / sr);
  let f1 = 0, f2 = 0;
  for (let b = 0; b < nBlk; b++) {
    let eL = 0, eM = 0, eH = 0;
    for (let i = Math.floor(b * blk), e = Math.min(d.length, Math.floor((b + 1) * blk)); i < e; i++) {
      const v = d[i];
      f1 += k1 * (v - f1); f2 += k2 * (v - f2);
      eL += f1 * f1; eM += (f2 - f1) * (f2 - f1); eH += (v - f2) * (v - f2);
    }
    lo[b] = eL; mid[b] = eM; hi[b] = eH;
  }
  return [lo, mid, hi];
}

// The engine's stored bands (uint8 per block per band, interleaved, 255 * sqrt(rms / band peak)) as energies.
export function bandsFromBytes(raw) {
  const n = Math.floor(raw.length / 3), out = [new Float32Array(n), new Float32Array(n), new Float32Array(n)];
  for (let b = 0; b < n; b++) for (let k = 0; k < 3; k++) { const r = (raw[3 * b + k] / 255) ** 2; out[k][b] = r * r; }
  return out;
}

// Each band's RMS over the whole track, the yardstick for a column's color.
export const bandAvg = (bands) => bands.map((a) => { let t = 0; for (const v of a) t += v; return Math.sqrt(t / (a.length || 1)) || 1; });

// One pixel column's color from its blocks b0..b1: each band's RMS there over its whole-track RMS, squared, then
// squared again as weights to mix the three colors (so the band that leads wins clearly). Scaled so the brightest
// channel is 255; on a light page anything brighter than luma 125 is darkened to it. Rounded to steps of 15.
export function columnColor(bands, avg, b0, b1, pageDark) {
  const nb = b1 - b0 + 1, w = [0, 1, 2].map((k) => { let s = 0; for (let i = b0; i <= b1; i++) s += bands[k][i]; return (Math.sqrt(s / nb) / avg[k]) ** 2; });
  const p2 = w.map((v) => v * v), t2 = p2[0] + p2[1] + p2[2] || 1, sh = p2.map((v) => v / t2);
  const mix = [0, 1, 2].map((ch) => sh[0] * WAVE_BANDS[0][ch] + sh[1] * WAVE_BANDS[1][ch] + sh[2] * WAVE_BANDS[2][ch]), top = Math.max(...mix) || 1;
  let rgb = mix.map((v) => (v * 255) / top);
  if (!pageDark) { const luma = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]; if (luma > 125) rgb = rgb.map((v) => (v * 125) / luma); }
  return rgb.map((v) => Math.min(255, Math.round(v / 15) * 15));
}

// Is a CSS color ("rgb(18, 18, 24)" or "rgb(18 18 24)") dark? Below luma 110 counts as dark.
export function isDarkColor(css) {
  const m = /(\d+)[, ]+(\d+)[, ]+(\d+)/.exec(css || "");
  return !!m && 0.299 * m[1] + 0.587 * m[2] + 0.114 * m[3] < 110;
}

// Where the playhead is, `raw` seconds after the source started from 0, with an optional [start, end] loop.
export function loopPos(raw, loop, end) {
  return loop && raw >= loop[1] ? loop[0] + ((raw - loop[0]) % (loop[1] - loop[0])) : Math.min(end, raw);
}

// The bar length (the loop's minimum), the 1-based bar a time falls in, and snapping to the nearest beat.
export const barLen = (bars) => (bars.length > 1 ? (bars[bars.length - 1] - bars[0]) / (bars.length - 1) : 2);
export const barAt = (bars, t) => { let n = 0; for (const b of bars) { if (b <= t + 0.05) n++; else break; } return Math.max(1, n); };
export const beatSnap = (beats, t) => { let best = t, bd = Infinity; for (const b of beats) { const d = Math.abs(b - t); if (d < bd) { bd = d; best = b; } } return bd < 0.4 ? best : t; };

// Moving one edge of the loop [a, b] to time t (already snapped): it stays inside the track and at least minLen long.
export function dragLoopEdge(edge, t, a, b, minLen, end) {
  return edge === "a" ? [Math.max(0, Math.min(b - minLen, t)), b] : [a, Math.min(end, Math.max(a + minLen, t))];
}

// A new cue at t in `list` (sorted by time): {cues} with it added, or {error} to show. Named after its section.
export function addCueTo(list, t, sec) {
  if (list.length >= CUE_MAX) return { error: `You have ${CUE_MAX} cues on this track, the most there can be. Remove one first.` };
  if (list.some((q) => Math.abs(q.t - t) < 0.05)) return { error: "There's already a cue there." };
  const used = new Set(list.map((q) => q.color)), color = [...CUE_COLORS.keys()].find((k) => !used.has(k)) ?? list.length % CUE_COLORS.length;
  const name = sec ? `${sec.label.charAt(0).toUpperCase()}${sec.label.slice(1)} ${sec.letter || ""}`.trim() : `Cue ${list.length + 1}`;
  return { cues: [...list, { t, name, color }].sort((x, y) => x.t - y.t) };
}

// What a key press on the page does to the open chart: "toggle", "loop", "cue", "cue-exact", "jump" (with n) or null.
// `focus` describes the focused element: { tag, editable, role, inChart }. `marking`: section starts are being marked (M is taken, so is L).
export function chartKey(e, focus, marking) {
  if (e.ctrlKey || e.metaKey) return null;
  const { tag = "", editable = false, role = null, inChart = false } = focus || {};
  if (/^(INPUT|SELECT|TEXTAREA)$/.test(tag) || editable) return null;
  if (e.key === " ") {
    if (!inChart && (/^(BUTTON|A|SUMMARY)$/.test(tag) || role === "button" || role === "tab")) return null; // space presses those
    return e.altKey ? null : { act: "toggle" };
  }
  const k = e.altKey && e.code === "KeyC" ? "c" : (e.key || "").toLowerCase(); // Alt+C types "ç" on a Mac
  if (k === "l" && !e.altKey) return marking ? null : { act: "loop" };
  if (k === "c") return { act: e.altKey ? "cue-exact" : "cue" };
  if (/^[1-9]$/.test(e.key) && !e.altKey) return { act: "jump", n: +e.key - 1 };
  return null;
}
