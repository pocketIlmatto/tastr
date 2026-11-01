// The sequencer (WebAudio, no samples), MIDI export and the drum pattern legend.

import { h } from "./core.js";
import { laneName } from "./groove-lanes.js";

export let actx = null, noiseBuf = null, player = null;
// Ableton-style number box: drag up or down to change the value (Shift for fine steps). A click without a drag still lets you type.
// opts.coarse: the step per drag notch without Shift (default: the input's step); opts.from(): the start value when the box is empty.
export function dragNumber(input, opts = {}) {
  const min = +input.min, max = +input.max, step = +input.step || 1, coarse = opts.coarse || step, dec = (String(step).split(".")[1] || "").length;
  input.classList.add("drag-num");
  input.title = "Drag up or down to change (Shift: fine). Click to type.";
  input.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || document.activeElement === input) return; // already typing: leave the caret alone
    const y0 = e.clientY, v0 = input.value === "" && opts.from ? opts.from() : +input.value || 0;
    let dragging = false;
    const move = (ev) => {
      const dy = y0 - ev.clientY;
      if (!dragging && Math.abs(dy) < 3) return;
      if (!dragging) { dragging = true; input.setPointerCapture(e.pointerId); document.body.classList.add("num-dragging"); }
      const v = +Math.max(min, Math.min(max, v0 + Math.round(dy / (ev.shiftKey ? 12 : 3)) * (ev.shiftKey ? step : coarse))).toFixed(dec);
      if (+input.value !== v) { input.value = String(v); input.dispatchEvent(new Event("input", { bubbles: true })); }
    };
    const up = () => {
      input.removeEventListener("pointermove", move); input.removeEventListener("pointerup", up); input.removeEventListener("pointercancel", up);
      document.body.classList.remove("num-dragging");
      if (dragging) input.dispatchEvent(new Event("change", { bubbles: true })); else { input.focus(); input.select(); }
    };
    e.preventDefault();
    input.addEventListener("pointermove", move); input.addEventListener("pointerup", up); input.addEventListener("pointercancel", up);
  });
}
export function audioCtx() {
  if (!actx) {
    actx = new (window.AudioContext || window.webkitAudioContext)();
    noiseBuf = actx.createBuffer(1, actx.sampleRate, actx.sampleRate);
    const d = noiseBuf.getChannelData(0);
    for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
  }
  return actx;
}
export function stopAllPlayback() { if (player) player.stop(); }
const isOpenHat = (l) => l.includes("hat") && l.includes("open");
export function voice(ctx, lane, t, vel) {
  const out = ctx.createGain(); out.connect(ctx.destination);
  const noise = (type, freq, dur, q) => {
    const n = ctx.createBufferSource(); n.buffer = noiseBuf;
    const f = ctx.createBiquadFilter(); f.type = type; f.frequency.value = freq; if (q) f.Q.value = q;
    n.connect(f); f.connect(out); n.start(t, Math.random() * 0.5); n.stop(t + dur + 0.02);
  };
  const tone = (type, f0, f1, dur) => {
    const o = ctx.createOscillator(); o.type = type; o.frequency.setValueAtTime(f0, t);
    o.frequency.exponentialRampToValueAtTime(f1, t + dur * 0.6); o.connect(out); o.start(t); o.stop(t + dur + 0.02);
  };
  const env = (peak, dur) => { out.gain.setValueAtTime(peak * vel, t); out.gain.exponentialRampToValueAtTime(0.001, t + dur); };
  const l = lane.toLowerCase();
  if (l.includes("kick")) { env(0.9, 0.35); tone("sine", 150, 45, 0.35); }
  else if (l.includes("snare")) { env(0.5, 0.2); noise("highpass", 1800, 0.2); tone("triangle", 190, 150, 0.12); }
  else if (l.includes("clap")) { env(0.5, 0.25); noise("bandpass", 1500, 0.25, 1.2); }
  else if (isOpenHat(l)) { env(0.25, 0.22); noise("highpass", 7000, 0.22); }
  else if (l.includes("hat")) { env(0.25, 0.045); noise("highpass", 7000, 0.045); }
  else if (l.includes("tom")) { env(0.6, 0.3); tone("sine", 170, 120, 0.3); }
  else if (l.includes("cymbal")) { env(0.2, 0.8); noise("highpass", 5000, 0.8); }
  else { env(0.4, 0.12); tone("sine", 800, 700, 0.12); }
}
// Lookahead scheduler: a 25 ms timer queues every 16th starting within the next 150 ms, looping `steps` 16ths.
// sched(ctx, t, step, stepDur) plays whatever lands on that step; it reads the edits and tempo each time, so changes apply live.
export function startLoop(sched, getBpm, steps, onStep, onEnd) {
  stopAllPlayback();
  const ctx = audioCtx(); if (ctx.state === "suspended") ctx.resume();
  let step = 0, next = ctx.currentTime + 0.06, alive = true, timers = [];
  const tick = () => {
    while (next < ctx.currentTime + 0.15) {
      const t = next, st = step, sd = 60 / getBpm() / 4;
      sched(ctx, t, st, sd);
      timers.push(setTimeout(() => alive && onStep(st), Math.max(0, (t - ctx.currentTime) * 1000)));
      if (timers.length > 64) timers = timers.slice(-32);
      next += sd; step = (step + 1) % steps;
    }
  };
  const iv = setInterval(tick, 25); tick();
  const me = { stop() { if (!alive) return; alive = false; clearInterval(iv); timers.forEach(clearTimeout); if (player === me) player = null; onEnd(); } };
  return (player = me);
}
export function bassVoice(ctx, midi, t, dur, vel) {
  const o = ctx.createOscillator(), f = ctx.createBiquadFilter(), g = ctx.createGain();
  o.type = "sawtooth"; o.frequency.value = 440 * 2 ** ((midi - 69) / 12);
  f.type = "lowpass"; f.Q.value = 6; f.frequency.setValueAtTime(900, t); f.frequency.exponentialRampToValueAtTime(220, t + dur);
  g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.35 * vel, t + 0.008); g.gain.exponentialRampToValueAtTime(0.001, t + dur);
  o.connect(f); f.connect(g); g.connect(ctx.destination); o.start(t); o.stop(t + dur + 0.02);
}
export function padVoice(ctx, midis, t, dur) {
  const g = ctx.createGain(), f = ctx.createBiquadFilter(); f.type = "lowpass"; f.frequency.value = 1800;
  g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.09, t + 0.05); g.gain.setValueAtTime(0.09, t + dur * 0.8); g.gain.exponentialRampToValueAtTime(0.001, t + dur);
  f.connect(g); g.connect(ctx.destination);
  for (const m of midis) for (const det of [-6, 6]) { const o = ctx.createOscillator(); o.type = "sawtooth"; o.detune.value = det; o.frequency.value = 440 * 2 ** ((m - 69) / 12); o.connect(f); o.start(t); o.stop(t + dur + 0.02); }
}
// General MIDI drum map (channel 10): kick 36, snare 38, clap 39, closed hat 42, open hat 46, low tom 45, perc (cowbell) 56, anything else 37.
export const GM_NOTE = (lane) => { const l = lane.toLowerCase(); return l.includes("kick") ? 36 : l.includes("snare") ? 38 : l.includes("clap") ? 39 : isOpenHat(l) ? 46 : l.includes("hat") ? 42 : l.includes("tom") ? 45 : l.includes("perc") ? 56 : l.includes("cymbal") ? 49 : 37; };
// Format 1, 480 PPQN (120 ticks per 16th, 1920 per 4/4 bar): a tempo and time signature track, then one track per part,
// as [name, channel, events {t, d, note, vel}]. `beats` is the time signature's top number (4 for 4/4, 3 for 3/4).
export function midiMulti(tracks, bpm, ticks, beats = 4) {
  const vlq = (n) => { const o = [n & 127]; while ((n >>= 7)) o.unshift((n & 127) | 128); return o; };
  const chunk = (data) => [0x4d, 0x54, 0x72, 0x6b, data.length >> 24 & 255, data.length >> 16 & 255, data.length >> 8 & 255, data.length & 255, ...data];
  const us = Math.round(60e6 / bpm);
  const out = [0x4d, 0x54, 0x68, 0x64, 0, 0, 0, 6, 0, 1, 0, tracks.length + 1, 1, 0xe0,
    ...chunk([0, 0xff, 0x51, 3, us >> 16 & 255, us >> 8 & 255, us & 255, 0, 0xff, 0x58, 4, beats, 2, 24, 8, ...vlq(ticks), 0xff, 0x2f, 0])];
  for (const [name, ch, events] of tracks) {
    const nm = [...name].map((c) => c.charCodeAt(0) & 127), ev = [];
    for (const e of events) ev.push([e.t, 1, [0x90 | ch, e.note, e.vel]], [e.t + e.d, 0, [0x80 | ch, e.note, 0]]);
    ev.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const tr = [0, 0xff, 0x03, nm.length, ...nm]; let last = 0;
    ev.forEach(([t, , bytes]) => { tr.push(...vlq(t - last), ...bytes); last = t; });
    tr.push(...vlq(Math.max(0, ticks - last)), 0xff, 0x2f, 0);
    out.push(...chunk(tr));
  }
  return new Uint8Array(out);
}
export function downloadBytes(bytes, fileName, type) {
  const url = URL.createObjectURL(new Blob([bytes], { type }));
  const a = h("a", { href: url, download: fileName });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function diffText(pattern, prev) {
  const parts = [];
  for (const lane of Object.keys(pattern)) {
    if (!prev || typeof prev[lane] !== "string") continue;
    let add = 0, drop = 0;
    [...pattern[lane]].forEach((ch, i) => { const was = prev[lane][i] !== "."; const is = ch !== "."; if (is && !was) add++; if (!is && was) drop++; });
    if (add || drop) parts.push(`${laneName(lane)} ${[add ? "+" + add : "", drop ? "−" + drop : ""].filter(Boolean).join(" ")}`);
  }
  return parts.length ? parts.join(", ") : "same as previous section";
}

const TPL_TIPS = ["A hit on this step in the template.", "A lighter hit in the template, such as an accent or open hat.", "No hit on this step."];
// `steps` is the bar's step count (16 in 4/4, 12 in 3/4), or false to leave out the "1 bar, N steps" item.
export const legend = (measured = true, steps = 16, chg = true) => h("div", { class: "grid-legend" },
  h("span", { tabindex: "0", "data-tip": !measured ? TPL_TIPS[0] : "Strong: at least 72% of the loudest hit in this lane." }, h("i", { class: "sw full" }), measured ? "Strong" : "Hit"),
  h("span", { tabindex: "0", "data-tip": !measured ? TPL_TIPS[1] : "Softer: 50% to 72% of the loudest hit in this lane. Often a ghost note, a quieter hat, or a hit that only lands in some bars." }, h("i", { class: "sw half" }), measured ? "Softer" : "Lighter"),
  h("span", { tabindex: "0", "data-tip": !measured ? TPL_TIPS[2] : "None: under 50% of the loudest hit in this lane, so treated as silence." }, h("i", { class: "sw" }), "None"),
  !chg ? null : h("span", { tabindex: "0", "data-tip": "A dark inner outline marks a step that differs from the same step in the previous section. It's not an edit. Turn it on or off with the Δ checkbox next to this legend." }, h("i", { class: "sw chg" }), "Δ"),
  h("span", { tabindex: "0", "data-tip": "During playback, a ring in the accent color moves along the steps, notes and chords being played." }, h("i", { class: "sw playing" }), "Playing"),
  steps ? h("span", { tabindex: "0", "data-tip": `The grid covers one typical bar of the music, split into ${steps} equal steps from left to right. Every group of 4 steps is one beat (${steps / 4} beats: ${steps / 4}/4 time). Each lane (kick, snare, hat) shows which of those steps it plays on.` }, `1 bar, ${steps} steps`) : null);

// Mirrors _style() in the suggestions stage: which tempo range picks which template style.
const STYLE_INFO = {
  four_floor: ["118-136 BPM", "Four on the floor means a kick on every beat, typical of house and techno."],
  dnb: ["160-185 BPM", "Drum and bass leans on a fast, syncopated kick and snare."],
  halftime: ["136-152 BPM with a percussive track", "Halftime puts the snare on beat 3 so the groove feels half as fast."],
  hiphop: ["80-105 BPM", "Hip-hop grooves sit on a laid-back kick and backbeat snare."],
  generic: ["any other tempo", "A plain backbeat: kick on 1, snare on 2 and 4."],
  waltz: ["3/4 time", "A waltz counts in 3: a kick on 1, lighter hits on 2 and 3."],
};
export const styleTip = (d) => {
  const [range, what] = STYLE_INFO[d.style_key] || ["", ""];
  if (d.style_key === "waltz") return `${what} Chosen because this track was read as 3/4, at any tempo. The track's actual rhythm was not checked.`;
  return `${what} Chosen only because this track is ${d.bpm} BPM and ${range} maps to this style. The track's actual rhythm was not checked.`.trim();
};

export function setPlayer(v) { return (player = v); }
