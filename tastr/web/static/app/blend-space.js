// Explore > Blend's math, with no page code so tests can run it in node (tests/test_blend.py).
// Every track is a point in a weighted space of measurements. Tempo is compared on a log scale folded by octaves
// (70 sits on 140), and key on the Camelot wheel (12A next to 1A). An item is
// { id, title, artist, bpm, camelot, energy, vibes: { tag: score } }.

const vibe = (tag) => (c) => [(c.vibes && c.vibes[tag]) || 0];
export const BL_DIMS = [
  { k: "tempo", name: "Tempo", w: 1, circ: true, get: (c) => { const a = (((Math.log2(c.bpm) % 1) + 1) % 1) * 2 * Math.PI; return [Math.cos(a), Math.sin(a)]; }, say: ["slower", "faster"], more: ["slower", "faster"], raw: (c) => c.bpm },
  { k: "key", name: "Key", w: 0.5, circ: true, get: (c) => { const m = /^(\d+)([AB])$/.exec(c.camelot); const a = ((+m[1] % 12) / 12) * 2 * Math.PI; return [Math.cos(a), Math.sin(a), m[2] === "B" ? 0.35 : -0.35]; } },
  { k: "energy", name: "Energy", w: 1, get: (c) => [c.energy], say: ["calmer", "higher energy"], more: ["calmer", "more energetic"] },
  { k: "danceable", name: "Danceable", w: 1, get: vibe("danceable"), say: ["less danceable", "danceable"], more: ["less danceable", "more danceable"] },
  { k: "percussive", name: "Percussive", w: 0.5, get: vibe("percussive"), say: ["softer hits", "percussive"], more: ["softer", "more percussive"] },
  { k: "bass", name: "Bass-heavy", w: 0.5, get: vibe("bass-heavy"), say: ["lighter low end", "bass-heavy"], more: ["lighter in the low end", "more bass-heavy"] },
  { k: "dark", name: "Dark", w: 0.5, get: vibe("dark"), say: ["brighter", "dark"], more: ["brighter", "darker"] },
  { k: "atmo", name: "Atmospheric", w: 0, get: vibe("atmospheric"), say: ["drier", "atmospheric"], more: ["drier", "more atmospheric"] },
];
// Starting points for the weights: [name, tooltip, weights]; anything not named is 0
export const BL_PRESETS = [
  ["Balanced", "Tempo, energy and the main vibes, with key a little", { tempo: 1, key: 0.5, energy: 1, danceable: 1, percussive: 0.5, bass: 0.5, dark: 0.5 }],
  ["Mixes well", "Tempo and key first: tracks you could beatmatch and blend in key", { tempo: 2, key: 2, energy: 0.5 }],
  ["Same feel", "Energy and vibes only, ignoring tempo and key", { energy: 1.5, danceable: 1, percussive: 1, bass: 1, dark: 1, atmo: 1 }],
];
export const defaultWeights = () => Object.fromEntries(BL_DIMS.map((d) => [d.k, d.w]));
export const presetOn = (w, p) => BL_DIMS.every((d) => (p[d.k] ?? 0) === w[d.k]);
// Blend needs a tempo, a Camelot key and an energy; tracks without them stay out of it
export const blendable = (c) => c.bpm > 0 && isFinite(c.bpm) && /^(\d+)([AB])$/.test(c.camelot || "") && typeof c.energy === "number" && isFinite(c.energy);

export const dist = (a, b) => Math.sqrt(a.reduce((s, x, i) => s + (x - b[i]) ** 2, 0));
const mean = (v) => v.reduce((a, x) => a + x, 0) / v.length;
const spread = (v, m) => Math.sqrt(v.reduce((a, x) => a + (x - m) ** 2, 0) / v.length) || 1;
export const median = (a) => { const s = [...a].sort((x, y) => x - y); return s.length ? s[Math.floor(s.length / 2)] : 0; };

