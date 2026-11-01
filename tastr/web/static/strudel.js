"use strict";
// "Try this in Strudel": the track card as Strudel live-code (https://strudel.cc).
// Linked out to strudel.cc only; Strudel itself (AGPL-3.0) is not bundled.
// Loaded before the UI modules (app/main.js); also loaded by tests/test_strudel.py under node.

const STRUDEL_NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const STRUDEL_SOUND = (lane) => {
  const l = lane.toLowerCase();
  return l.includes("kick") ? "bd" : l.includes("snare") ? "sd" : l.includes("clap") ? "cp" : l.includes("open") ? "oh"
    : l.includes("hat") ? "hh" : l.includes("tom") ? "lt" : l.includes("cymbal") ? "cr" : "rim";
};
// A bar of 16ths (16 in 4/4, 12 in 3/4; "~" = rest) as short mini-notation, the way it's typed live:
// the shortest repeating unit (1, 2, 4 or 8 steps) times its repeats, at the coarsest step its hits sit on.
// x...x...x...x... -> bd*4, ....x.......x... -> [~ sd]*2, ..x...x...x...x. -> [~ hh]*4. With no repeat
// the bar is grouped by beat, at that step: [bd ~] [~ bd] [~ bd] [~ ~].
function compactSteps(cells, beats = 4) {
  const n = beats * 4;
  const p = [1, 2, 4, 8].find((q) => q < n && n % q === 0 && cells.every((x, i) => x === cells[i % q])) || n;
  const r = [8, 4, 2, 1].find((q) => q <= p && p % q === 0 && cells.every((x, i) => x === "~" || i % q === 0));
  const unit = cells.slice(0, p).filter((_, i) => i % r === 0);
  if (unit.every((x) => x === unit[0] && x !== "~")) return `${unit[0]}*${n / r}`;
  if (p < n) return `[${unit.join(" ")}]*${n / p}`;
  if (r >= 4) return unit.join(" ");
  const per = 4 / r;
  return [...Array(beats).keys()].map((b) => `[${unit.slice(b * per, b * per + per).join(" ")}]`).join(" ");
}

// A list of per-bar values for <…>, with runs written as x!n and a list that repeats its first half
// shortened to that half: [Am, ~, C, C] -> <Am ~ C!2>, [Am, F, Am, F] -> <Am F>, [Am, Am] -> Am.
function compactBars(values) {
  let v = values.slice();
  while (v.length % 2 === 0 && v.length > 1 && v.every((x, i) => x === v[i % (v.length / 2)])) v = v.slice(0, v.length / 2);
  if (v.length === 1) return v[0];
  const out = [];
  for (let i = 0; i < v.length;) {
    let j = i + 1;
    while (j < v.length && v[j] === v[i]) j++;
    out.push(j - i > 1 ? `${v[i]}!${j - i}` : v[i]);
    i = j;
  }
  return `<${out.join(" ")}>`;
}

// A knob strudel.cc draws as a slider in the code, to sweep while it plays.
const slider = (v, lo, hi) => `slider(${v}, ${lo}, ${hi})`;

// One lane (16 steps in 4/4, 12 in 3/4) as s("…") in short mini-notation; softer ("o") steps add a .gain with .55. Empty lanes give null.
function strudelLane(lane, p, beats = 4) {
  const n = beats * 4, steps = [...p.slice(0, n).padEnd(n, ".")], snd = STRUDEL_SOUND(lane);
  if (steps.every((ch) => ch === ".")) return null;
  let line = `s("${compactSteps(steps.map((ch) => (ch === "." ? "~" : snd)), beats)}")`;
  // Rests read as "~" here too: a gain pattern only matters where the lane has a hit.
  if (steps.includes("o")) line += `.gain("${compactSteps(steps.map((ch) => (ch === "o" ? ".55" : ch === "." ? "~" : "1")), beats)}")`;
  return line;
}

const STRUDEL_STEMS = ["vocals", "bass", "other", "drums"];

