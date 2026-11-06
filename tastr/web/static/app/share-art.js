// The preview picture for a mash-up's short link: both tracks' album art in the 808 look, 1200×630 (the size chat apps
// show for og:image). Drawn here on a canvas and sent with the share, so the server needs no image library. A track
// with no art gets a drum-pad tile instead. Only the admins' links get one (routes/strudel.py decides).

export const ART_W = 1200, ART_H = 630;
const BG = "#161514", CREAM = "#f4efe2", MUTED = "#b9b1a0", ORANGE = "#e2531d", AMBER = "#f0a01c", KEY = "#3a3734";
const MONO = '"Space Mono", ui-monospace, monospace', SANS = '"Space Grotesk", system-ui, sans-serif';
const SIDE = 340, TOP = 170, LEFT = 80, GAP = 40; // the two covers: squares SIDE wide, GAP apart

function rounded(g, x, y, w, ht, r) {
  g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + ht, r); g.arcTo(x + w, y + ht, x, y + ht, r);
  g.arcTo(x, y + ht, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath();
}

// Text cut to `max` px wide with an ellipsis; returns at most `lines` lines.
function wrap(g, text, max, lines) {
  const words = String(text || "").split(/\s+/).filter(Boolean), out = [];
  let cur = "";
  for (const w of words) {
    const next = cur ? `${cur} ${w}` : w;
    if (g.measureText(next).width <= max || !cur) cur = next;
    else { out.push(cur); cur = w; }
  }
  if (cur) out.push(cur);
  if (out.length > lines) { out.length = lines; out[lines - 1] += "…"; }
  return out.map((l) => {
    if (g.measureText(l).width <= max) return l;
    while (l.length > 1 && g.measureText(l + "…").width > max) l = l.slice(0, -1);
    return l.replace(/…?$/, "…");
  });
}

// A cover, cropped to a square from its middle, with the 808 key's shadow under it.
function cover(g, img, x, y) {
  g.fillStyle = "#000"; rounded(g, x, y + 8, SIDE, SIDE, 6); g.fill();
  g.save(); rounded(g, x, y, SIDE, SIDE, 6); g.clip();
  const s = Math.min(img.naturalWidth || img.width, img.naturalHeight || img.height);
  const sx = ((img.naturalWidth || img.width) - s) / 2, sy = ((img.naturalHeight || img.height) - s) / 2;
  g.drawImage(img, sx, sy, s, s, x, y, SIDE, SIDE);
  g.restore();
}

// A drum-machine pad in the track's color, its LED lit and the title on it, for a track with no art.
function pad(g, t, color, x, y) {
  g.fillStyle = "#000"; rounded(g, x, y + 8, SIDE, SIDE, 6); g.fill();
  g.fillStyle = color; rounded(g, x, y, SIDE, SIDE, 6); g.fill();
  g.fillStyle = "#0002"; g.fillRect(x, y + SIDE - 26, SIDE, 26); // the key's lower lip
  g.fillStyle = "#ff7a3d"; g.shadowColor = "#ff7a3d"; g.shadowBlur = 18;
  g.beginPath(); g.arc(x + SIDE / 2, y + 40, 9, 0, Math.PI * 2); g.fill(); g.shadowBlur = 0;
  g.fillStyle = BG; g.font = `700 34px ${MONO}`; g.textAlign = "center";
  wrap(g, (t.title || "Untitled").toUpperCase(), SIDE - 60, 3).forEach((l, i, all) =>
    g.fillText(l, x + SIDE / 2, y + SIDE / 2 + 12 + (i - (all.length - 1) / 2) * 42));
  g.textAlign = "left";
}

// The 3×3 pad logo of the standard preview, small.
function logo(g, x, y) {
  const lit = { 0: "#b9381f", 1: ORANGE, 2: ORANGE, 4: "#e8801c", 7: AMBER };
  for (let i = 0; i < 9; i++) { g.fillStyle = lit[i] || KEY; rounded(g, x + (i % 3) * 22, y + Math.floor(i / 3) * 22, 18, 18, 3); g.fill(); }
}

