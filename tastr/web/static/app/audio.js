// Track audio: the original file for playback and scrubbing, and waveform and spectrogram picture data.

import { api } from "./core.js";
import { audioCtx } from "./sequencer.js";

export const SPEC_ROWS = 120, SPEC_HOP = 0.1, SPEC_FMIN = 40, SPEC_FMAX = 10000;
export const SPEC_RAMP = [[8, 8, 24], [60, 20, 120], [170, 40, 120], [240, 120, 40], [255, 235, 160]];
export let chartMode = "energy"; // Energy | Waveform | Spectrogram, remembered across cards for the session
export let audioCache = null, viewsCache = null;
export const txt = (s) => document.createTextNode(s);
export const delay = (ms) => new Promise((r) => setTimeout(r, ms));
// The decoded original file (plus a mono reversed copy, made on first backwards scrub). Fetched on first need, not on card open.
export function trackAudio(c) {
  if (!audioCache || audioCache.id !== c.id) {
    const promise = fetch(`/api/tracks/${c.id}/audio`)
      .then((r) => { if (!r.ok) throw new Error(r.status === 404 ? "The original file is not on disk." : r.statusText); return r.arrayBuffer(); })
      .then((bytes) => new Promise((ok, fail) => audioCtx().decodeAudioData(bytes, ok, () => fail(new Error("This browser can't decode the file's format.")))))
      .then((buf) => ({ buf, rev: null }));
    audioCache = { id: c.id, promise };
    promise.catch(() => { if (audioCache && audioCache.promise === promise) audioCache = null; });
  }
  return audioCache.promise;
}
// Gray levels (0..252, row 0 = top/highest band) -> a dark-to-yellow image as a data URL.
function rampImage(level, frames, rows) {
  const cv = document.createElement("canvas"); cv.width = frames; cv.height = rows;
  const g = cv.getContext("2d"), img = g.createImageData(frames, rows), n = SPEC_RAMP.length - 1;
  for (let i = 0; i < frames * rows; i++) {
    const u = Math.max(0, Math.min(1, level[i] / 252)) * n, i0 = Math.min(n - 1, Math.floor(u)), q = u - i0;
    for (let ch = 0; ch < 3; ch++) img.data[i * 4 + ch] = SPEC_RAMP[i0][ch] + (SPEC_RAMP[i0 + 1][ch] - SPEC_RAMP[i0][ch]) * q;
    img.data[i * 4 + 3] = 255;
  }
  g.putImageData(img, 0, 0);
  return cv.toDataURL("image/png");
}
function loadImage(src) {
  return new Promise((ok, fail) => { const im = new Image(); im.onload = () => ok(im); im.onerror = () => fail(new Error("bad spectrogram image")); im.src = src; });
}
// Stored by the engine's `views` stage: int8 min/max per block, and a grayscale log-frequency PNG to color here.
async function viewsFromCard(c) {
  const v = await api(`/api/tracks/${c.id}/views`);
  const bin = atob(v.waveform.data), raw = new Int8Array(bin.length);
  for (let i = 0; i < bin.length; i++) raw[i] = (bin.charCodeAt(i) << 24) >> 24;
  const n = raw.length >> 1, mn = new Float32Array(n), mx = new Float32Array(n);
  for (let b = 0; b < n; b++) { mn[b] = raw[2 * b] / 127; mx[b] = raw[2 * b + 1] / 127; }
  const s = v.spectrogram, im = await loadImage(s.png);
  const cv = document.createElement("canvas"); cv.width = im.width; cv.height = im.height;
  const g = cv.getContext("2d"); g.drawImage(im, 0, 0);
  const px = g.getImageData(0, 0, im.width, im.height).data, level = new Uint8Array(im.width * im.height);
  for (let i = 0; i < level.length; i++) level[i] = px[i * 4];
  return { mn, mx, peak: 1, perSec: v.waveform.sr / v.waveform.block, spec: rampImage(level, im.width, im.height), specEnd: s.frames * s.hop };
}
// Cards analyzed before the `views` stage existed: compute both from the decoded file in the browser.
async function viewsFromAudio(c) {
  const { buf } = await trackAudio(c);
  const sr = buf.sampleRate, chans = [...Array(buf.numberOfChannels)].map((_, k) => buf.getChannelData(k));
  const d = new Float32Array(buf.length);
  for (const ch of chans) for (let i = 0; i < d.length; i++) d[i] += ch[i] / chans.length;
  await delay(0);
  const BLK = Math.round(sr / 86), nBlk = Math.ceil(d.length / BLK), mn = new Float32Array(nBlk), mx = new Float32Array(nBlk);
  let peak = 1e-6;
  for (let b = 0; b < nBlk; b++) {
    let lo = 1, hi = -1;
    for (let i = b * BLK, e = Math.min(d.length, i + BLK); i < e; i++) { const v = d[i]; if (v < lo) lo = v; if (v > hi) hi = v; }
    mn[b] = lo; mx[b] = hi; peak = Math.max(peak, -lo, hi);
  }
  await delay(0);
  const s = spectrogramLevels(d, sr);
  return { mn, mx, peak, perSec: sr / BLK, spec: rampImage(s.level, s.frames, SPEC_ROWS), specEnd: s.frames * SPEC_HOP };
}
export function trackViews(c) {
  if (!viewsCache || viewsCache.id !== c.id) {
    const promise = c.has_views ? viewsFromCard(c) : viewsFromAudio(c);
    viewsCache = { id: c.id, promise };
    promise.catch(() => { if (viewsCache && viewsCache.promise === promise) viewsCache = null; });
  }
  return viewsCache.promise;
}
// Log-frequency magnitude (dB) per 0.1 s, same layout as the engine's: 0..252 over the top 70 dB, row 0 = top.
function spectrogramLevels(d, sr) {
  const N = sr > 32000 ? 4096 : 2048, bits = Math.log2(N), hop = Math.round(SPEC_HOP * sr), frames = Math.max(1, Math.floor((d.length - N) / hop));
  const win = new Float32Array(N), cosT = new Float32Array(N / 2), sinT = new Float32Array(N / 2), rev = new Uint16Array(N);
  for (let i = 0; i < N; i++) { win[i] = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (N - 1)); let r = 0; for (let b = 0; b < bits; b++) if (i & (1 << b)) r |= 1 << (bits - 1 - b); rev[i] = r; }
  for (let i = 0; i < N / 2; i++) { cosT[i] = Math.cos((2 * Math.PI * i) / N); sinT[i] = -Math.sin((2 * Math.PI * i) / N); }
  const re = new Float32Array(N), im = new Float32Array(N);
  const rowBin = [...Array(SPEC_ROWS + 1)].map((_, r) => (SPEC_FMIN * Math.pow(SPEC_FMAX / SPEC_FMIN, r / SPEC_ROWS)) / (sr / N));
  const mag = new Float32Array(N / 2), db = new Float32Array(frames * SPEC_ROWS);
  let top = -1e9;
  for (let f = 0; f < frames; f++) {
    for (let i = 0; i < N; i++) { const v = f * hop + i < d.length ? d[f * hop + i] : 0; re[rev[i]] = v * win[i]; im[rev[i]] = 0; }
    for (let size = 2; size <= N; size <<= 1) {
      const half = size >> 1, stepT = N / size;
      for (let i = 0; i < N; i += size) for (let j = 0, k = 0; j < half; j++, k += stepT) {
        const a = i + j, b = a + half, tr = re[b] * cosT[k] - im[b] * sinT[k], ti = re[b] * sinT[k] + im[b] * cosT[k];
        re[b] = re[a] - tr; im[b] = im[a] - ti; re[a] += tr; im[a] += ti;
      }
    }
    for (let i = 0; i < N / 2; i++) mag[i] = Math.hypot(re[i], im[i]);
    for (let r = 0; r < SPEC_ROWS; r++) {
      const a = Math.ceil(rowBin[r]), b = Math.max(a + 1, Math.ceil(rowBin[r + 1]));
      let m = 0; for (let k = a; k < b && k < N / 2; k++) m = Math.max(m, mag[k]);
      const v = 20 * Math.log10(m + 1e-6); db[f * SPEC_ROWS + r] = v; if (v > top) top = v;
    }
  }
  const level = new Uint8Array(frames * SPEC_ROWS);
  for (let f = 0; f < frames; f++) for (let r = 0; r < SPEC_ROWS; r++)
    level[(SPEC_ROWS - 1 - r) * frames + f] = Math.round(Math.max(0, Math.min(1, (db[f * SPEC_ROWS + r] - (top - 70)) / 70)) * 252);
  return { level, frames };
}

