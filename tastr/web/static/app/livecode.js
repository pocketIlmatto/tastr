// The track card as code for two more live-coding tools: TidalCycles (https://tidalcycles.org) and
// Sonic Pi (https://sonic-pi.net). Written from the same sketch inputs as the Strudel sketch (strudel.js):
// the song's sections, their drums, chords and bass with your edits in, and the tempo.
// Tastr only writes text in these languages; neither tool is bundled or linked in.
// The sketch helpers (sketchInputs, compactSteps, compactBars, varOf, sketchSecName, STRUDEL_NOTES) are strudel.js's
// globals, loaded before the modules; tests/test_livecode.py sets them up the same way under node.

// Lane name -> sound. Tidal: SuperDirt's default Dirt-Samples folders. Sonic Pi: its built-in samples.
const LC_DRUMS = [
  [/kick/, "bd", "bd_haus"], [/snare/, "sn", "sn_dolf"], [/clap/, "cp", "perc_snap"], [/open/, "oh", "drum_cymbal_open"],
  [/hat/, "hh", "drum_cymbal_closed"], [/tom/, "lt", "drum_tom_lo_soft"], [/cymbal/, "cr", "drum_splash_soft"], [/./, "rm", "elec_wood"],
];
const lcDrum = (lane, tool) => { const l = lane.toLowerCase(), row = LC_DRUMS.find(([re]) => re.test(l)); return row[tool === "tidal" ? 1 : 2]; };

// "C#m" -> { pc: 1, name: "c#", q: "minor" }; null for rests ("N", "~", empty). Qualities beyond major and minor
// (the engine names only those two; edits may hold more) map to each tool's own chord names.
const LC_QUALITY = { "": "major", maj: "major", m: "minor", min: "minor", 7: "dom7", maj7: "major7", m7: "minor7", min7: "minor7",
  dim: "dim", aug: "aug", sus2: "sus2", sus4: "sus4" };