// One track's lines in the text column: a colored key with its letter, the title and the artist.
function trackText(g, t, letter, color, x, y, w) {
  g.fillStyle = color; rounded(g, x, y, 34, 34, 4); g.fill();
  g.fillStyle = BG; g.font = `700 20px ${MONO}`; g.textAlign = "center"; g.fillText(letter, x + 17, y + 24); g.textAlign = "left";
  g.fillStyle = CREAM; g.font = `600 30px ${SANS}`;
  const lines = wrap(g, t.title || "Untitled", w - 50, 2);
  lines.forEach((l, i) => g.fillText(l, x + 50, y + 26 + i * 36));
  let yy = y + 26 + lines.length * 36;
  if (t.artist) { g.fillStyle = MUTED; g.font = `400 22px ${SANS}`; g.fillText(wrap(g, t.artist, w - 50, 1)[0], x + 50, yy); yy += 30; }
  return yy;
}

// a, b: { title, artist, img } (img an HTMLImageElement or null). Draws onto `canvas` (sized here) and returns it.
export function drawShareArt(canvas, a, b) {
  canvas.width = ART_W; canvas.height = ART_H;
  const g = canvas.getContext("2d");
  g.fillStyle = BG; g.fillRect(0, 0, ART_W, ART_H);
  g.fillStyle = ORANGE; g.fillRect(0, ART_H - 10, ART_W, 10);
  logo(g, LEFT, 64);
  g.fillStyle = ORANGE; g.font = `700 34px ${MONO}`; g.fillText("T A S T R", LEFT + 84, 96);
  g.fillStyle = MUTED; g.font = `400 16px ${MONO}`; g.fillText("M A S H - U P", LEFT + 86, 124);
  [[a, ORANGE], [b, AMBER]].forEach(([t, color], i) => {
    const x = LEFT + i * (SIDE + GAP);
    if (t.img) cover(g, t.img, x, TOP); else pad(g, t, color, x, TOP);
  });
  // The × between the covers: an orange key over the gap.
  const cx = LEFT + SIDE + GAP / 2, cy = TOP + SIDE / 2;
  g.fillStyle = "#000"; g.beginPath(); g.arc(cx, cy + 4, 34, 0, Math.PI * 2); g.fill();
  g.fillStyle = ORANGE; g.beginPath(); g.arc(cx, cy, 34, 0, Math.PI * 2); g.fill();
  g.fillStyle = CREAM; g.font = `700 40px ${MONO}`; g.textAlign = "center"; g.fillText("×", cx, cy + 13); g.textAlign = "left";
  // The text column, right of the covers.
  const tx = LEFT + 2 * SIDE + GAP + 50, tw = ART_W - tx - 60;
  const y = trackText(g, a, "A", ORANGE, tx, TOP + 10, tw);
  trackText(g, b, "B", AMBER, tx, Math.max(y + 34, TOP + 170), tw);
  g.fillStyle = MUTED; g.font = `400 18px ${MONO}`; g.fillText("PLAY IT IN STRUDEL", tx, TOP + SIDE - 4);
  return canvas;
}

// The track's album art as an image, or null when it has none (or it won't load).
function coverOf(id) {
  return fetch(`/api/tracks/${id}/cover`).then((r) => (r.ok ? r.blob() : null)).then((blob) => {
    if (!blob) return null;
    const img = new Image(), url = URL.createObjectURL(blob);
    return new Promise((done) => {
      img.onload = () => { URL.revokeObjectURL(url); done(img); };
      img.onerror = () => { URL.revokeObjectURL(url); done(null); };
      img.src = url;
    });
  }).catch(() => null);
}

const about = (c, img) => ({ title: (c.source && c.source.title) || "Untitled", artist: c.source && c.source.artist, img });

// The picture for cards a and b as a PNG blob, or null when neither track has art (the link keeps the standard one).
export async function shareArt(a, b) {
  const [ia, ib] = await Promise.all([coverOf(a.id), coverOf(b.id)]);
  if (!ia && !ib) return null;
  await Promise.all(['700 34px "Space Mono"', '400 16px "Space Mono"', '600 30px "Space Grotesk"', '400 22px "Space Grotesk"']
    .map((f) => document.fonts.load(f).catch(() => null)));
  const canvas = drawShareArt(document.createElement("canvas"), about(a, ia), about(b, ib));
  return new Promise((done) => canvas.toBlob(done, "image/png"));
}