// Z-score each part across the library (circular parts share one spread), then weight by sqrt(weight / parts)
export function blendSpace(cs, w) {
  if (!cs.length) return [];
  const raw = cs.map((c) => BL_DIMS.map((d) => d.get(c)));
  const cols = BL_DIMS.map((d, j) => {
    const sd = raw[0][j].map((_, q) => { const v = raw.map((r) => r[j][q]), m = mean(v); return { m, s: spread(v, m) }; });
    if (d.circ) { const s0 = Math.max(...sd.map((x) => x.s)); sd.forEach((x) => (x.s = s0)); }
    return sd;
  });
  return raw.map((r) => r.flatMap((parts, j) => parts.map((x, q) => ((x - cols[j][q].m) / cols[j][q].s) * Math.sqrt((w[BL_DIMS[j].k] || 0) / parts.length))));
}

// k-means++ with a fixed seed, so the groups don't jump around between redraws
export function kmeans(vec, k) {
  if (!vec.length) return [];
  let seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const cent = [vec[Math.floor(rnd() * vec.length)]];
  while (cent.length < k) {
    const d2 = vec.map((v) => Math.min(...cent.map((c) => dist(v, c) ** 2))), tot = d2.reduce((a, x) => a + x, 0);
    let r = rnd() * tot, i = 0;
    while ((r -= d2[i]) > 0 && i < vec.length - 1) i++;
    cent.push(vec[i]);
  }
  let lab = vec.map(() => 0);
  for (let it = 0; it < 30; it++) {
    lab = vec.map((v) => { let best = 0; cent.forEach((c, j) => { if (dist(v, c) < dist(v, cent[best])) best = j; }); return best; });
    cent.forEach((_, j) => { const mem = vec.filter((_, i) => lab[i] === j); if (mem.length) cent[j] = mem[0].map((_, q) => mem.reduce((a, m) => a + m[q], 0) / mem.length); });
  }
  return lab;
}

// The two main directions of the space, by power iteration: { pts: [[x, y]], project(v) }
export function pca2(vec) {
  const n = vec.length, d = vec[0].length, mu = vec[0].map((_, q) => vec.reduce((a, v) => a + v[q], 0) / n), X = vec.map((v) => v.map((x, q) => x - mu[q]));
  const cov = Array.from({ length: d }, (_, i) => Array.from({ length: d }, (_, j) => X.reduce((a, v) => a + v[i] * v[j], 0) / n));
  const power = (deflate) => {
    let v = Array.from({ length: d }, (_, i) => Math.sin(i + 1));
    for (let it = 0; it < 60; it++) {
      let u = cov.map((row) => row.reduce((a, x, j) => a + x * v[j], 0));
      if (deflate) { const p = u.reduce((a, x, i) => a + x * deflate[i], 0); u = u.map((x, i) => x - p * deflate[i]); }
      const nn = Math.hypot(...u) || 1; v = u.map((x) => x / nn);
    }
    return v;
  };
  const e1 = power(null), e2 = power(e1), dot = (u, e) => u.reduce((a, x, i) => a + x * e[i], 0);
  return { pts: X.map((v) => [dot(v, e1), dot(v, e2)]), project: (v) => { const u = v.map((x, q) => x - mu[q]); return [dot(u, e1), dot(u, e2)]; } };
}

// A dimension's single number per track: log2 BPM for tempo, the measurement itself for the others
const valOf = (d) => (c) => (d.raw ? Math.log2(d.raw(c)) : d.get(c)[0]);
// One group in words: the two dimensions where its mean differs most from the library (|z| > 0.35), its size and median BPM
export function groupSummary(cs, lab, j, w) {
  const mem = cs.filter((_, i) => lab[i] === j);
  const parts = BL_DIMS.filter((d) => d.say && w[d.k] > 0).map((d) => {
    const val = valOf(d), all = cs.map(val), m = mean(all), sd = spread(all, m);
    return { d, z: (mem.reduce((a, c) => a + val(c), 0) / (mem.length || 1) - m) / sd };
  }).sort((a, b) => Math.abs(b.z) - Math.abs(a.z)).slice(0, 2).filter((x) => Math.abs(x.z) > 0.35);
  return `${parts.map((x) => x.d.say[x.z > 0 ? 1 : 0]).join(", ") || "middle of the library"} · ${mem.length} track${mem.length === 1 ? "" : "s"} · around ${Math.round(median(mem.map((c) => c.bpm)))} BPM`;
}