export function lcChord(name) {
  const m = /^([A-G])([#b]?)(.*)$/.exec(name || "");
  if (!m) return null;
  const pc = (STRUDEL_NOTES.indexOf(m[1]) + (m[2] === "#" ? 1 : m[2] === "b" ? 11 : 0)) % 12;
  const q = LC_QUALITY[m[3]] || (/^m(?!aj)/.test(m[3]) ? "minor" : "major");
  return { pc, name: (m[1] + m[2]).toLowerCase(), q };
}
// A bass note in scientific octaves (C4 = middle C): 2 for C-E, 1 for F-B, as in the Strudel sketch.
const lcBassOct = (ch) => (ch.pc <= 4 ? 2 : 1);
// Tidal note names: s for sharp, f for flat, and octave 5 holds middle C (one above scientific).
const tidalNote = (ch, oct) => `${ch.name.replace("#", "s").replace(/^([a-g])b$/, "$1f")}${oct + 1}`;
// Sonic Pi note symbols: s for sharp, b for flat, scientific octaves.
const sonicNote = (ch, oct) => `:${ch.name.replace("#", "s")}${oct}`;

// One bass bar as the notes it plays: one per beat when the engine has per-beat notes, else the root alone. [] = rest.
function lcBassBar(name, beatNotes) {
  if (Array.isArray(beatNotes) && beatNotes.length && beatNotes.every((n) => lcChord(n))) return beatNotes.map(lcChord);
  const ch = lcChord(name);
  return ch ? [ch] : [];
}

// The parts, one per section type, the way the Strudel sketch builds them: each holds its drums (lane -> steps),
// chords (one per bar, looping), bass (one bar of notes per chord) and bass rhythm (steps, x = hit).
export function livecodeParts(s) {
  const parts = [], byName = new Map(), partOf = new Map();
  for (const sec of s.song) {
    const drums = s.mine("drums", sec) || s.pats[sec.idx] || (s.det && s.det.data.pattern) || {};
    const hs = s.perSection ? s.harmSecs[sec.idx] : null, bs = s.perSection ? s.bassSecs[sec.idx] : null;
    const chords = (hs ? hs.progression || [] : s.chordsArr).map(lcChord);
    const bass = bs ? (bs.loop || []).map((n, k) => lcBassBar(n, bs.beats && bs.beats[k])) : s.chordsArr.map((n) => lcBassBar(n));
    const r = bs && typeof bs.rhythm === "string" && /[xo]/.test(bs.rhythm) ? bs.rhythm : null;
    // Default bass rhythm: the off-beat 8ths, [~ x]*4 in the Strudel sketch.
    const hits = r ? [...r.slice(0, s.nSteps).padEnd(s.nSteps, ".")].map((ch) => (ch === "." ? "." : "x")).join("")
      : [...Array(s.nSteps).keys()].map((i) => (i % 4 === 2 ? "x" : ".")).join("");
    const lanes = Object.entries(drums).filter(([, p]) => typeof p === "string" && /[xo]/.test(p.slice(0, s.nSteps)))
      .map(([lane, p]) => [lane, p.slice(0, s.nSteps).padEnd(s.nSteps, ".")]);
    const part = { lanes, chords, bass, hits }, body = JSON.stringify(part);
    let v = varOf(sec);
    // An edited section that differs from its letter's first section gets a part of its own, so the edit is heard.
    if (byName.has(v) && byName.get(v).body !== body && s.editedSecs.includes(sec)) v = `${v}_bar${sec.start_bar}`;
    partOf.set(sec, v);
    if (byName.has(v)) continue;
    const p = { v, body, ...part };
    byName.set(v, p);
    parts.push(p);
  }
  return { parts, partOf };
}

const lcEnd = (sec) => (sec.end_bar != null ? sec.end_bar : sec.start_bar);
const lcTitle = (c) => `${((c.source && c.source.title) || "Untitled").replace(/\s+/g, " ")}${c.source && c.source.artist ? " by " + c.source.artist.replace(/\s+/g, " ") : ""}`;
function lcAbout(s, tool) {
  const tempo = s.hasBpm ? `${s.bpm} BPM` : "tempo unknown, using 120 BPM";
  return [`Sketched by Tastr for ${tool}: ${tempo}, ${s.keyStr}. Not the original audio: the ${s.editedSecs.length ? "" : "detected "}drums,`,
    `bass and chords for each section, played for that section's length.`,
    ...(s.editedSecs.length ? [`With your edits in: ${[...new Set(s.editedSecs.map(sketchSecName))].join(", ")}.`] : [])];
}

// --- TidalCycles ---

// A bar of steps as Tidal mini-notation, which is the same as Strudel's: x...x...x...x... -> bd*4.
const tidalSteps = (steps, tok, beats) => compactSteps([...steps].map((ch) => (ch === "." ? "~" : tok(ch))), beats);
function tidalDrums(p, beats) {
  if (!p.lanes.length) return "silence";
  const lines = p.lanes.map(([lane, steps]) => {
    const snd = lcDrum(lane, "tidal");
    // Tidal's gain is a curve, not a level: 0.85 sounds about half as loud as 1.
    return `s "${tidalSteps(steps, () => snd, beats)}"${steps.includes("o") ? ` # gain "${tidalSteps(steps, (ch) => (ch === "o" ? "0.85" : "1"), beats)}"` : ""}`;
  });
  return `stack [${lines.join(", ")}]`;
}
const TIDAL_Q = { major: "major", minor: "minor", dom7: "dom7", major7: "major7", minor7: "minor7", dim: "dim", aug: "aug", sus2: "sus2", sus4: "sus4" };
function tidalChords(p) {
  const bars = p.chords.map((ch) => (ch ? `${tidalNote(ch, 4).replace(/\d+$/, "")}'${TIDAL_Q[ch.q]}` : "~"));
  return bars.some((x) => x !== "~") ? `n "${compactBars(bars)}"` : "silence";
}
function tidalBass(p, beats) {
  const bars = p.bass.map((notes) => {
    if (!notes.length) return "~";
    const runs = [];
    for (const n of notes.map((ch) => tidalNote(ch, lcBassOct(ch)))) {
      if (runs.length && runs[runs.length - 1].n === n) runs[runs.length - 1].k++;
      else runs.push({ n, k: 1 });
    }
    if (runs.length === 1) return runs[0].n;
    const even = runs.every((r) => r.k === runs[0].k);
    return `[${runs.map((r) => r.n + (even || r.k === 1 ? "" : "@" + r.k)).join(" ")}]`;
  });
  if (!bars.some((x) => x !== "~")) return "silence";
  return `struct "${tidalSteps(p.hits, () => "t", beats)}" $ n "${compactBars(bars)}"`;
}

export function tidalCode(c, edits = null) {
  const s = sketchInputs(c, null, edits), { parts, partOf } = livecodeParts(s);
  const out = [`-- ${lcTitle(c)}`, ...lcAbout(s, "TidalCycles").map((l) => `-- ${l}`),
    "-- Evaluate each block with Ctrl+Enter (Cmd+Enter on a Mac): it runs the lines around the cursor up to a blank line.",
    "-- hush stops everything. Needs SuperDirt running (the usual Tidal setup).", "",
    `setcps (${s.cpmBpm}/60/${s.beats}) -- 1 cycle = 1 bar${s.beats === 3 ? " of 3/4" : ""}${s.cpmBpm !== s.bpm ? " (tempo from the detected bar grid)" : ""}`, "",
    "-- Parts: one per section type (same letter = same part), each (drums, chords, bass).",
    "-- Song: (first cycle, end cycle, part) in the order they happen; it loops at the end."];
  parts.forEach((p, i) => {
    const pad = " ".repeat(p.v.length + 7);
    out.push(`${i ? "    " : "let "}${p.v} = ( ${tidalDrums(p, s.beats)}`, `${pad}, ${tidalChords(p)}`, `${pad}, ${tidalBass(p, s.beats)} )`);
  });
  out.push("    song layer = seqPLoop");
  let at = 0;
  s.song.forEach((sec, i) => {
    const bars = lcEnd(sec) - sec.start_bar + 1;
    out.push(`      ${i ? "," : "["} (${at}, ${at + bars}, layer ${partOf.get(sec)}) -- ${sketchSecName(sec)}, bars ${sec.start_bar}-${lcEnd(sec)}`);
    at += bars;
  });
  out.push("      ]", "    drumsOf (d, _, _) = d", "    chordsOf (_, c, _) = c", "    bassOf (_, _, b) = b", "",
    "-- Start from the top: sets the cycle count back to 0, the song's first bar", "resetCycles", "",
    "-- One line per layer: d1 drums, d2 bass, d3 chords. Run d2 silence to mute one, or change a sound and re-run.",
    "d1 $ song drumsOf", "", "d2 $ song bassOf # s \"supersaw\" # lpf 320 # gain 0.9", "",
    "d3 $ song chordsOf # s \"superpiano\" # gain 0.8 # room 0.3", "",
    "-- Ideas: d1 $ every 4 (fast 2) $ song drumsOf, or # crush 4 on the drums, or # lpf (range 200 2000 sine) on the bass.");
  return out.join("\n");
}

// --- Sonic Pi ---

const SONIC_Q = { major: "major", minor: "minor", dom7: "dom7", major7: "major7", minor7: "minor7", dim: "diminished", aug: "augmented", sus2: "sus2", sus4: "sus4" };
const sonicChord = (ch) => (ch ? `chord(${sonicNote(ch, 3)}, :${SONIC_Q[ch.q]})` : "nil");

export function sonicPiCode(c, edits = null) {
  const s = sketchInputs(c, null, edits), { parts, partOf } = livecodeParts(s), n = s.nSteps;
  const out = [`# ${lcTitle(c)}`, ...lcAbout(s, "Sonic Pi").map((l) => `# ${l}`),
    "# Paste into an empty buffer and press Run; Stop stops it. Change anything and press Run again.", "",
    `use_bpm ${s.cpmBpm} # one beat = a quarter note, ${s.beats} beats to the bar${s.cpmBpm !== s.bpm ? " (tempo from the detected bar grid)" : ""}`, "",
    "# Parts: one per section type (same letter = same part).",
    `# drums: a sample and its ${n} steps (x hit, o soft hit, . rest). chords: one per bar, looping.`,
    `# bass: the notes in each bar (one, or one per beat), looping, played on the x steps of hits.`,
    "parts = {"];
  for (const p of parts) {
    out.push(`  ${p.v}: {`,
      `    drums: [${p.lanes.map(([lane, steps]) => `[:${lcDrum(lane, "sonic")}, "${steps}"]`).join(", ")}],`,
      `    chords: [${p.chords.map(sonicChord).join(", ")}],`,
      `    bass: [${p.bass.map((notes) => `[${notes.map((ch) => sonicNote(ch, lcBassOct(ch))).join(", ")}]`).join(", ")}],`,
      `    hits: "${p.hits}",`, "  },");
  }
  out.push("}", "", "# Song: [bars, part] in the order they happen. Delete a line to skip that section, or reorder lines.", "song = [");
  for (const sec of s.song) out.push(`  [${lcEnd(sec) - sec.start_bar + 1}, :${partOf.get(sec)}], # ${sketchSecName(sec)}, bars ${sec.start_bar}-${lcEnd(sec)}`);
  out.push("]", "# the part playing in each bar, and the bar within that section (its chords and bass start over at each section)",
    "bars = song.flat_map { |count, part| count.times.map { |k| [part, k] } }", "",
    "# One loop per layer, one bar per pass. Add a # in front of a loop's lines (or Run with it deleted) to mute it.",
    "live_loop :drums do",
    "  p = parts[bars[tick % bars.length][0]]",
    `  ${n}.times do |i|`,
    "    p[:drums].each { |snd, steps| sample snd, amp: (steps[i] == \"o\" ? 0.5 : 1) if steps[i] != \".\" }",
    "    sleep 0.25",
    "  end",
    "end", "",
    "live_loop :bass do",
    "  part, bar = bars[tick % bars.length]",
    "  p = parts[part]",
    "  notes = p[:bass].empty? ? [] : p[:bass][bar % p[:bass].length]",
    "  use_synth :tb303",
    `  ${n}.times do |i|`,
    `    nt = notes[i * notes.length / ${n}]`,
    "    play nt, release: 0.2, cutoff: 70, amp: 0.6 if nt && p[:hits][i] == \"x\"",
    "    sleep 0.25",
    "  end",
    "end", "",
    "live_loop :chords do",
    "  part, bar = bars[tick % bars.length]",
    "  p = parts[part]",
    "  c = p[:chords].empty? ? nil : p[:chords][bar % p[:chords].length]",
    "  use_synth :blade",
    `  play c, sustain: ${s.beats - 0.5}, release: 0.5, amp: 0.3, cutoff: 90 if c`,
    `  sleep ${s.beats}`,
    "end", "",
    "# Ideas: wrap the drums in with_fx :reverb, change :tb303 to :fm, or use_bpm a little faster.");
  return out.join("\n");
}

