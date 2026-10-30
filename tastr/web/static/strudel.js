"use strict";
// "Try this in Strudel": the track card as Strudel live-code (https://strudel.cc).
// Linked out to strudel.cc only; Strudel itself (AGPL-3.0) is not bundled.
// Loaded before app.js; also loaded by tests/test_strudel.py under node.

const STRUDEL_NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const STRUDEL_SOUND = (lane) => {
  const l = lane.toLowerCase();
  return l.includes("kick") ? "bd" : l.includes("snare") ? "sd" : l.includes("clap") ? "cp" : l.includes("open") ? "oh"
    : l.includes("hat") ? "hh" : l.includes("tom") ? "lt" : l.includes("cymbal") ? "cr" : "rim";
};
const byBeat = (cells) => [0, 1, 2, 3].map((b) => `[${cells.slice(b * 4, b * 4 + 4).join(" ")}]`).join(" ");

// One 16-step lane as s("…"), grouped by beat; softer ("o") steps add a .gain with .55. Empty lanes give null.
function strudelLane(lane, p) {
  const steps = [...p.slice(0, 16).padEnd(16, ".")], snd = STRUDEL_SOUND(lane);
  if (steps.every((ch) => ch === ".")) return null;
  let line = `s("${byBeat(steps.map((ch) => (ch === "." ? "~" : snd)))}")`;
  if (steps.includes("o")) line += `.gain("${byBeat(steps.map((ch) => (ch === "o" ? ".55" : "1")))}")`;
  return line;
}

const STRUDEL_STEMS = ["vocals", "bass", "other", "drums"];