export const lerp = (a, b, t) => a.map((x, q) => x + t * (b[q] - x));
// The n nearest of `pool` (indexes into vec) to a point
export const nearest = (vec, pool, to, n = 5) => pool.map((i) => ({ i, d: dist(vec[i], to) })).sort((x, y) => x.d - y.d).slice(0, n);
// A path from ia to ib: at each 1/(steps+1) of the way, the nearest track not used yet
export function makePath(vec, ia, ib, steps = 5) {
  const used = new Set([ia, ib]), out = [];
  for (let s = 1; s <= steps; s++) {
    const tg = lerp(vec[ia], vec[ib], s / (steps + 1));
    let best = -1;
    vec.forEach((v, i) => { if (!used.has(i) && (best < 0 || dist(v, tg) < dist(vec[best], tg))) best = i; });
    if (best < 0) break;
    used.add(best); out.push(best);
  }
  return out;
}

// Two lists in the same space: their middles, how well they separate, and per-dimension differences (biggest first).
// ia, ib: indexes into cs/vec. nameB is list B's name, for the words.
export function compareLists(cs, vec, ia, ib, w, nameB) {
  const center = (ii) => vec[ii[0]].map((_, q) => ii.reduce((a, i) => a + vec[i][q], 0) / ii.length), cA = center(ia), cB = center(ib);
  const inB = new Set(ib), both = ia.filter((i) => inB.has(i)), inBoth = new Set(both);
  // share of tracks (in only one list) that sit closer to their own list's middle
  const onlyA = ia.filter((i) => !inBoth.has(i)), onlyB = ib.filter((i) => !inBoth.has(i));
  const right = onlyA.filter((i) => dist(vec[i], cA) <= dist(vec[i], cB)).length + onlyB.filter((i) => dist(vec[i], cB) < dist(vec[i], cA)).length;
  const share = onlyA.length + onlyB.length ? right / (onlyA.length + onlyB.length) : 0;
  const apart = share > 0.85 ? "Easy to tell apart" : share > 0.7 ? "Fairly distinct" : "Hard to tell apart";
  const A = ia.map((i) => cs[i]), B = ib.map((i) => cs[i]);
  const rows = BL_DIMS.filter((d) => w[d.k] > 0).map((d) => {
    if (d.k === "key") {
      const mode = (L) => { const n = {}; L.forEach((c) => (n[c.camelot] = (n[c.camelot] || 0) + 1)); return Object.entries(n).sort((x, y) => y[1] - x[1])[0][0]; };
      const ka = mode(A), kb = mode(B), na = parseInt(ka), nb = parseInt(kb);
      const dd = Math.min(Math.abs(na - nb), 12 - Math.abs(na - nb)) + (ka.slice(-1) !== kb.slice(-1) ? 1 : 0);
      return { d, a: `mostly ${ka}`, b: `mostly ${kb}`, z: null, words: dd === 0 ? "same home key" : dd <= 1 ? "keys mix well" : `${dd} steps apart on the wheel`, abs: dd / 3 };
    }
    const val = valOf(d), all = cs.map(val), m = mean(all), sd = spread(all, m);
    const avg = (L) => (d.raw ? median(L.map(val)) : mean(L.map(val))), z = (avg(B) - avg(A)) / sd;
    const fmt = (L) => (d.raw ? `${Math.round(median(L.map(d.raw)))} BPM` : mean(L.map(val)).toFixed(2));
    const words = Math.abs(z) < 0.25 ? "about the same" : `${nameB} is ${Math.abs(z) > 1 ? "a lot " : ""}${d.more[z > 0 ? 1 : 0]}`;
    return { d, a: fmt(A), b: fmt(B), z, words, abs: Math.abs(z) };
  }).sort((x, y) => y.abs - x.abs);
  return { cA, cB, both, onlyA, onlyB, share, apart, rows };
}