// Sharp spectrogram tiles, computed in the browser from the decoded file for exactly the range on screen at the screen's pixel
// size. Low rows narrower than an FFT bin are interpolated between bins; high rows that span several bins take the strongest.
// Colors are scaled to `top` dB (the whole track's loudest), so a zoomed-in tile keeps the overview's brightness.
const specFfts = {};
function specSetup(N) {
  if (specFfts[N]) return specFfts[N];
  const bits = Math.log2(N), win = new Float32Array(N), cosT = new Float32Array(N / 2), sinT = new Float32Array(N / 2), rev = new Uint16Array(N);
  for (let i = 0; i < N; i++) { win[i] = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (N - 1)); let r = 0; for (let b = 0; b < bits; b++) if (i & (1 << b)) r |= 1 << (bits - 1 - b); rev[i] = r; }
  for (let i = 0; i < N / 2; i++) { cosT[i] = Math.cos((2 * Math.PI * i) / N); sinT[i] = -Math.sin((2 * Math.PI * i) / N); }
  return (specFfts[N] = { win, cosT, sinT, rev, re: new Float32Array(N), im: new Float32Array(N), mag: new Float32Array(N / 2) });
}
// dB per column (t0..t1) and log-frequency row (SPEC_FMIN..SPEC_FMAX) of an AudioBuffer, mixed to mono on the fly.
// `part` ({ from, to, db }) fills only those columns of an existing result, so a big tile can be built a slice at a time.
function specDb(buf, t0, t1, cols, rows, part) {
  const sr = buf.sampleRate, N = sr > 32000 ? 4096 : 2048, { win, cosT, sinT, rev, re, im, mag } = specSetup(N), binHz = sr / N;
  const chans = [...Array(buf.numberOfChannels)].map((_, k) => buf.getChannelData(k)), nc = chans.length, len = buf.length;
  const lo = new Float32Array(rows), hi = new Float32Array(rows);
  for (let r = 0; r < rows; r++) { lo[r] = (SPEC_FMIN * Math.pow(SPEC_FMAX / SPEC_FMIN, r / rows)) / binHz; hi[r] = (SPEC_FMIN * Math.pow(SPEC_FMAX / SPEC_FMIN, (r + 1) / rows)) / binHz; }
  const db = part ? part.db : new Float32Array(cols * rows); let top = -1e9;
  for (let f = part ? part.from : 0, fEnd = part ? part.to : cols; f < fEnd; f++) {
    const c = Math.round((t0 + ((f + 0.5) / cols) * (t1 - t0)) * sr) - N / 2;
    for (let i = 0; i < N; i++) {
      const k = c + i; let v = 0;
      if (k >= 0 && k < len) { for (let ch = 0; ch < nc; ch++) v += chans[ch][k]; v /= nc; }
      re[rev[i]] = v * win[i]; im[rev[i]] = 0;
    }
    for (let size = 2; size <= N; size <<= 1) {
      const half = size >> 1, stepT = N / size;
      for (let i = 0; i < N; i += size) for (let j = 0, k = 0; j < half; j++, k += stepT) {
        const a = i + j, b = a + half, tr = re[b] * cosT[k] - im[b] * sinT[k], ti = re[b] * sinT[k] + im[b] * cosT[k];
        re[b] = re[a] - tr; im[b] = im[a] - ti; re[a] += tr; im[a] += ti;
      }
    }
    for (let i = 0; i < N / 2; i++) mag[i] = Math.hypot(re[i], im[i]);
    for (let r = 0; r < rows; r++) {
      let m;
      if (hi[r] - lo[r] < 1) { const p = (lo[r] + hi[r]) / 2, a = Math.floor(p), q = p - a; m = mag[a] * (1 - q) + mag[Math.min(N / 2 - 1, a + 1)] * q; }
      else { m = 0; for (let k = Math.floor(lo[r]); k < Math.ceil(hi[r]) && k < N / 2; k++) if (mag[k] > m) m = mag[k]; }
      const v = 20 * Math.log10(m + 1e-6); db[f * rows + r] = v; if (v > top) top = v;
    }
  }
  return { db, top };
}
// The whole track's loudest dB, from a coarse pass, as the reference for every tile of this file.
export function specTopOf(audio) {
  if (audio.specTop == null) audio.specTop = specDb(audio.buf, 0, audio.buf.duration, Math.min(600, Math.max(50, Math.round(audio.buf.duration * 2))), 80).top;
  return audio.specTop;
}
// Built in slices with a pause between them, so the page stays responsive; resolves null if `stale()` turns true meanwhile.
export async function specImage(buf, t0, t1, cols, rows, top, stale = () => false) {
  const db = new Float32Array(cols * rows), SLICE = 96;
  for (let from = 0; from < cols; from += SLICE) {
    if (stale()) return null;
    specDb(buf, t0, t1, cols, rows, { from, to: Math.min(cols, from + SLICE), db });
    await delay(0);
  }
  if (stale()) return null;
  const r = { db }, ref = top, n = SPEC_RAMP.length - 1;
  const cv = document.createElement("canvas"); cv.width = cols; cv.height = rows;
  const g = cv.getContext("2d"), img = g.createImageData(cols, rows);
  for (let f = 0; f < cols; f++) for (let y = 0; y < rows; y++) {
    const u = Math.max(0, Math.min(1, (r.db[f * rows + y] - (ref - 70)) / 70)) * n, i0 = Math.min(n - 1, Math.floor(u)), q = u - i0;
    const o = ((rows - 1 - y) * cols + f) * 4;
    for (let ch = 0; ch < 3; ch++) img.data[o + ch] = SPEC_RAMP[i0][ch] + (SPEC_RAMP[i0 + 1][ch] - SPEC_RAMP[i0][ch]) * q;
    img.data[o + 3] = 255;
  }
  g.putImageData(img, 0, 0);
  return cv.toDataURL("image/png");
}

export function setChartMode(v) { return (chartMode = v); }
export function setViewsCache(v) { return (viewsCache = v); }
