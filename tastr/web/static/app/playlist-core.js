// Lists and playlists without the page (TAS-79): list names that fit, the track picker's matches, and the player's
// queue. No imports, so tests/test_playlist_core.py runs it in node.

// The server takes list names up to 80 characters (routes/lists.py).
export const LIST_NAME_MAX = 80;

// A name that fits in `max`: drop bracketed parts like "(Extended Mix)" or "[Remastered]" first, then cut at a word
// boundary and end with "…".
export function fitName(s, max = LIST_NAME_MAX) {
  s = String(s || "").replace(/\s+/g, " ").trim();
  if (s.length <= max) return s;
  let t = s;
  const br = /\s*[([][^)\]]*[)\]]/;
  while (t.length > max && br.test(t)) t = t.replace(br, "").replace(/\s+/g, " ").trim();
  if (t.length <= max) return t;
  const cut = t.slice(0, max - 1), sp = cut.lastIndexOf(" ");
  return (sp > max / 3 ? cut.slice(0, sp) : cut).replace(/[\s,;:–—-]+$/, "") + "…";
}

// "Title — Artist", as the track picker shows and matches a track.
export const comboName = (t) => (t ? (t.title || `Track ${t.id}`) + (t.artist ? ` — ${t.artist}` : "") : "");

// The picker's rows for what's typed: every track by title when empty, else a case-insensitive substring match.
// Returns { hits, shown, more, at }: at is the row to highlight (the picked track, or the first match).
export const COMBO_ROWS = 60;
export function comboHits(list, q, curId) {
  const s = String(q || "").trim().toLowerCase();
  const all = [...list].sort((a, b) => (a.title || "").localeCompare(b.title || "") || a.id - b.id);
  const hits = s ? all.filter((t) => comboName(t).toLowerCase().includes(s)) : all;
  let at = hits.findIndex((t) => t.id === curId);
  if (at >= COMBO_ROWS) at = -1;
  if (s && at < 0) at = hits.length ? 0 : -1;
  return { hits, shown: hits.slice(0, COMBO_ROWS), more: Math.max(0, hits.length - COMBO_ROWS), at, s };
}

// ---- the player's queue: ids in list order, `order` the play order (indexes into ids), `i` the place in it ----
export function newQueue(name, ids, { shuffle = false, rnd = Math.random } = {}) {
  const q = { name, ids: [...ids], order: ids.map((_, k) => k), i: 0, shuffle: false, played: new Set() };
  if (shuffle) { q.i = Math.floor(rnd() * ids.length); toggleShuffle(q, rnd); }
  return q;
}
export const queueId = (q) => q.ids[q.order[q.i]];
// Shuffle on: the tracks that haven't played yet go in random order after the current one; the played ones stay
// where they are, before it. Shuffle off: back to list order, from the current track.
export function toggleShuffle(q, rnd = Math.random) {
  const cur = q.order[q.i];
  q.shuffle = !q.shuffle;
  if (q.shuffle) {
    const before = q.order.slice(0, q.i).filter((k) => q.played.has(k));
    const rest = q.order.filter((k) => k !== cur && !before.includes(k));
    for (let k = rest.length - 1; k > 0; k--) { const j = Math.floor(rnd() * (k + 1)); [rest[k], rest[j]] = [rest[j], rest[k]]; }
    q.order = [...before, cur, ...rest];
    q.i = before.length;
  } else {
    q.order = q.ids.map((_, k) => k);
    q.i = cur;
  }
  return q;
}
// Where ⏮ goes: "restart" more than 3 s into a track (or on the first one), else the previous place.
export const prevStep = (q, t) => (t > 3 || q.i === 0 ? "restart" : q.i - 1);
// The next place when a track ends or is skipped, or null after the last one.
export const nextStep = (q) => (q.i < q.order.length - 1 ? q.i + 1 : null);
export const markPlayed = (q) => q.played.add(q.order[q.i]);
