// HPSS in the page: the harmonic part of a short clip, for the label panel's 🔊 with the drums off (what the key
// detector listens to). A step-by-step port of librosa.effects.hpss with its defaults (STFT, 31-wide median filters
// across time and frequency, soft masks, inverse STFT), first written for the browser signals of PR #35. The clip is
// a few seconds, so it runs on the main thread in about a fifth of a second.

const SIG_NFFT = 2048, SIG_HOP = 512, SIG_KERNEL = 31;
const SIG_BINS = SIG_NFFT / 2 + 1;
const F32_TINY = 1.1754943508222875e-38; // np.finfo(np.float32).tiny
const f32 = Math.fround;
// Periodic Hann window, as scipy.signal.get_window("hann", n, fftbins=True).
function sigHann(n) {
  const w = new Float64Array(n);
  for (let i = 0; i < n; i++) w[i] = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / n);
  return w;
}

// In-place iterative radix-2 complex FFT of length re.length (a power of two); `inverse` leaves out the 1/N.
function sigFFTPlan(n) {
  const rev = new Uint32Array(n), bits = Math.log2(n);
  for (let i = 0; i < n; i++) {
    let r = 0;
    for (let b = 0; b < bits; b++) r |= ((i >> b) & 1) << (bits - 1 - b);
    rev[i] = r;
  }
  const cos = new Float64Array(n / 2), sin = new Float64Array(n / 2);
  for (let i = 0; i < n / 2; i++) { cos[i] = Math.cos((2 * Math.PI * i) / n); sin[i] = Math.sin((2 * Math.PI * i) / n); }
  return { n, rev, cos, sin };
}
function sigFFT(plan, re, im, inverse) {
  const { n, rev, cos, sin } = plan, sign = inverse ? 1 : -1;
  for (let i = 0; i < n; i++) {
    const j = rev[i];
    if (j > i) { let t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t; }
  }
  for (let size = 2; size <= n; size <<= 1) {
    const half = size >> 1, step = n / size;
    for (let start = 0; start < n; start += size) {
      for (let k = 0; k < half; k++) {
        const wr = cos[k * step], wi = sign * sin[k * step];
        const a = start + k, b = a + half;
        const xr = re[b] * wr - im[b] * wi, xi = re[b] * wi + im[b] * wr;
        re[b] = re[a] - xr; im[b] = im[a] - xi;
        re[a] += xr; im[a] += xi;
      }
    }
  }
}

// Real FFT of x (length 2h) through one complex FFT of length h: X[k] for k = 0..h into xr, xi (length h + 1).
// The plan is for length h; tw* are e^(-2*pi*i*k / 2h) for k < h. zr, zi: scratch of length h.
function sigTwiddles(n) {
  const c = new Float64Array(n / 2), s = new Float64Array(n / 2);
  for (let k = 0; k < n / 2; k++) { c[k] = Math.cos((2 * Math.PI * k) / n); s[k] = -Math.sin((2 * Math.PI * k) / n); }
  return { c, s };
}
function sigRFFT(plan, tw, x, zr, zi, xr, xi) {
  const h = plan.n;
  for (let m = 0; m < h; m++) { zr[m] = x[2 * m]; zi[m] = x[2 * m + 1]; }
  sigFFT(plan, zr, zi, false);
  for (let k = 0; k <= h; k++) {
    const a = k % h, b = (h - k) % h;
    const er = (zr[a] + zr[b]) / 2, ei = (zi[a] - zi[b]) / 2; // even part: (Z[k] + conj Z[h-k]) / 2
    const or = (zi[a] + zi[b]) / 2, oi = -(zr[a] - zr[b]) / 2; // odd part: (Z[k] - conj Z[h-k]) / 2i
    const c = k < h ? tw.c[k] : -1, s = k < h ? tw.s[k] : 0;
    xr[k] = er + or * c - oi * s; xi[k] = ei + or * s + oi * c;
  }
}
// numpy.fft.irfft(X, n = 2h): X[0..h] in xr, xi (the imaginary parts of bins 0 and h are ignored) -> x (length 2h).
function sigIRFFT(plan, tw, xr, xi, zr, zi, x) {
  const h = plan.n;
  for (let k = 0; k < h; k++) {
    const ar = xr[k], ai = k ? xi[k] : 0, br = xr[h - k], bi = h - k === h ? 0 : -xi[h - k]; // b = conj X[h-k]
    const er = (ar + br) / 2, ei = (ai + bi) / 2, dr = (ar - br) / 2, di = (ai - bi) / 2;
    const c = tw.c[k], s = -tw.s[k]; // e^(+2*pi*i*k / 2h)
    const or = dr * c - di * s, oi = dr * s + di * c;
    zr[k] = er - oi; zi[k] = ei + or; // Z = E + i * O
  }
  sigFFT(plan, zr, zi, true);
  for (let m = 0; m < h; m++) { x[2 * m] = zr[m] / h; x[2 * m + 1] = zi[m] / h; }
}

