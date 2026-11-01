// The chart's transport: playing the track from the playhead, seeking, and audible scrubbing while you drag the playhead.

import { mmss } from "./core.js";
import { actx, audioCtx, player, setPlayer, stopAllPlayback } from "./sequencer.js";
import { T } from "./chart-draw.js";
import { loopPos } from "./chart-math.js";

const stopSrc = (s, when) => { try { s.stop(when); } catch (_) { /* already stopped */ } };

// Mono reversed copy, for scrubbing backwards: half the memory of a stereo one, and fine for hearing where you are.
function reversed(ctx, b) {
  const n = b.length, chans = [...Array(b.numberOfChannels)].map((_, k) => b.getChannelData(k)), r = new Float32Array(n);
  for (const ch of chans) for (let i = 0; i < n; i++) r[i] += ch[n - 1 - i] / chans.length;
  const rev = ctx.createBuffer(1, n, b.sampleRate); rev.copyToChannel(r, 0);
  return rev;
}

// Playback for one chart (c is the chart's state, see chart.js).
export function transport(c) {
  let pos = 0, src = null, startedAt = 0, playing = false, raf = 0;
  const loopRange = () => (c.loop.on ? [c.loop.a, c.loop.b] : null);
  const tNow = () => (playing ? loopPos(actx.currentTime - startedAt, loopRange(), c.end) : pos);
  function paintHead() {
    const ph = c.svgEl.querySelector(".playhead"); if (!ph) return;
    const t = tNow(), vis = t >= c.view.a && t <= c.view.b, x = c.x(t);
    ph.setAttribute("x1", x); ph.setAttribute("x2", x); ph.setAttribute("opacity", vis ? 0.9 : 0);
    const kn = c.svgEl.querySelector(".playhead-knob"); kn.setAttribute("transform", `translate(${x},${T - 9})`); kn.setAttribute("opacity", vis ? 1 : 0);
    c.seekR.value = String(t); c.timeL.textContent = `${mmss(t)} / ${mmss(c.duration)}`;
  }
  function startSrc() {
    const ctx = audioCtx(); if (ctx.state === "suspended") ctx.resume();
    if (src) { src.onended = null; stopSrc(src); }
    src = ctx.createBufferSource(); src.buffer = c.audio.buf; src.connect(c.live.analyser()); // the analyser passes the sound on to the speakers
    const lr = loopRange();
    if (lr) { if (pos < lr[0] || pos >= lr[1]) pos = lr[0]; src.loop = true; src.loopStart = lr[0]; src.loopEnd = lr[1]; } // gapless: the browser loops the buffer itself
    src.onended = () => { if (playing) { me.stop(); pos = 0; paintHead(); } };
    src.start(0, pos); startedAt = ctx.currentTime - pos;
  }
  // Registered as the shared `player`, so the drum sequencer and track playback stop each other.
  const me = { stop() {
    if (!playing) return;
    pos = tNow(); playing = false; cancelAnimationFrame(raf);
    if (src) { src.onended = null; stopSrc(src); src = null; }
    if (player === me) setPlayer(null);
    c.playBtn.textContent = "▶"; c.playBtn.setAttribute("aria-label", "Play"); paintHead();
  } };
  function loop() {
    if (!playing) return;
    if (!c.svgEl.isConnected) { me.stop(); return; }
    const t = tNow(), v = c.view;
    // When zoomed in and the playhead leaves the view, page so it sits 10% from the left. Live keeps "now" at the right edge.
    if (c.mode === "live") { c.live.frame(t); c.live.follow(t); c.draw(); }
    else if (t > v.b || t < v.a) { const span = v.b - v.a; v.a = Math.max(0, Math.min(c.end - span, t - span * 0.1)); v.b = v.a + span; c.draw(); }
    else paintHead();
    raf = requestAnimationFrame(loop);
  }
  async function play() {
    if (!(await c.ensureAudio()) || !c.svgEl.isConnected) return;
    stopAllPlayback();
    if (pos >= c.end - 0.2) pos = 0;
    startSrc(); playing = true; setPlayer(me); c.liveHint.hidden = true;
    c.playBtn.textContent = "❚❚"; c.playBtn.setAttribute("aria-label", "Pause"); loop();
  }
  const toggle = () => { if (playing) me.stop(); else play(); };
  c.playBtn.onclick = toggle;
  function seek(t) {
    pos = Math.max(0, Math.min(c.end, t));
    if (playing) startSrc();
    if (c.mode === "live" && !playing) { c.live.follow(pos); c.draw(); }
    paintHead();
  }
  c.seekR.addEventListener("input", () => seek(+c.seekR.value));

  // ---- audible scrubbing: the play position chases the pointer, speed follows the drag (backwards uses a reversed copy) ----
  let scrub = null;
  function scrubStart() {
    scrub = { pos, target: pos, dir: 0, src: null, g: null, last: performance.now(), raf: 0 };
    const s = scrub;
    if (c.audio) scrubAudio(s);
    else c.ensureAudio().then(() => { if (scrub === s && c.audio) scrubAudio(s); });
    scrubLoop();
  }
  function scrubAudio(s) {
    const ctx = audioCtx(); if (ctx.state === "suspended") ctx.resume();
    if (!c.audio.rev) c.audio.rev = reversed(ctx, c.audio.buf);
    s.g = ctx.createGain(); s.g.gain.value = 0; s.g.connect(ctx.destination);
  }
  function scrubSrc(dir) {
    const ctx = audioCtx();
    if (scrub.src) stopSrc(scrub.src);
    const sn = ctx.createBufferSource(); sn.buffer = dir > 0 ? c.audio.buf : c.audio.rev; sn.playbackRate.value = 0.0001; sn.connect(scrub.g);
    sn.start(0, Math.max(0, dir > 0 ? scrub.pos : c.audio.buf.duration - scrub.pos)); scrub.src = sn; scrub.dir = dir;
  }
  // Point the scrub source the way it's moving, at this speed, and fade it out when it stops.
  function scrubSteer(rate) {
    const ctx = audioCtx(), dir = rate > 0 ? 1 : rate < 0 ? -1 : scrub.dir;
    if (dir && dir !== scrub.dir) scrubSrc(dir);
    if (scrub.src) scrub.src.playbackRate.setTargetAtTime(Math.max(0.0001, Math.abs(rate)), ctx.currentTime, 0.015);
    scrub.g.gain.setTargetAtTime(rate ? 0.9 : 0, ctx.currentTime, 0.02);
  }
  function scrubLoop() {
    if (!scrub) return;
    const now = performance.now(), dt = Math.min(0.05, (now - scrub.last) / 1000); scrub.last = now;
    const gap = scrub.target - scrub.pos;
    if (Math.abs(gap) > 1.5) { scrub.pos = scrub.target - Math.sign(gap) * 0.1; if (scrub.g) scrubSrc(Math.sign(gap)); }
    let rate = Math.max(-8, Math.min(8, (scrub.target - scrub.pos) / 0.08));
    if (Math.abs(rate) < 0.04) rate = 0;
    if (scrub.g) scrubSteer(rate);
    scrub.pos = Math.max(0, Math.min(c.end, scrub.pos + rate * dt)); pos = scrub.pos; paintHead();
    scrub.raf = requestAnimationFrame(scrubLoop);
  }
  function scrubEnd() {
    if (!scrub) return;
    cancelAnimationFrame(scrub.raf);
    if (scrub.src) stopSrc(scrub.src, audioCtx().currentTime + 0.03);
    if (scrub.g) scrub.g.gain.setTargetAtTime(0, audioCtx().currentTime, 0.01);
    pos = scrub.pos; scrub = null;
  }
  const scrubTo = (t) => { if (scrub) scrub.target = t; };
  // After the loop changes: keep playing from where it is, with (or without) the new loop.
  const restart = () => { if (playing) { pos = tNow(); startSrc(); } };
  return { tNow, paintHead, play, toggle, seek, restart, stop: () => me.stop(), isPlaying: () => playing, scrubStart, scrubTo, scrubEnd };
}