const capWord = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
// Bass octave: 2 for C-E, 1 for F-B, so the roots stay in one low register. Unknown notes are rests.
function rootNote(name) {
  const m = /^([A-G][#b]?)/.exec(name || "");
  if (!m) return "~";
  const n = m[1], i = STRUDEL_NOTES.indexOf(n.replace(/^([A-G])b$/, (_, l) => STRUDEL_NOTES[(STRUDEL_NOTES.indexOf(l) + 11) % 12]));
  return `${n.toLowerCase()}${i >= 0 && i <= 4 ? 2 : 1}`;
}
const okChord = (n) => (n && /^[A-G]/.test(n) ? n : "~");
// A bar whose bass moves (beat notes from the engine, e.g. C C C G) as weighted runs: [c2@3 g1].
function bassBar(name, beatNotes) {
  if (!Array.isArray(beatNotes) || !beatNotes.length || beatNotes.some((n) => !n)) return rootNote(name);
  const runs = [];
  for (const n of beatNotes.map(rootNote)) {
    if (runs.length && runs[runs.length - 1].n === n) runs[runs.length - 1].k++;
    else runs.push({ n, k: 1 });
  }
  if (runs.length === 1) return runs[0].n;
  const even = runs.every((r) => r.k === runs[0].k);
  return `[${runs.map((r) => r.n + (even || r.k === 1 ? "" : "@" + r.k)).join(" ")}]`;
}
// Same letter = same part: the part's variable name.
const varOf = (sec) => `${sec.label.replace(/[^a-z]/gi, "").toLowerCase() || "part"}${(sec.letter || "").replace(/[^a-z0-9]/gi, "")}`;
const sketchSecName = (sec) => (sec.whole ? "Whole track" : [capWord(sec.label), sec.letter].filter(Boolean).join(" "));
const byIdx = (list) => { const o = {}; if (Array.isArray(list)) list.forEach((x) => (o[x.idx] = x)); return o; };

// Tempo, meter and the bar grid. The tempo comes from the bar grid when there is one, so the sketch and the original audio don't drift apart.
function sketchTempo(c, audioUrl) {
  const core = c.core || {};
  const hasBpm = typeof core.bpm === "number" && isFinite(core.bpm);
  const beats = core.beats_per_bar === 3 ? 3 : 4; // 3/4 bars have 3 beats, 12 steps
  const bpm = hasBpm ? Math.round(core.bpm * 10) / 10 : 120;
  const dbs = core.downbeat_times || [], gridBar = dbs.length > 1 ? (dbs[dbs.length - 1] - dbs[0]) / (dbs.length - 1) : null;
  const cpmBpm = gridBar ? Math.round((60 * beats / gridBar) * 1000) / 1000 : bpm;
  const withAudio = Boolean(audioUrl) && typeof c.duration === "number" && c.duration > 0;
  return { hasBpm, beats, nSteps: beats * 4, bpm, cpmBpm, withAudio, bar: gridBar || 60 * beats / bpm, first: dbs.length ? dbs[0] : 0 };
}

// Everything the sketch is written from (s): the song's sections, their drums, chords and bass with your edits in, and the tempo.
function sketchInputs(c, audioUrl, edits) {
  const core = c.core || {}, key = core.key || {}, sugs = c.suggestions || [];
  const t = sketchTempo(c, audioUrl);
  // by_section idx is the section's index in card.sections; sections without bars can't be arranged.
  const secs = (c.sections || []).map((x, idx) => ({ ...x, idx })).filter((x) => x.start_bar != null);
  const det = sugs.find((q) => q.data && q.data.origin === "detected");
  const pats = {};
  if (det && det.data.by_section) det.data.by_section.forEach((x) => (pats[x.idx] = x.pattern));
  const harm = sugs.find((q) => q.data && Array.isArray(q.data.progression));
  const prog = harm ? harm.data.progression.filter((n) => n && n !== "N") : [];
  // Per-section chord loops and bass lines (cards analyzed before they existed have only the single loop).
  const bassSug = sugs.find((q) => q.category === "bass" && q.data && Array.isArray(q.data.loop));
  const harmSecs = byIdx(harm && harm.data.by_section), bassSecs = byIdx(bassSug && bassSug.data.by_section);
  // Without sections with bars, the whole track is one part.
  const song = secs.length ? secs : [{ idx: "all", label: "track", letter: "", start_bar: 1,
    end_bar: t.withAudio ? Math.max(1, Math.floor((c.duration - t.first) / t.bar)) : 1, whole: true }];
  const E = (part) => (edits && edits[part]) || {}, mine = (part, sec) => E(part)[sec.idx] || E(part).all;
  for (const sec of song) {
    const ec = mine("chords", sec), eb = mine("bass", sec);
    if (ec) harmSecs[sec.idx] = { progression: ec.chords || [] };
    if (eb) bassSecs[sec.idx] = { loop: eb.notes || [], rhythm: eb.rhythm || null };
  }
  const hasKey = typeof key.tonic === "number", minor = key.mode === "minor";
  const chordsArr = prog.length ? prog : !hasKey ? []
    : (minor ? [0, 8, 3, 10] : [0, 7, 9, 5]).map((d, i) => STRUDEL_NOTES[(key.tonic + d) % 12] + ((minor ? [1, 0, 0, 0] : [0, 0, 1, 0])[i] ? "m" : ""));
  const perSection = song.some((x) => harmSecs[x.idx] || bassSecs[x.idx]);
  // Sidechain: when the card suggests ducking the bass under the kick, the kick ducks the bass's orbit (2) and the chords' (3).
  const pump = sugs.some((q) => q.category === "bass" && q.data && q.data.template === "{sidechain}");
  return { ...t, c, secs, song, det, pats, prog, harmSecs, bassSecs, mine, perSection, chordsArr,
    editedSecs: song.filter((sec) => ["drums", "bass", "chords"].some((p) => mine(p, sec))),
    keyStr: hasKey ? `${STRUDEL_NOTES[key.tonic % 12]} ${key.mode || "major"}` : "unknown key",
    duck: pump && (perSection || chordsArr.length > 0),
    // Wobble: a bass that sits on one note at dubstep tempo gets Strudel's filter LFO, locked to the bar, on the 8ths.
    wobble: Boolean(bassSug && bassSug.data.movement === "pedal" && (c.tags || []).some((x) => x && x.tag === "dubstep/halftime tempo")) };
}

function headerLines(s) {
  const { c, hasBpm, bpm, cpmBpm, beats, withAudio, perSection, editedSecs } = s;
  const title = (c.source && c.source.title) || "Untitled", artist = c.source && c.source.artist;
  const detected = editedSecs.length ? "" : "detected ";
  return [
    `// ${title}${artist ? " by " + artist : ""}`,
    `// Sketched by Tastr: ${hasBpm ? bpm + " BPM" : "tempo unknown, using 120 BPM"}, ${s.keyStr}. ${withAudio ? "The original audio cut into bars, layered" : `Not the original audio: the ${detected}`.trimEnd()}`,
    withAudio ? `// with the ${detected}${perSection ? "drums, bass and chords for each section." : "drum pattern for each section and the chord loop."}`
      : perSection ? "// drums, bass and chords for each section, played for that section's length."
      : "// drum pattern for each section, played for that section's length, plus the chord loop.",
    ...(editedSecs.length ? [`// With your edits in: ${[...new Set(editedSecs.map(sketchSecName))].join(", ")}.`] : []),
    `// Press Ctrl+Enter (Cmd+Enter on a Mac) to play, Ctrl+. to stop. Change anything and re-run.`,
    "",
    `setcpm(${cpmBpm}/${beats}) // 1 cycle = 1 bar${beats === 3 ? " of 3/4" : ""}${cpmBpm !== bpm ? " (tempo from the detected bar grid)" : ""}`,
    "",
  ];
}

function drumsOf(s, pat) {
  const lanes = Object.entries(pat || {}).filter(([, p]) => typeof p === "string")
    .map(([lane, p]) => { const l = strudelLane(lane, p, s.beats); return l && s.duck && /kick/i.test(lane) ? l + `.duck("2:3").duckdepth(.6)` : l; }).filter(Boolean);
  return lanes.length ? `stack(\n    ${lanes.join(",\n    ")}\n  )` : "silence";
}
const bassStruct = (s, r) => (r && /[xo]/.test(r) ? compactSteps([...r.slice(0, s.nSteps).padEnd(s.nSteps, ".")].map((ch) => (ch === "." ? "~" : "x")), s.beats) : `[~ x]*${s.beats}`);

// One part's body: its drums, and with per-section harmony its chords and bass (silence when it has none).
function partBody(s, sec) {
  const lines = [`  drums: ${drumsOf(s, s.mine("drums", sec) || s.pats[sec.idx] || (s.det && s.det.data.pattern))},`];
  if (!s.perSection) return lines;
  const hs = s.harmSecs[sec.idx], bs = s.bassSecs[sec.idx];
  const cp = hs ? (hs.progression || []).map(okChord) : s.chordsArr;
  const bl = bs ? (bs.loop || []).map((n, k) => bassBar(n, bs.beats && bs.beats[k])) : s.chordsArr.map(rootNote);
  lines.push(`  chords: ${cp.some((n) => n !== "~") ? `chord("${compactBars(cp)}")` : "silence"},`,
    `  bass: ${bl.some((n) => n !== "~") ? `note("${compactBars(bl)}").struct("${bassStruct(s, bs ? bs.rhythm : null)}")` : "silence"},`);
  return lines;
}

// The parts (one per section type, holding the first such section's drums, chords and bass) and the song that arranges them.
function partAndSongLines(s) {
  const out = [`// --- Parts: one per section type (same letter = same part): drums${s.perSection ? ", chords and bass" : ""} ---`];
  // An edited section whose part differs from its letter's first section gets a part of its own, so the edit is heard.
  const bodies = new Map(), partOf = new Map();
  for (const sec of s.song) {
    const lines = partBody(s, sec), body = lines.join("\n");
    let v = varOf(sec);
    if (bodies.has(v) && bodies.get(v) !== body && s.editedSecs.includes(sec)) v = `${v}_bar${sec.start_bar}`;
    partOf.set(sec, v);
    if (bodies.has(v)) continue;
    bodies.set(v, body);
    out.push(`const ${v} = {`, ...lines, "}");
  }
  out.push("", "// --- Song: [bars, part, first bar] in the order they happen ---",
    "// Comment a line out (//) to skip that section in every layer, or reorder lines to rearrange.", "const song = [");
  for (const sec of s.song) {
    const end = sec.end_bar != null ? sec.end_bar : sec.start_bar;
    out.push(`  [${end - sec.start_bar + 1}, ${partOf.get(sec)}, ${sec.start_bar}], // ${sketchSecName(sec)}, bars ${sec.start_bar}-${end}`);
  }
  out.push("]", "", `const drums = arrange(...song.map(([bars, p]) => [bars, p.drums])).bank("RolandTR909")`);
  return out;
}

// The drums line is always there; bass and chords follow the song per section, or loop the chord progression.
// Filter knobs are sliders: drag them in strudel.cc to open up or darken the chords and bass.
function layerLines(s) {
  const { duck, wobble, beats, chordsArr } = s, out = [], parts = ["drums"];
  const CHORD_LPF = slider(1400, 200, 6000), BASS_LPF = slider(320, 60, 2000);
  const orbit = (n) => (duck ? `.orbit(${n})` : "");
  const WOB = wobble ? `.lpsync(${beats * 2}).lpdepth(2).lpq(6)` : "";
  if (duck || wobble) {
    out.push("", ...(duck ? ["// Sidechain: the kick ducks the bass (orbit 2) and the chords (orbit 3), the pumping in most house and EDM.",
      "// Remove .duck(...) from the kicks above for none, or change .duckdepth (0 to 1)."] : []),
      ...(wobble ? ["// Wobble: the bass's filter sweeps on the 8ths (.lpsync: sweeps per bar; try 12 for triplets, 16 for 16ths)."] : []));
  }
  if (s.perSection) {
    out.push(`const chords = arrange(...song.map(([bars, p]) => [bars, p.chords])).voicing().s("sawtooth").lpf(${CHORD_LPF}).gain(.22).room(.3)${orbit(3)}`,
      `const bass = arrange(...song.map(([bars, p]) => [bars, p.bass])).s("sawtooth").lpf(${BASS_LPF}).gain(.5)${WOB}${orbit(2)}`);
    parts.push("bass", "chords");
  } else if (chordsArr.length) {
    out.push("", `// --- Harmony: ${s.prog.length ? "the detected chord loop" : `a common loop in ${s.keyStr} (no confident chord loop was detected)`}, one chord per bar ---`);
    out.push(`const chords = chord("${compactBars(chordsArr)}").voicing().s("sawtooth").lpf(${CHORD_LPF}).gain(.22).room(.3)${orbit(3)}`);
    out.push(`const bass = note("${compactBars(chordsArr.map(rootNote))}").struct("[~ x]*${beats}").s("sawtooth").lpf(${BASS_LPF}).gain(.5)${WOB}${orbit(2)}`);
    parts.push("bass", "chords");
  }
  out.push("");
  return { lines: out, parts };
}

// One named line per layer: in Strudel `name: pattern` plays, and `_name:` mutes that line.
function playLines(s, startBar, lines) {
  const { song } = s;
  const lastBar = Math.max(...song.map((x) => (x.end_bar != null ? x.end_bar : x.start_bar)));
  const startAt = Number.isInteger(startBar) && startBar >= 1 && startBar <= lastBar ? startBar : 1;
  const startSec = song.find((x) => x.start_bar === startAt);
  return ["// --- Play: one named line per layer. Put _ in front of a name to mute it (_drums:), take it away to bring it back ---",
    `const start = ${startAt} // the bar to play from: 1 is the top${startAt > 1 && startSec && !startSec.whole ? ` (here: ${sketchSecName(startSec)})` : ""}`,
    ...lines,
    "// all() changes every line: start from bar `start`, and ._punchcard() draws what's playing under the code",
    "all(x => x.early(start - 1)._punchcard())", ""];
}

// The original track (or its stems) as samples, cut into bars at the detected downbeats, following the song.
function audioLines(s, audioUrl, stems) {
  const { c, bar, first } = s;
  const url = (n) => (n ? audioUrl.replace(/\/audio$/, `/stems/${n}`) : audioUrl).replace(/'/g, "%27");
  return [stems ? "// --- The original track as stems, cut into bars at the detected downbeats ---" : "// --- The original track, cut into bars at the detected downbeats ---",
    ...(/\/pub\/[^/]+\/tracks\//.test(audioUrl) // a shared server's signed link
      ? ["// Tastr serves the audio through a private link that works for a week; after that, open the sketch from Tastr again."]
      : [`// Tastr serves the ${stems ? "audio" : "file"} from your computer, so keep \`tastr serve\` running. The first time,`,
        "// your browser may ask whether strudel.cc may reach your local network: allow it."]),
    "// Single quotes on purpose: in Strudel, double quotes mean a pattern.",
    ...(stems ? ["samples({", ...STRUDEL_STEMS.map((n) => `  ${n}: '${url(n)}',`), "})"] : [`samples({ orig: '${url()}' })`]),
    `const len = ${c.duration.toFixed(3)}, bar = ${bar.toFixed(4)}, first = ${first.toFixed(3)} // seconds: track, one bar, first downbeat`,
    `// orig(name, a, b): bars a to b of ${stems ? "a stem" : "the original"}, one bar per cycle`,
    "const orig = (name, a, b) => {",
    "  const k = run(b - a + 1).slow(b - a + 1).add(a - 1)",
    "  return s(name).begin(k.mul(bar).add(first).div(len)).end(k.add(1).mul(bar).add(first).div(len)).clip(1)",
    "}",
    `// part(name): ${stems ? "one stem" : "the original"}, following the song above`,
    "const part = (name) => arrange(...song.map(([bars, p, from]) => [bars, orig(name, from, from + bars - 1)]))", ""];
}

// audioUrl: where strudel.cc can fetch the original file (the track's /audio endpoint, or a shared server's signed /pub/ link). When given, the sketch
// adds the original audio cut into bars at the detected downbeats, layered under the drums.
// stems: with audioUrl, the separated stems to play (names from STRUDEL_STEMS, served next to /audio at
// /stems/<name>); every stem gets a line in stack(), and the ones not listed start commented out.
// edits: the user's edits from the "Drums, bass & chords" view, {drums|bass|chords: {section idx or "all": …}}.
// Edited sections use them (drums: a pattern; bass: {notes, rhythm}; chords: {chords}); the rest use the detected data.
// startBar: the bar the sketch plays from (1 = the top), written as `const start` so it can be changed in strudel.cc.
//
// One object per section type holds its drums (and chords and bass), and one `song` list of
// [bars, part, first bar] drives every layer, so commenting out a line skips that section everywhere.
function strudelCode(c, audioUrl = null, stems = null, edits = null, startBar = 1) {
  const s = sketchInputs(c, audioUrl, edits);
  const layers = layerLines(s);
  const out = [...headerLines(s), ...partAndSongLines(s), ...layers.lines];
  const drop = s.secs.find((x) => x.label === "drop") || s.song[0], at = drop.start_bar;
  const where = drop.label === "drop" ? "first drop" : "opening bars";
  if (!s.withAudio) {
    out.push(...playLines(s, startBar, layers.parts.map((p) => `${p}: ${p}`)),
      "// Ideas: mute a layer with _, drag the sliders, skip or reorder lines in song,",
      "// try .bank(\"RolandTR808\"), or add .fast(2) to the hats for double time.");
    return out.join("\n");
  }
  const origDrums = !stems || stems.includes("drums");
  // Stem lines are named <stem>_stem so they don't clash with the sketch's own drums and bass lines.
  const lines = stems
    ? STRUDEL_STEMS.map((n) => `${stems.includes(n) ? "" : "_"}${n}_stem: part("${n}")${n === "drums" && !origDrums ? " // the sketch drums below play instead" : ""}`)
    : [`original: part("orig")`];
  lines.push(`drums: drums.gain(${slider(origDrums ? ".5" : ".8", 0, 1)})`, ...layers.parts.slice(1).map((p) => `_${p}: ${p}`));
  out.push(...audioLines(s, audioUrl, stems), ...playLines(s, startBar, lines),
    `// Ideas: loop the ${where}${stems ? " of the vocals" : ""} with orig("${stems ? "vocals" : "orig"}", ${at}, ${at + 3}), skip or reorder lines in song,`,
    "// bring in bass and chords (take the _ off), or load a second track as another sample for a mash-up.");
  return out.join("\n");
}

// Same encoding as Strudel's code2hash (@strudel/core util.mjs): base64 of the UTF-8 code, URI-encoded.
function strudelLink(code) {
  const bytes = new TextEncoder().encode(code);
  let bin = "";
  for (const x of bytes) bin += String.fromCharCode(x);
  return `https://strudel.cc/#${encodeURIComponent(btoa(bin))}`;
}


// ---------- Templates (TAS-67) ----------
// A template is Strudel code with {name} fill-ins, filled from the open track (strudelFillins). {{name}} keeps the braces.
// "Full sketch" is strudelCode itself; the other built-ins are template text, so "Copy this one to edit" gives real text.
const STRUDEL_TEMPLATES = [
  { id: "full", name: "Full sketch", tip: "Every section's drums, bass and chords, in song order, with the original audio if you want it.",
    text: "// {title}{by}: full sketch\n// {bpm} BPM, {key} ({camelot}).\n// Press Ctrl+Enter (Cmd+Enter on a Mac) to play, Ctrl+. to stop.\n\nsetcpm({cpm}) // 1 cycle = 1 bar\n\n{song}\n" },
  { id: "loop", name: "Loop the drop", tip: "Four bars of the first drop from the original audio (or stems), with a filter slider to ride.",
    text: "// {title}{by}: loop the drop, bars {drop}-{drop.end}\n// Press Ctrl+Enter to play. Drag the slider in the code to ride the filter.\n\nsetcpm({cpm}) // 1 cycle = 1 bar\n\n{audio}\n\nconst drums = {drums.drop}\n\nstack(\n  {loop}.lpf(slider(6000, 200, 12000)),\n  drums.bank(\"RolandTR909\").gain(.5),\n)\n" },
  { id: "drums", name: "Drums only", tip: "Each section's drum pattern in song order, with one line to switch the drum machine.",
    text: "// {title}{by}: the drums, section by section\n// Press Ctrl+Enter to play.\n\nsetcpm({cpm}) // 1 cycle = 1 bar\n\n// The kit: change the name and re-run. Try RolandTR808, RolandTR707, AkaiLinn or RolandCR78.\nconst kit = \"RolandTR909\"\n// Or switch kits every bar: const kit = \"<RolandTR909 RolandTR808>\"\n\n{drums.song}.bank(kit)\n" },
  { id: "half", name: "Half-time remix", tip: "The drop at the same tempo but half the pace: drums spread over two bars, audio at half speed (an octave down).",
    text: "// {title}{by}: half-time remix of the drop\n// Same tempo, half the pace. Each drum bar spreads over two, and the audio plays at half speed,\n// which also drops it an octave. Remove .speed(.5) to keep the pitch and hear it stutter instead.\n\nsetcpm({cpm}) // 1 cycle = 1 bar\n\n{audio}\n\nconst drums = {drums.drop}\n\nstack(\n  {loop}.slow(2).speed(.5).gain(.8),\n  drums.slow(2).bank(\"RolandTR808\").gain(.7),\n  note(\"{bass}\").struct(\"x ~ ~ ~\").slow(2).s(\"sawtooth\").lpf(250).gain(.5),\n)\n" },
  { id: "mash", name: "Mash-up", tip: "Pick a second track: its drop plays under this one, sped to line the bars up.",
    text: "// Mash-up: {title} with {b.title}\n// A: {bpm} BPM, {key} ({camelot}). B: {b.bpm} BPM, {b.key} ({b.camelot}).\n// B plays at speed {b.speed} so its bars line up with A. That also moves its pitch {b.shift}, to about {b.newkey}.\n\nsetcpm({cpm}) // 1 cycle = 1 bar of A\n\n{audio}\n{b.audio}\n\nstack(\n  {loop},\n  {b.loop}.speed({b.speed}).gain(.8),\n)\n" },
];
const STRUDEL_NEW_TEXT = "// {title}: my template\n// Fill-ins in curly braces come from the open track. Click one in the list to add it.\n\nsetcpm({cpm}) // 1 cycle = 1 bar\n\nconst drums = {drums.drop}\n\nstack(\n  drums.bank(\"RolandTR808\"),\n  note(\"{bass}\").struct(\"[~ x]*4\").s(\"sawtooth\").lpf(400).gain(.5),\n  chord(\"{chords}\").voicing().s(\"sawtooth\").lpf(1200).gain(.2),\n)\n";
const FILL_RE = /\{\{([a-z][\w.]*)\}\}|\{([a-z][\w.]*)\}/gi;
const strudelUsesAudio = (text) => /\{(audio|loop|track|stems\.\w+|b\.audio|b\.loop)\}/.test(text);
const strudelUsesMash = (text) => /\{b\.[\w.]+\}/.test(text);

// Camelot codes (8A = A minor, 8B = C major): each step round the wheel is a fifth.
function strudelCamelot(tonic, mode) { return `${((tonic * 7 + (mode === "minor" ? 4 : 7)) % 12) + 1}${mode === "minor" ? "A" : "B"}`; }
function camelotKey(code) { // "8A" -> { tonic: 9, mode: "minor" }
  const m = /^(\d+)([AB])$/.exec(code || "");
  if (!m) return null;
  const minor = m[2] === "A", n = +m[1] - 1;
  return { tonic: ((((n - (minor ? 4 : 7)) * 7) % 12) + 12) % 12, mode: minor ? "minor" : "major" };
}
function strudelCamelotNear(k) {
  const m = /^(\d+)([AB])$/.exec(k || "");
  if (!m) return [k];
  const n = +m[1], l = m[2], w = (d) => ((n + d + 11) % 12) + 1;
  return [k, `${w(-1)}${l}`, `${w(1)}${l}`, `${n}${l === "A" ? "B" : "A"}`];
}
// Playback speed that lines B's bars up with A's: A BPM / B BPM, folded by octaves into 0.71 to 1.42.
function strudelSpeed(aBpm, bBpm) {
  let r = aBpm / bBpm;
  while (r > 1.42) r /= 2;
  while (r < 0.71) r *= 2;
  return Math.round(r * 1000) / 1000;
}
// How well track B ({bpm, camelot}) mixes under A at that speed: the pitch shift, B's key after it, and whether it fits.
function strudelMashFit(a, b) {
  const r = strudelSpeed(a.bpm, b.bpm), semis = 12 * Math.log2(r), k = camelotKey(b.camelot);
  const nk = k ? { tonic: (((k.tonic + Math.round(semis)) % 12) + 12) % 12, mode: k.mode } : null;
  const cam = nk ? strudelCamelot(nk.tonic, nk.mode) : null;
  const fit = !cam || !a.camelot ? "key unknown" : cam === a.camelot ? "same key" : strudelCamelotNear(a.camelot).includes(cam) ? "mixes well" : "keys clash";
  return { r, semis, nk, cam, fit, ok: fit === "same key" || fit === "mixes well", score: (fit === "same key" || fit === "mixes well" ? 0 : 10) + Math.abs(semis) };
}

const tplSec = (s, label) => s.secs.find((x) => x.label === label)
  || (label === "drop" ? [...s.secs].sort((a, b) => (b.energy ?? 0) - (a.energy ?? 0))[0] || s.song[0] : null);
const keyText = (k) => (k && typeof k.tonic === "number" ? `${STRUDEL_NOTES[k.tonic % 12]} ${k.mode || "major"}` : "unknown key");
const quoteUrl = (u) => u.replace(/'/g, "%27");

// The original audio (or picked stems) as samples, cut into bars, with orig(name, a, b). `v` names the helpers
// ("" for track A; "B" for a mash-up's second track: samples b, origB(a, b)).
function tplAudio(s, url, stems, v = "") {
  if (!url) return { audio: "// (No original audio: this track's file isn't on the server, or the private link is still loading.)", loop: "silence" };
  const drop = tplSec(s, "drop"), a = drop.start_bar, b = Math.min(a + 3, drop.end_bar != null ? drop.end_bar : a + 3);
  const stemUrl = (n) => quoteUrl(url.replace(/\/audio$/, `/stems/${n}`)), names = stems && stems.length ? stems : null;
  const lc = v.toLowerCase();
  const lines = v ? [`samples({ ${lc}: '${quoteUrl(url)}' })`]
    : ["// The original audio, cut into bars at the detected downbeats. Single quotes on purpose: double quotes mean a pattern.",
      names ? `samples({\n${names.map((n) => `  ${n}: '${stemUrl(n)}',`).join("\n")}\n})` : `samples({ orig: '${quoteUrl(url)}' })`];
  lines.push(`const ${lc}len = ${s.c.duration.toFixed(3)}, ${lc}bar = ${s.bar.toFixed(4)}, ${lc}first = ${s.first.toFixed(3)} // seconds: track, one bar, first downbeat`,
    v ? `// orig${v}(a, b): bars a to b of the second track, one bar per cycle` : "// orig(name, a, b): bars a to b, one bar per cycle",
    v ? `const orig${v} = (a, b) => {` : "const orig = (name, a, b) => {",
    "  const k = run(b - a + 1).slow(b - a + 1).add(a - 1)",
    `  return s(${v ? `"${lc}"` : "name"}).begin(k.mul(${lc}bar).add(${lc}first).div(${lc}len)).end(k.add(1).mul(${lc}bar).add(${lc}first).div(${lc}len)).clip(1)`,
    "}");
  const loop = v ? `orig${v}(${a}, ${b})` : names ? `stack(${names.map((n) => `orig("${n}", ${a}, ${b})`).join(", ")})` : `orig("orig", ${a}, ${b})`;
  return { audio: lines.join("\n"), loop, a, b };
}

// Every fill-in for this track: name -> { v: value, d: what it is }.
// ctx: { audioUrl, stems, edits, startBar, b: { card, audioUrl } for a mash-up's second track }.
function strudelFillins(c, ctx = {}) {
  const s = sketchInputs(c, ctx.audioUrl, ctx.edits), key = (c.core || {}).key || {}, src = c.source || {};
  const ind = (n) => " ".repeat(n);
  const patOf = (sec) => (sec && (s.mine("drums", sec) || s.pats[sec.idx])) || (s.det && s.det.data.pattern) || {};
  const stackOf = (pat, pad = 0) => {
    const lanes = Object.entries(pat || {}).filter(([, p]) => typeof p === "string").map(([lane, p]) => strudelLane(lane, p, s.beats)).filter(Boolean);
    return lanes.length ? `stack(\n${ind(pad + 2)}${lanes.join(`,\n${ind(pad + 2)}`)}\n${ind(pad)})` : "silence";
  };
  const drop = tplSec(s, "drop"), dEnd = Math.min(drop.start_bar + 3, drop.end_bar != null ? drop.end_bar : drop.start_bar + 3);
  // Harmony: the drop's own chords and bass (your edits when they're in), else the track's loop.
  const hs = s.harmSecs[drop.idx], bs = s.bassSecs[drop.idx];
  const chords = hs ? (hs.progression || []).map(okChord) : s.chordsArr;
  const roots = bs && (bs.loop || []).length ? bs.loop.map(rootNote) : chords.map(rootNote);
  const loopOf = (xs) => (xs.some((x) => x !== "~") ? `<${xs.join(" ")}>` : "~");
  const full = strudelCode(c, ctx.audioUrl, ctx.stems, ctx.edits, ctx.startBar || 1).split("\n");
  const at = full.findIndex((l) => l.startsWith("setcpm("));
  const audio = tplAudio(s, ctx.audioUrl, ctx.stems);
  const url = ctx.audioUrl ? quoteUrl(ctx.audioUrl) : "";
  const labels = [...new Set([...s.secs.map((x) => x.label.replace(/[^\w]/g, "").toLowerCase()).filter(Boolean), "drop"])];
  const f = {
    title: { v: src.title || "Untitled", d: "Track title" },
    by: { v: src.artist ? ` by ${src.artist}` : "", d: "\" by Artist\", or nothing if there's no artist" },
    artist: { v: src.artist || "", d: "Artist" },
    bpm: { v: s.hasBpm ? String(s.bpm) : "120", d: "Tempo" },
    cpm: { v: `${s.cpmBpm}/${s.beats}`, d: "For setcpm(): one cycle per bar, from the bar grid" },
    key: { v: s.keyStr, d: "Key" },
    camelot: { v: key.camelot || "", d: "Camelot code" },
    scale: { v: typeof key.tonic === "number" ? `${STRUDEL_NOTES[key.tonic % 12].toLowerCase()}:${key.mode || "major"}` : "c:major", d: "For .scale(): n(\"0 2 4\").scale(\"{scale}\")" },
    chords: { v: loopOf(chords), d: "Chord loop, one per bar, for chord(\"…\")" },
    bass: { v: loopOf(roots), d: "Bass roots, one per bar, for note(\"…\")" },
    drums: { v: stackOf(patOf(drop)), d: "Drum pattern of the drop (or the loudest section)" },
    ...Object.fromEntries(labels.map((l) => [`drums.${l}`, {
      v: stackOf(patOf(l === "drop" ? drop : s.secs.find((x) => x.label.replace(/[^\w]/g, "").toLowerCase() === l))), d: `Drum pattern of the first ${l}` }])),
    "drums.song": { v: `arrange(\n${s.song.map((sec) => {
      const end = sec.end_bar != null ? sec.end_bar : sec.start_bar;
      return `  [${end - sec.start_bar + 1}, ${stackOf(patOf(sec), 2)}], // ${sketchSecName(sec)}, bars ${sec.start_bar}-${end}`;
    }).join("\n")}\n)`, d: "Every section's drums, in song order" },
    song: { v: full.slice(at + 1).join("\n").replace(/^\n+/, ""), d: "The whole full sketch after setcpm: parts, song order and layers (follows the audio, stems and edits choices)" },
    drop: { v: String(drop.start_bar), d: "First bar of the first drop" },
    "drop.end": { v: String(dEnd), d: "Fourth bar of the first drop" },
    audio: { v: audio.audio, d: "Loads the original audio (or the picked stems) and defines orig(name, a, b)" },
    loop: { v: audio.loop, d: "Bars {drop}-{drop.end} of the audio, needs {audio}" },
    track: { v: url ? `'${url}'` : "''", d: "Address of the original audio" },
    ...Object.fromEntries(STRUDEL_STEMS.map((n) => [`stems.${n}`, { v: url ? `'${url.replace(/\/audio$/, `/stems/${n}`)}'` : "''", d: `Address of the ${n} stem (separate stems first)` }])),
  };
  const B = ctx.b && ctx.b.card;
  if (B) {
    const sb = sketchInputs(B, ctx.b.audioUrl, null), bk = (B.core || {}).key || {};
    const fit = strudelMashFit({ bpm: s.bpm, camelot: key.camelot }, { bpm: sb.bpm, camelot: bk.camelot });
    const ba = tplAudio(sb, ctx.b.audioUrl, null, "B");
    Object.assign(f, {
      "b.title": { v: (B.source && B.source.title) || "Untitled", d: "Second track: title" },
      "b.bpm": { v: String(sb.bpm), d: "Second track: tempo" },
      "b.key": { v: sb.keyStr, d: "Second track: key" },
      "b.camelot": { v: bk.camelot || "", d: "Second track: Camelot code" },
      "b.speed": { v: String(fit.r), d: "Playback speed that lines B's bars up with this track" },
      "b.shift": { v: `${fit.semis >= 0 ? "up" : "down"} ${Math.abs(fit.semis).toFixed(1)} semitones`, d: "How far that speed moves B's pitch" },
      "b.newkey": { v: fit.nk ? `${keyText(fit.nk)} (${fit.cam}, ${fit.fit})` : "an unknown key", d: "B's key after the speed change" },
      "b.audio": { v: ba.audio, d: "Loads the second track and defines origB(a, b)" },
      "b.loop": { v: ba.loop, d: "Four bars of the second track's drop, needs {b.audio}" },
    });
  }
  return f;
}

// Fill a template: {{name}} stays as {name}; an unknown {name} stays as written and is reported.
function strudelFill(text, f) {
  const unknown = new Set();
  const code = text.replace(FILL_RE, (m, lit, name) => {
    if (lit) return `{${lit}}`;
    if (Object.prototype.hasOwnProperty.call(f, name)) return f[name].v;
    unknown.add(name);
    return m;
  });
  return { code, unknown: [...unknown] };
}
// Swap this track's values back for fill-ins (title, cpm, Camelot, key, BPM). `escape`: first turn braces that look
// like fill-ins into {{name}}, for code (Claude's version) saved as a template.
function strudelUnfill(code, f, escape = true) {
  let t = escape ? code.replace(/\{([a-z][\w.]*)\}/gi, "{{$1}}") : code;
  for (const k of ["title", "cpm", "camelot", "key", "bpm"]) {
    const v = f[k] && f[k].v;
    if (v && v.length > 1 && v !== "unknown key") t = t.split(v).join(`{${k}}`);
  }
  return t;
}
// Line diff (LCS): [[" " | "-" | "+", line]]. Fine for a few hundred lines.
function strudelDiff(a, b) {
  const A = a.split("\n"), B = b.split("\n"), n = A.length, m = B.length;
  const L = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && A[i] === B[j]) { out.push([" ", A[i]]); i++; j++; }
    else if (i < n && (j >= m || L[i + 1][j] >= L[i][j + 1])) out.push(["-", A[i++]]);
    else out.push(["+", B[j++]]);
  }
  return out;
}
// The changed lines with `context` lines around them, and null where lines are skipped (shown as ⋯).
function strudelHunks(diff, context = 1) {
  const keep = diff.map((_, i) => diff.slice(Math.max(0, i - context), i + context + 1).some(([m]) => m !== " "));
  const out = [];
  diff.forEach((d, i) => { if (keep[i]) { if (out.length && !keep[i - 1]) out.push(null); out.push(d); } });
  return out;
}
// What Claude gets to know about the track along with the code: tempo, meter, key and the sections.
function strudelSummary(c) {
  const s = sketchInputs(c, null, null), src = c.source || {};
  return [`${src.title || "Untitled"}${src.artist ? " by " + src.artist : ""}`,
    `${s.hasBpm ? s.bpm + " BPM" : "tempo unknown"}, ${s.beats}/4, ${s.keyStr}${((c.core || {}).key || {}).camelot ? ` (${c.core.key.camelot})` : ""}`,
    `Sections: ${s.secs.length ? s.secs.map((x) => `${sketchSecName(x)} bars ${x.start_bar}-${x.end_bar != null ? x.end_bar : x.start_bar}`).join("; ") : "none detected"}`].join("\n").slice(0, 1900);
}

if (typeof module !== "undefined") module.exports = { strudelCode, strudelLink, strudelLane, compactSteps, compactBars, STRUDEL_STEMS,
  STRUDEL_TEMPLATES, STRUDEL_NEW_TEXT, strudelFillins, strudelFill, strudelUnfill, strudelDiff, strudelHunks, strudelSummary,
  strudelSpeed, strudelMashFit, strudelCamelot, strudelUsesAudio, strudelUsesMash };