// audioUrl: where strudel.cc can fetch the original file (the track's /audio endpoint). When given, the sketch
// adds the original audio cut into bars at the detected downbeats, layered under the drums.
// stems: with audioUrl, the separated stems to play (names from STRUDEL_STEMS, served next to /audio at
// /stems/<name>); every stem gets a line in stack(), and the ones not listed start commented out.
// edits: the user's edits from the "Drums, bass & chords" view, {drums|bass|chords: {section idx or "all": …}}.
// Edited sections use them (drums: a pattern; bass: {notes, rhythm}; chords: {chords}); the rest use the detected data.
//
// One object per section type holds its drums (and chords and bass), and one `song` list of
// [bars, part, first bar] drives every layer, so commenting out a line skips that section everywhere.
function strudelCode(c, audioUrl = null, stems = null, edits = null) {
  const core = c.core || {}, key = core.key || {}, sugs = c.suggestions || [];
  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
  // by_section idx is the section's index in card.sections; sections without bars can't be arranged.
  const secs = (c.sections || []).map((s, idx) => ({ ...s, idx })).filter((s) => s.start_bar != null);
  const det = sugs.find((q) => q.data && q.data.origin === "detected");
  const pats = {};
  if (det && det.data.by_section) det.data.by_section.forEach((x) => (pats[x.idx] = x.pattern));
  const harm = sugs.find((q) => q.data && Array.isArray(q.data.progression));
  const prog = harm ? harm.data.progression.filter((n) => n && n !== "N") : [];
  // Per-section chord loops and bass lines (cards analyzed before they existed have only the single loop).
  const bassSug = sugs.find((q) => q.category === "bass" && q.data && Array.isArray(q.data.loop));
  const harmSecs = {}, bassSecs = {};
  if (harm && Array.isArray(harm.data.by_section)) harm.data.by_section.forEach((x) => (harmSecs[x.idx] = x));
  if (bassSug && Array.isArray(bassSug.data.by_section)) bassSug.data.by_section.forEach((x) => (bassSecs[x.idx] = x));
  const hasBpm = typeof core.bpm === "number" && isFinite(core.bpm);
  const bpm = hasBpm ? Math.round(core.bpm * 10) / 10 : 120;
  // The tempo comes from the bar grid when there is one, so the sketch and the original audio don't drift apart.
  const dbs = core.downbeat_times || [], gridBar = dbs.length > 1 ? (dbs[dbs.length - 1] - dbs[0]) / (dbs.length - 1) : null;
  const cpmBpm = gridBar ? Math.round((240 / gridBar) * 1000) / 1000 : bpm;
  const withAudio = Boolean(audioUrl) && typeof c.duration === "number" && c.duration > 0;
  const bar = gridBar || 240 / bpm, first = dbs.length ? dbs[0] : 0;
  // Without sections with bars, the whole track is one part.
  const song = secs.length ? secs : [{ idx: "all", label: "track", letter: "", start_bar: 1,
    end_bar: withAudio ? Math.max(1, Math.floor((c.duration - first) / bar)) : 1, whole: true }];
  const E = (part) => (edits && edits[part]) || {}, mine = (part, sec) => E(part)[sec.idx] || E(part).all;
  for (const sec of song) {
    const ec = mine("chords", sec), eb = mine("bass", sec);
    if (ec) harmSecs[sec.idx] = { progression: ec.chords || [] };
    if (eb) bassSecs[sec.idx] = { loop: eb.notes || [], rhythm: eb.rhythm || null };
  }
  const editedSecs = song.filter((sec) => ["drums", "bass", "chords"].some((p) => mine(p, sec)));
  const perSection = song.some((s) => harmSecs[s.idx] || bassSecs[s.idx]);
  const hasKey = typeof key.tonic === "number";
  const keyStr = hasKey ? `${STRUDEL_NOTES[key.tonic % 12]} ${key.mode || "major"}` : "unknown key";
  const title = (c.source && c.source.title) || "Untitled", artist = c.source && c.source.artist;
  const secName = (sec) => (sec.whole ? "Whole track" : [cap(sec.label), sec.letter].filter(Boolean).join(" "));
  const detected = editedSecs.length ? "" : "detected ";
  const out = [
    `// ${title}${artist ? " by " + artist : ""}`,
    `// Sketched by Tastr: ${hasBpm ? bpm + " BPM" : "tempo unknown, using 120 BPM"}, ${keyStr}. ${withAudio ? "The original audio cut into bars, layered" : `Not the original audio: the ${detected}`.trimEnd()}`,
    withAudio ? `// with the ${detected}${perSection ? "drums, bass and chords for each section." : "drum pattern for each section and the chord loop."}`
      : perSection ? "// drums, bass and chords for each section, played for that section's length."
      : "// drum pattern for each section, played for that section's length, plus the chord loop.",
    ...(editedSecs.length ? [`// With your edits in: ${[...new Set(editedSecs.map(secName))].join(", ")}.`] : []),
    `// Press Ctrl+Enter (Cmd+Enter on a Mac) to play, Ctrl+. to stop. Change anything and re-run.`,
    "",
    `setcpm(${cpmBpm}/4) // 1 cycle = 1 bar${cpmBpm !== bpm ? " (tempo from the detected bar grid)" : ""}`,
    "",
  ];

  const minor = key.mode === "minor";
  const chordsArr = prog.length ? prog : !hasKey ? []
    : (minor ? [0, 8, 3, 10] : [0, 7, 9, 5]).map((d, i) => STRUDEL_NOTES[(key.tonic + d) % 12] + ((minor ? [1, 0, 0, 0] : [0, 0, 1, 0])[i] ? "m" : ""));
  // Bass octave: 2 for C-E, 1 for F-B, so the roots stay in one low register. Unknown notes are rests.
  const rootNote = (name) => {
    const m = /^([A-G][#b]?)/.exec(name || "");
    if (!m) return "~";
    const n = m[1], i = STRUDEL_NOTES.indexOf(n.replace(/^([A-G])b$/, (_, l) => STRUDEL_NOTES[(STRUDEL_NOTES.indexOf(l) + 11) % 12]));
    return `${n.toLowerCase()}${i >= 0 && i <= 4 ? 2 : 1}`;
  };
  const okChord = (n) => (n && /^[A-G]/.test(n) ? n : "~");
  const bassStruct = (r) => (r && /[xo]/.test(r) ? byBeat([...r.slice(0, 16).padEnd(16, ".")].map((ch) => (ch === "." ? "~" : "x"))) : "[~ x]*4");
  const drumsOf = (pat) => {
    const lanes = Object.entries(pat || {}).filter(([, p]) => typeof p === "string").map(([lane, p]) => strudelLane(lane, p)).filter(Boolean);
    return lanes.length ? `stack(\n    ${lanes.join(",\n    ")}\n  )` : "silence";
  };
  // Same letter = same part, holding the first such section's drums (and chords and bass); a section with no chords or no bass is silence.
  const varOf = (sec) => `${sec.label.replace(/[^a-z]/gi, "").toLowerCase() || "part"}${(sec.letter || "").replace(/[^a-z0-9]/gi, "")}`;
  out.push(`// --- Parts: one per section type (same letter = same part): drums${perSection ? ", chords and bass" : ""} ---`);
  // An edited section whose part differs from its letter's first section gets a part of its own, so the edit is heard.
  const bodies = new Map(), partOf = new Map();
  for (const sec of song) {
    const lines = [`  drums: ${drumsOf(mine("drums", sec) || pats[sec.idx] || (det && det.data.pattern))},`];
    if (perSection) {
      const hs = harmSecs[sec.idx], bs = bassSecs[sec.idx];
      const cp = hs ? (hs.progression || []).map(okChord) : chordsArr;
      const bl = bs ? (bs.loop || []).map(rootNote) : chordsArr.map(rootNote);
      lines.push(`  chords: ${cp.some((n) => n !== "~") ? `chord("<${cp.join(" ")}>")` : "silence"},`,
        `  bass: ${bl.some((n) => n !== "~") ? `note("<${bl.join(" ")}>").struct("${bassStruct(bs ? bs.rhythm : null)}")` : "silence"},`);
    }
    const body = lines.join("\n");
    let v = varOf(sec);
    if (bodies.has(v) && bodies.get(v) !== body && editedSecs.includes(sec)) v = `${v}_bar${sec.start_bar}`;
    partOf.set(sec, v);
    if (bodies.has(v)) continue;
    bodies.set(v, body);
    out.push(`const ${v} = {`, ...lines, "}");
  }
  out.push("", "// --- Song: [bars, part, first bar] in the order they happen ---",
    "// Comment a line out (//) to skip that section in every layer, or reorder lines to rearrange.", "const song = [");
  for (const sec of song) {
    const end = sec.end_bar != null ? sec.end_bar : sec.start_bar;
    out.push(`  [${end - sec.start_bar + 1}, ${partOf.get(sec)}, ${sec.start_bar}], // ${secName(sec)}, bars ${sec.start_bar}-${end}`);
  }
  out.push("]", "", `const drums = arrange(...song.map(([bars, p]) => [bars, p.drums])).bank("RolandTR909")`);
  const parts = ["drums"];
  if (perSection) {
    out.push(`const chords = arrange(...song.map(([bars, p]) => [bars, p.chords])).voicing().s("sawtooth").lpf(1400).gain(.22).room(.3)`,
      `const bass = arrange(...song.map(([bars, p]) => [bars, p.bass])).s("sawtooth").lpf(320).gain(.5)`);
    parts.push("bass", "chords");
  } else if (chordsArr.length) {
    out.push("", `// --- Harmony: ${prog.length ? "the detected chord loop" : `a common loop in ${keyStr} (no confident chord loop was detected)`}, one chord per bar ---`);
    out.push(`const chords = chord("<${chordsArr.join(" ")}>").voicing().s("sawtooth").lpf(1400).gain(.22).room(.3)`);
    out.push(`const bass = note("<${chordsArr.map(rootNote).join(" ")}>").struct("[~ x]*4").s("sawtooth").lpf(320).gain(.5)`);
    parts.push("bass", "chords");
  }
  out.push("");
  const drop = secs.find((x) => x.label === "drop") || song[0], at = drop.start_bar;
  const where = drop.label === "drop" ? "first drop" : "opening bars";
  if (!withAudio) {
    out.push(`stack(${parts.join(", ")})`, "",
      "// Ideas: mute a layer by deleting it from stack(), skip or reorder lines in song,",
      "// try .bank(\"RolandTR808\"), or add .fast(2) to the hats for double time.");
    return out.join("\n");
  }
  const url = (n) => (n ? audioUrl.replace(/\/audio$/, `/stems/${n}`) : audioUrl).replace(/'/g, "%27");
  out.push(stems ? "// --- The original track as stems, cut into bars at the detected downbeats ---" : "// --- The original track, cut into bars at the detected downbeats ---",
    `// Tastr serves the ${stems ? "audio" : "file"} from your computer, so keep \`tastr serve\` running. The first time,`,
    "// your browser may ask whether strudel.cc may reach your local network: allow it.",
    "// Single quotes on purpose: in Strudel, double quotes mean a pattern.",
    ...(stems ? ["samples({", ...STRUDEL_STEMS.map((n) => `  ${n}: '${url(n)}',`), "})"] : [`samples({ orig: '${url()}' })`]),
    `const len = ${c.duration.toFixed(3)}, bar = ${bar.toFixed(4)}, first = ${first.toFixed(3)} // seconds: track, one bar, first downbeat`,
    `// orig(name, a, b): bars a to b of ${stems ? "a stem" : "the original"}, one bar per cycle`,
    "const orig = (name, a, b) => {",
    "  const k = run(b - a + 1).slow(b - a + 1).add(a - 1)",
    "  return s(name).begin(k.mul(bar).add(first).div(len)).end(k.add(1).mul(bar).add(first).div(len)).clip(1)",
    "}",
    `// part(name): ${stems ? "one stem" : "the original"}, following the song above`,
    "const part = (name) => arrange(...song.map(([bars, p, from]) => [bars, orig(name, from, from + bars - 1)]))", "");
  const origDrums = !stems || stems.includes("drums");
  if (stems) out.push("// Comment a line out (//) to mute it, or remove the // to bring it in.");
  out.push("stack(");
  if (stems) STRUDEL_STEMS.forEach((n) => out.push(`  ${stems.includes(n) ? "" : "// "}part("${n}"),${n === "drums" && !origDrums ? " // the sketch drums below play instead" : ""}`));
  else out.push(`  part("orig"),`);
  out.push(`  drums.gain(${origDrums ? ".5" : ".8"}),`, ...(parts.length > 1 ? ["  // bass, chords,"] : []), ")", "",
    `// Ideas: loop the ${where}${stems ? " of the vocals" : ""} with orig("${stems ? "vocals" : "orig"}", ${at}, ${at + 3}), skip or reorder lines in song,`,
    "// bring in bass and chords, or load a second track as another sample for a mash-up.");
  return out.join("\n");
}

// Same encoding as Strudel's code2hash (@strudel/core util.mjs): base64 of the UTF-8 code, URI-encoded.
function strudelLink(code) {
  const bytes = new TextEncoder().encode(code);
  let bin = "";
  for (const x of bytes) bin += String.fromCharCode(x);
  return `https://strudel.cc/#${encodeURIComponent(btoa(bin))}`;
}

if (typeof module !== "undefined") module.exports = { strudelCode, strudelLink, strudelLane, STRUDEL_STEMS };
