"use strict";
// "Try this in Strudel": the track card as Strudel live-code (https://strudel.cc).
// Linked out to strudel.cc only; Strudel itself (AGPL-3.0) is not bundled.
// Loaded before app.js; also loaded by tests/test_strudel.py under node.

const STRUDEL_NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const STRUDEL_SOUND = (lane) => {
  const l = lane.toLowerCase();
  return l.includes("kick") ? "bd" : l.includes("snare") ? "sd" : l.includes("clap") ? "cp" : l.includes("open") ? "oh"
    : l.includes("hat") ? "hh" : l.includes("tom") ? "lt" : "rim";
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
function strudelCode(c, audioUrl = null, stems = null) {
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
  const perSection = secs.length > 0 && secs.some((s) => harmSecs[s.idx] || bassSecs[s.idx]);
  const hasBpm = typeof core.bpm === "number" && isFinite(core.bpm);
  const bpm = hasBpm ? Math.round(core.bpm * 10) / 10 : 120;
  // The tempo comes from the bar grid when there is one, so the sketch and the original audio don't drift apart.
  const dbs = core.downbeat_times || [], gridBar = dbs.length > 1 ? (dbs[dbs.length - 1] - dbs[0]) / (dbs.length - 1) : null;
  const cpmBpm = gridBar ? Math.round((240 / gridBar) * 1000) / 1000 : bpm;
  const withAudio = Boolean(audioUrl) && typeof c.duration === "number" && c.duration > 0;
  const hasKey = typeof key.tonic === "number";
  const keyStr = hasKey ? `${STRUDEL_NOTES[key.tonic % 12]} ${key.mode || "major"}` : "unknown key";
  const title = (c.source && c.source.title) || "Untitled", artist = c.source && c.source.artist;
  const out = [
    `// ${title}${artist ? " by " + artist : ""}`,
    `// Sketched by Tastr: ${hasBpm ? bpm + " BPM" : "tempo unknown, using 120 BPM"}, ${keyStr}. ${withAudio ? "The original audio cut into bars, layered" : "Not the original audio: the detected"}`,
    withAudio ? `// with the detected ${perSection ? "drums, bass and chords for each section." : "drum pattern for each section and the chord loop."}`
      : perSection ? "// drums, bass and chords for each section, played for that section's length."
      : "// drum pattern for each section, played for that section's length, plus the chord loop.",
    `// Press Ctrl+Enter (Cmd+Enter on a Mac) to play, Ctrl+. to stop. Change anything and re-run.`,
    "",
    `setcpm(${cpmBpm}/4) // 1 cycle = 1 bar${cpmBpm !== bpm ? " (tempo from the detected bar grid)" : ""}`,
    "",
  ];

  let sectionVar = null;
  const partOf = (pat) => {
    const lanes = Object.entries(pat || {}).filter(([, p]) => typeof p === "string").map(([lane, p]) => strudelLane(lane, p)).filter(Boolean);
    return lanes.length ? `stack(\n  ${lanes.join(",\n  ")}\n)` : "silence";
  };
  if (secs.length) {
    out.push("// --- Drum parts (one per section type; same letter = same part) ---");
    const varOf = (sec) => `${sec.label.replace(/[^a-z]/gi, "").toLowerCase() || "part"}${sec.letter || ""}`;
    const seen = new Set();
    for (const sec of secs) {
      const v = varOf(sec);
      if (seen.has(v)) continue;
      seen.add(v);
      out.push(`const ${v} = ${partOf(pats[sec.idx] || (det && det.data.pattern))}`);
    }
    out.push("", "// --- Arrangement: [bars, part] in the order they happen ---", "const drums = arrange(");
    secs.forEach((sec, i) => {
      const end = sec.end_bar != null ? sec.end_bar : sec.start_bar, name = [cap(sec.label), sec.letter].filter(Boolean).join(" ");
      out.push(`  [${end - sec.start_bar + 1}, ${varOf(sec)}]${i < secs.length - 1 ? "," : ""} // ${name}, bars ${sec.start_bar}-${end}`);
    });
    out.push(`).bank("RolandTR909")`, "");
    sectionVar = varOf;
  } else {
    out.push("// --- Drums: the detected groove, looped (no sections with bar numbers) ---",
      `const drums = ${partOf(det && det.data.pattern)}.bank("RolandTR909")`, "");
  }

  const minor = key.mode === "minor";
  const chordsArr = prog.length ? prog : !hasKey ? []
    : (minor ? [0, 8, 3, 10] : [0, 7, 9, 5]).map((d, i) => STRUDEL_NOTES[(key.tonic + d) % 12] + ((minor ? [1, 0, 0, 0] : [0, 0, 1, 0])[i] ? "m" : ""));
  const parts = ["drums"];
  // Bass octave: 2 for C-E, 1 for F-B, so the roots stay in one low register. Unknown notes are rests.
  const rootNote = (name) => {
    const m = /^([A-G][#b]?)/.exec(name || "");
    if (!m) return "~";
    const n = m[1], i = STRUDEL_NOTES.indexOf(n.replace(/^([A-G])b$/, (_, l) => STRUDEL_NOTES[(STRUDEL_NOTES.indexOf(l) + 11) % 12]));
    return `${n.toLowerCase()}${i >= 0 && i <= 4 ? 2 : 1}`;
  };
  if (perSection) {
    // One bass/chord part per section type, like the drums; a section with no chords or no bass is silence.
    const okChord = (n) => (n && /^[A-G]/.test(n) ? n : "~");
    const bassStruct = (r) => (r && /[xo]/.test(r) ? byBeat([...r.slice(0, 16).padEnd(16, ".")].map((ch) => (ch === "." ? "~" : "x"))) : "[~ x]*4");
    out.push("// --- Bass and chords per section (one chord per bar, looping; same letter = same part) ---");
    const done = new Set();
    for (const sec of secs) {
      const v = sectionVar(sec);
      if (done.has(v)) continue;
      done.add(v);
      const hs = harmSecs[sec.idx], bs = bassSecs[sec.idx];
      const cp = hs ? (hs.progression || []).map(okChord) : chordsArr;
      const ch = cp.some((n) => n !== "~") ? `chord("<${cp.join(" ")}>")` : "silence";
      const bl = bs ? (bs.loop || []).map(rootNote) : chordsArr.map(rootNote);
      const bn = bl.some((n) => n !== "~") ? `note("<${bl.join(" ")}>").struct("${bassStruct(bs ? bs.rhythm : null)}")` : "silence";
      out.push(`const ${v}_chords = ${ch}, ${v}_bass = ${bn}`);
    }
    const arr = (suffix) => secs.map((sec, i) => {
      const end = sec.end_bar != null ? sec.end_bar : sec.start_bar;
      return `  [${end - sec.start_bar + 1}, ${sectionVar(sec)}_${suffix}]${i < secs.length - 1 ? "," : ""}`;
    });
    out.push("const chords = arrange(", ...arr("chords"), `).voicing().s("sawtooth").lpf(1400).gain(.22).room(.3)`);
    out.push("const bass = arrange(", ...arr("bass"), `).s("sawtooth").lpf(320).gain(.5)`, "");
    parts.push("bass", "chords");
  } else if (chordsArr.length) {
    out.push(`// --- Harmony: ${prog.length ? "the detected chord loop" : `a common loop in ${keyStr} (no confident chord loop was detected)`}, one chord per bar ---`);
    out.push(`const chords = chord("<${chordsArr.join(" ")}>").voicing().s("sawtooth").lpf(1400).gain(.22).room(.3)`);
    out.push(`const bass = note("<${chordsArr.map(rootNote).join(" ")}>").struct("[~ x]*4").s("sawtooth").lpf(320).gain(.5)`, "");
    parts.push("bass", "chords");
  }
  if (withAudio) {
    const bar = gridBar || 240 / bpm, first = dbs.length ? dbs[0] : 0;
    const spans = secs.length ? secs.map((sec) => [sec.start_bar, sec.end_bar != null ? sec.end_bar : sec.start_bar, [cap(sec.label), sec.letter].filter(Boolean).join(" ")])
      : [[1, Math.max(1, Math.floor((c.duration - first) / bar)), "Whole track"]];
    if (stems) {
      const drumsOn = stems.includes("drums"), stemUrl = (n) => audioUrl.replace(/\/audio$/, `/stems/${n}`).replace(/'/g, "%27");
      out.push("// --- The original track as stems, cut into bars at the detected downbeats ---",
        "// Tastr serves the audio from your computer, so keep `tastr serve` running. The first time,",
        "// your browser may ask whether strudel.cc may reach your local network: allow it.",
        "// Single quotes on purpose: in Strudel, double quotes mean a pattern.",
        "samples({", ...STRUDEL_STEMS.map((n) => `  ${n}: '${stemUrl(n)}',`), "})",
        `const len = ${c.duration.toFixed(3)}, bar = ${bar.toFixed(4)}, first = ${first.toFixed(3)} // seconds: track, one bar, first downbeat`,
        "// orig(name, a, b): bars a to b of a stem, one bar per cycle",
        "const orig = (name, a, b) => {",
        "  const k = run(b - a + 1).slow(b - a + 1).add(a - 1)",
        "  return s(name).begin(k.mul(bar).add(first).div(len)).end(k.add(1).mul(bar).add(first).div(len)).clip(1)",
        "}",
        "// part(name): one stem across the whole track, section by section",
        "const part = (name) => arrange(");
      spans.forEach(([a, b, name], i) => out.push(`  [${b - a + 1}, orig(name, ${a}, ${b})]${i < spans.length - 1 ? "," : ""} // ${name}`));
      out.push(")", "", "// Comment a line out (//) to mute it, or remove the // to bring it in.", "stack(",
        ...STRUDEL_STEMS.map((n) => `  ${stems.includes(n) ? "" : "// "}part("${n}"),${n === "drums" && !drumsOn ? " // the sketch drums below play instead" : ""}`),
        `  drums.gain(${drumsOn ? ".5" : ".8"}),`, ...(parts.length > 1 ? ["  // bass, chords,"] : []), ")");
      const drop = secs.find((x) => x.label === "drop") || secs[0], at = drop ? drop.start_bar : 1;
      out.push("", `// Ideas: loop the ${drop && drop.label === "drop" ? "first drop" : "opening bars"} of the vocals with orig("vocals", ${at}, ${at + 3}), reorder the lines in arrange(),`,
        "// bring in bass and chords, or load a second track as another sample for a mash-up.");
      return out.join("\n");
    }
    out.push("// --- The original track, cut into bars at the detected downbeats ---",
      "// Tastr serves the file from your computer, so keep `tastr serve` running. The first time,",
      "// your browser may ask whether strudel.cc may reach your local network: allow it.",
      `samples({ orig: '${audioUrl.replace(/'/g, "%27")}' }) // single quotes: in Strudel, double quotes mean a pattern`,
      `const len = ${c.duration.toFixed(3)}, bar = ${bar.toFixed(4)}, first = ${first.toFixed(3)} // seconds: track, one bar, first downbeat`,
      "// orig(a, b): bars a to b of the original, one bar per cycle",
      "const orig = (a, b) => {",
      "  const k = run(b - a + 1).slow(b - a + 1).add(a - 1)",
      "  return s(\"orig\").begin(k.mul(bar).add(first).div(len)).end(k.add(1).mul(bar).add(first).div(len)).clip(1)",
      "}",
      "const original = arrange(");
    spans.forEach(([a, b, name], i) => out.push(`  [${b - a + 1}, orig(${a}, ${b})]${i < spans.length - 1 ? "," : ""} // ${name}`));
    const drop = secs.find((x) => x.label === "drop") || secs[0], at = drop ? drop.start_bar : 1;
    out.push(")", "", `stack(original, drums.gain(.5)${parts.length > 1 ? " /*, bass, chords */" : ""})`, "",
      `// Ideas: loop the ${drop && drop.label === "drop" ? "first drop" : "opening bars"} with orig(${at}, ${at + 3}), reorder the lines in arrange(),`,
      "// add bass and chords back into stack(), or load a second track as another sample for a mash-up.");
  } else {
    out.push(`stack(${parts.join(", ")})`, "",
      "// Ideas: mute a part by deleting it from stack(), swap parts in arrange(),",
      "// try .bank(\"RolandTR808\"), or add .fast(2) to the hats for double time.");
  }
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