// Running median over src[0..count-1], window SIG_KERNEL, scipy.ndimage's "reflect" edges (d c b a | a b c d),
// into dst. Needs count >= SIG_KERNEL. `win` is scratch of length SIG_KERNEL.
function sigMedian(src, dst, count, win) {
  const r = SIG_KERNEL >> 1;
  const at = (i) => src[i < 0 ? -i - 1 : i >= count ? 2 * count - 1 - i : i];
  for (let k = 0; k < SIG_KERNEL; k++) win[k] = at(k - r);
  win.sort();
  for (let i = 0; ; i++) {
    dst[i] = win[r];
    if (i + 1 === count) return;
    // Slide: drop the value leaving at i - r, add the one arriving at i + r + 1, keeping `win` sorted.
    const out = at(i - r), inn = at(i + r + 1);
    let p = 0;
    while (win[p] !== out) p++;
    if (inn >= out) { while (p + 1 < SIG_KERNEL && win[p + 1] < inn) { win[p] = win[p + 1]; p++; } }
    else { while (p > 0 && win[p - 1] > inn) { win[p] = win[p - 1]; p--; } }
    win[p] = inn;
  }
}

// HPSS's harmonic part of mono audio (librosa.effects.hpss(y)[0]), or null when the clip is too short for the
// 31-frame median.
function hpssHarmonic(y, onProgress) {
  const len = y.length, frames = 1 + Math.floor(len / SIG_HOP), pad = SIG_NFFT / 2, half = SIG_NFFT / 2;
  if (frames < SIG_KERNEL) return null;
  const w = sigHann(SIG_NFFT), plan = sigFFTPlan(half), tw = sigTwiddles(SIG_NFFT);
  const x = new Float64Array(SIG_NFFT), zr = new Float64Array(half), zi = new Float64Array(half);
  const xr = new Float64Array(SIG_BINS), xi = new Float64Array(SIG_BINS);
  const n = frames * SIG_BINS, report = (f) => { if (onProgress) onProgress(f); };
  const dRe = new Float32Array(n), dIm = new Float32Array(n), mag = new Float32Array(n);
  // STFT, centred with zero padding (librosa's pad_mode="constant"): frame t covers y[t*hop - n_fft/2 ...].
  for (let t = 0; t < frames; t++) {
    const o = t * SIG_HOP - pad;
    for (let m = 0; m < SIG_NFFT; m++) { const j = o + m; x[m] = j >= 0 && j < len ? y[j] * w[m] : 0; }
    sigRFFT(plan, tw, x, zr, zi, xr, xi);
    for (let k = 0, base = t * SIG_BINS; k < SIG_BINS; k++) {
      const r = (dRe[base + k] = xr[k]), i = (dIm[base + k] = xi[k]);
      mag[base + k] = Math.hypot(dRe[base + k], dIm[base + k]);
      if (r !== r || i !== i) throw new Error("non-finite audio");
    }
    if (t % 1024 === 0) report(0.25 * t / frames);
  }
  // Median filters: the percussive one across 31 bins of each frame, the harmonic one across 31 frames of each bin.
  const perc = new Float32Array(n), harm = new Float32Array(n), win = new Float64Array(SIG_KERNEL);
  for (let t = 0; t < frames; t++) sigMedian(mag.subarray(t * SIG_BINS, (t + 1) * SIG_BINS), perc.subarray(t * SIG_BINS), SIG_BINS, win);
  report(0.4);
  const col = new Float32Array(frames), med = new Float32Array(frames);
  for (let k = 0; k < SIG_BINS; k++) {
    for (let t = 0; t < frames; t++) col[t] = mag[t * SIG_BINS + k];
    sigMedian(col, med, frames, win);
    for (let t = 0; t < frames; t++) harm[t * SIG_BINS + k] = med[t];
    if (k % 128 === 0) report(0.4 + 0.35 * k / SIG_BINS);
  }
  // Soft mask (librosa.util.softmask, power 2, split_zeros), in float32 like librosa, applied to the STFT.
  for (let i = 0; i < n; i++) {
    const h = harm[i], p = perc[i], z = h > p ? h : p;
    let mask = 0.5;
    if (z >= F32_TINY) {
      const a = f32(f32(h / z) ** 2), b = f32(f32(p / z) ** 2);
      mask = f32(a / f32(a + b));
    }
    dRe[i] = dRe[i] * mask; dIm[i] = dIm[i] * mask;
  }
  // Inverse STFT: overlap-add the windowed frames, divide by the summed squared window, drop the centring pad.
  const full = new Float64Array(len + 2 * pad + SIG_NFFT), wss = new Float64Array(full.length);
  for (let t = 0; t < frames; t++) {
    const base = t * SIG_BINS, o = t * SIG_HOP;
    for (let k = 0; k < SIG_BINS; k++) { xr[k] = dRe[base + k]; xi[k] = dIm[base + k]; }
    sigIRFFT(plan, tw, xr, xi, zr, zi, x);
    for (let m = 0; m < SIG_NFFT; m++) { full[o + m] += x[m] * w[m]; wss[o + m] += w[m] * w[m]; }
    if (t % 1024 === 0) report(0.75 + 0.25 * t / frames);
  }
  const out = new Float32Array(len);
  for (let j = 0; j < len; j++) {
    const s = wss[j + pad];
    out[j] = s > F32_TINY ? full[j + pad] / s : full[j + pad];
  }
  return out;
}

if (typeof module !== "undefined") module.exports = { hpssHarmonic };
