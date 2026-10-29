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

function strudelCode(c) {
  const core = c.core || {}, key = core.key || {}, sugs = c.suggestions || [];
  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
  // by_section idx is the section's index in card.sections; sections without bars can't be arranged.
  const secs = (c.sections || []).map((s, idx) => ({ ...s, idx })).filter((s) => s.start_bar != null);
  const det = sugs.find((q) => q.data && q.data.origin === "detected");
  const pats = {};
  if (det && det.data.by_section) det.data.by_section.forEach((x) => (pats[x.idx] = x.pattern));
  const harm = sugs.find((q) => q.data && Array.isArray(q.data.progression));
  const prog = harm ? harm.data.progression.filter((n) => n && n !== "N") : [];
  const hasBpm = typeof core.bpm === "number" && isFinite(core.bpm);
  const bpm = hasBpm ? Math.round(core.bpm * 10) / 10 : 120;
  const hasKey = typeof key.tonic === "number";
  const keyStr = hasKey ? `${STRUDEL_NOTES[key.tonic % 12]} ${key.mode || "major"}` : "unknown key";
  const title = (c.source && c.source.title) || "Untitled", artist = c.source && c.source.artist;
  const out = [
    `// ${title}${artist ? " by " + artist : ""}`,
    `// Sketched by Tastr: ${hasBpm ? bpm + " BPM" : "tempo unknown, using 120 BPM"}, ${keyStr}. Not the original audio: the detected`,
    `// drum pattern for each section, played for that section's length, plus the chord loop.`,
    `// Press Ctrl+Enter (Cmd+Enter on a Mac) to play, Ctrl+. to stop. Change anything and re-run.`,
    "",
    `setcpm(${bpm}/4) // 1 cycle = 1 bar`,
    "",
  ];

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
  } else {
    out.push("// --- Drums: the detected groove, looped (no sections with bar numbers) ---",
      `const drums = ${partOf(det && det.data.pattern)}.bank("RolandTR909")`, "");
  }

  const minor = key.mode === "minor";
  const chordsArr = prog.length ? prog : !hasKey ? []
    : (minor ? [0, 8, 3, 10] : [0, 7, 9, 5]).map((d, i) => STRUDEL_NOTES[(key.tonic + d) % 12] + ((minor ? [1, 0, 0, 0] : [0, 0, 1, 0])[i] ? "m" : ""));
  const parts = ["drums"];
  if (chordsArr.length) {
    // Bass octave: 2 for C-E, 1 for F-B, so the roots stay in one low register.
    const rootNote = (name) => {
      const m = /^([A-G][#b]?)/.exec(name), n = m ? m[1] : STRUDEL_NOTES[key.tonic % 12];
      const i = STRUDEL_NOTES.indexOf(n.replace(/^([A-G])b$/, (_, l) => STRUDEL_NOTES[(STRUDEL_NOTES.indexOf(l) + 11) % 12]));
      return `${n.toLowerCase()}${i >= 0 && i <= 4 ? 2 : 1}`;
    };
    out.push(`// --- Harmony: ${prog.length ? "the detected chord loop" : `a common loop in ${keyStr} (no confident chord loop was detected)`}, one chord per bar ---`);
    out.push(`const chords = chord("<${chordsArr.join(" ")}>").voicing().s("sawtooth").lpf(1400).gain(.22).room(.3)`);
    out.push(`const bass = note("<${chordsArr.map(rootNote).join(" ")}>").struct("[~ x]*4").s("sawtooth").lpf(320).gain(.5)`, "");
    parts.push("bass", "chords");
  }
  out.push(`stack(${parts.join(", ")})`, "",
    "// Ideas: mute a part by deleting it from stack(), swap parts in arrange(),",
    "// try .bank(\"RolandTR808\"), or add .fast(2) to the hats for double time.");
  return out.join("\n");
}

// Same encoding as Strudel's code2hash (@strudel/core util.mjs): base64 of the UTF-8 code, URI-encoded.
function strudelLink(code) {
  const bytes = new TextEncoder().encode(code);
  let bin = "";
  for (const x of bytes) bin += String.fromCharCode(x);
  return `https://strudel.cc/#${encodeURIComponent(btoa(bin))}`;
}

if (typeof module !== "undefined") module.exports = { strudelCode, strudelLink, strudelLane };
