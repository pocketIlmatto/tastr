"""Write listen.html into a stem_ab.py output folder: a page for blind A/B listening (docs/stems-tuning.md).

    python scripts/stem_ab_page.py stem-ab/out

Open OUT/listen.html in Chrome or Safari. It plays the FLAC files next to it (nothing is uploaded), keeps all
four letters in sync so switching is instant, and lets you mark the best and worst letter per track. The page
never reads answers.json; "Copy my picks" puts your picks on the clipboard to paste back.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

STEMS = ["vocals", "drums", "bass", "other"]

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stem listening test</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1f;--mute:#6b6b70;--line:#dddbd5;--acc:#2f6fdf;--best:#1f8a4c;--worst:#c0392b}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1e1e21;--ink:#ececee;--mute:#9a9aa1;--line:#333338;--acc:#6d9bff;--best:#3fbf74;--worst:#ff6b5e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}
.help{color:var(--mute);margin:0 0 20px}.help kbd{border:1px solid var(--line);border-radius:4px;padding:0 5px;font-size:12px}
.track{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
.track.on{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc)}
.track h2{font-size:16px;margin:0 0 12px}.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
.lbl{color:var(--mute);font-size:13px;width:52px}
button{font:inherit;border:1px solid var(--line);background:transparent;color:var(--ink);border-radius:8px;padding:6px 12px;cursor:pointer}
button.sel{background:var(--acc);border-color:var(--acc);color:#fff}
.letter{min-width:46px;font-weight:600}.play{min-width:84px}
input[type=range]{flex:1;min-width:160px;accent-color:var(--acc)}.time{color:var(--mute);font-variant-numeric:tabular-nums;font-size:13px}
.pick button.best{background:var(--best);border-color:var(--best);color:#fff}.pick button.worst{background:var(--worst);border-color:var(--worst);color:#fff}
.bar{position:sticky;bottom:0;background:var(--bg);padding:12px 0;display:flex;gap:10px;align-items:center;border-top:1px solid var(--line)}
#copied{color:var(--mute);font-size:13px}
</style></head><body><main>
<h1>Stem listening test</h1>
<p class="help">Each track was split four ways; the letters are shuffled per track. Pick a stem, press play, and switch letters while it plays: they stay in sync.
Keys for the highlighted track: <kbd>space</kbd> play/pause, <kbd>1</kbd>–<kbd>4</kbd> letter, <kbd>v</kbd> <kbd>d</kbd> <kbd>b</kbd> <kbd>o</kbd> stem, <kbd>←</kbd> <kbd>→</kbd> 5 s.</p>
<div id="tracks"></div>
<div class="bar"><button id="copy">Copy my picks</button><span id="copied"></span></div>
</main>
<script>
const DATA = __DATA__, STEMS = __STEMS__;
const KEY = "stem-ab-picks";
let picks = {}; try { picks = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch {}
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(picks)); } catch {} };
const fmt = s => isFinite(s) ? `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}` : "0:00";
const el = (tag, attrs = {}, ...kids) => { const e = document.createElement(tag); Object.assign(e, attrs); e.append(...kids); return e; };
const players = [];
let active = null;

function makePlayer(t) {
  const st = { stem: "vocals", letter: t.letters[0], audios: {}, playing: false };
  const box = el("section", { className: "track" });
  const stemRow = el("div", { className: "row" }, el("span", { className: "lbl", textContent: "Stem" }));
  const letRow = el("div", { className: "row" }, el("span", { className: "lbl", textContent: "Version" }));
  const play = el("button", { className: "play", textContent: "▶ Play" });
  const seek = el("input", { type: "range", min: 0, max: 1000, value: 0 });
  const time = el("span", { className: "time", textContent: "0:00 / 0:00" });
  const pick = el("div", { className: "row pick" });
  const stemBtns = {}, letBtns = {};
  STEMS.forEach(s => { const b = el("button", { textContent: s }); b.onclick = () => setStem(s); stemBtns[s] = b; stemRow.append(b); });
  t.letters.forEach((l, i) => { const b = el("button", { className: "letter", textContent: l, title: `key ${i + 1}` }); b.onclick = () => setLetter(l); letBtns[l] = b; letRow.append(b); });
  box.append(el("h2", { textContent: t.name }), stemRow, letRow, el("div", { className: "row" }, play, seek, time), pick);

  const cur = () => st.audios[st.letter];
  function load() {
    Object.values(st.audios).forEach(a => { a.pause(); a.removeAttribute("src"); a.load(); });
    st.audios = {};
    t.letters.forEach(l => {
      const a = new Audio(encodeURI(`${t.dir}/${l}/${st.stem}.flac`).replace(/#/g, "%23"));
      a.preload = "auto"; a.muted = l !== st.letter;
      a.ontimeupdate = () => { if (l === st.letter) tick(); };
      a.onended = () => { if (l === st.letter) stop(); };
      a.onloadedmetadata = () => { if (l === st.letter) tick(); };
      st.audios[l] = a;
    });
  }
  function tick() { const a = cur(); if (!a) return; seek.value = a.duration ? a.currentTime / a.duration * 1000 : 0; time.textContent = `${fmt(a.currentTime)} / ${fmt(a.duration)}`; }
  function syncTo(t0) { Object.values(st.audios).forEach(a => { try { a.currentTime = t0; } catch {} }); }
  function start() { players.forEach(p => p !== api && p.stop()); const t0 = cur().currentTime; syncTo(t0); Object.values(st.audios).forEach(a => a.play().catch(() => {})); st.playing = true; play.textContent = "❚❚ Pause"; focus(); }
  function stop() { Object.values(st.audios).forEach(a => a.pause()); st.playing = false; play.textContent = "▶ Play"; }
  function setLetter(l) { const t0 = cur().currentTime; st.letter = l; Object.entries(st.audios).forEach(([k, a]) => { a.muted = k !== l; }); syncTo(t0); paint(); focus(); }
  function setStem(s) { const t0 = cur() ? cur().currentTime : 0, was = st.playing; stop(); st.stem = s; load(); const go = () => { syncTo(t0); if (was) start(); tick(); }; cur().addEventListener("loadedmetadata", go, { once: true }); paint(); focus(); }
  function nudge(d) { const a = cur(); syncTo(Math.max(0, Math.min((a.duration || 0) - 0.1, a.currentTime + d))); tick(); }
  function paint() {
    STEMS.forEach(s => stemBtns[s].classList.toggle("sel", s === st.stem));
    t.letters.forEach(l => letBtns[l].classList.toggle("sel", l === st.letter));
    const p = picks[t.name] || {};
    pick.replaceChildren(el("span", { className: "lbl", textContent: "Best" }),
      ...t.letters.map(l => { const b = el("button", { className: "letter" + (p.best === l ? " best" : ""), textContent: l }); b.onclick = () => mark("best", l); return b; }),
      el("span", { className: "lbl", textContent: "Worst", style: "margin-left:16px" }),
      ...t.letters.map(l => { const b = el("button", { className: "letter" + (p.worst === l ? " worst" : ""), textContent: l }); b.onclick = () => mark("worst", l); return b; }),
      (() => { const b = el("button", { textContent: "Can't tell", className: p.cant ? "sel" : "" }); b.onclick = () => { picks[t.name] = p.cant ? {} : { cant: true }; save(); paint(); }; return b; })());
  }
  function mark(kind, l) { const p = picks[t.name] && !picks[t.name].cant ? picks[t.name] : {}; p[kind] = p[kind] === l ? undefined : l; picks[t.name] = p; save(); paint(); }
  function focus() { players.forEach(p => p.box.classList.toggle("on", p === api)); active = api; }
  play.onclick = () => st.playing ? stop() : start();
  seek.oninput = () => { const a = cur(); if (a.duration) syncTo(seek.value / 1000 * a.duration); tick(); };
  box.addEventListener("pointerdown", focus);
  const api = { box, stop, start, setLetter, setStem, nudge, st, t };
  load(); paint();
  return api;
}

DATA.forEach(t => { const p = makePlayer(t); players.push(p); document.getElementById("tracks").append(p.box); });
active = players[0]; if (active) active.box.classList.add("on");
document.addEventListener("keydown", e => {
  if (!active || e.metaKey || e.ctrlKey || e.altKey || e.target.tagName === "INPUT") return;
  const k = e.key.toLowerCase(), stems = { v: "vocals", d: "drums", b: "bass", o: "other" };
  if (k === " ") { e.preventDefault(); active.st.playing ? active.stop() : active.start(); }
  else if (/^[1-9]$/.test(k) && active.t.letters[+k - 1]) active.setLetter(active.t.letters[+k - 1]);
  else if (stems[k]) active.setStem(stems[k]);
  else if (k === "arrowleft") active.nudge(-5);
  else if (k === "arrowright") active.nudge(5);
});
document.getElementById("copy").onclick = async () => {
  const lines = DATA.map(t => { const p = picks[t.name] || {}; return `${t.name}: ` + (p.cant ? "can't tell" : `best ${p.best || "-"}, worst ${p.worst || "-"}`); });
  const text = lines.join("\n");
  try { await navigator.clipboard.writeText(text); document.getElementById("copied").textContent = "Copied. Paste it in the thread."; }
  catch { prompt("Copy this:", text); }
};
</script></body></html>
"""


def build(out: Path) -> Path:
    tracks = []
    for d in sorted(p for p in out.iterdir() if p.is_dir()):
        letters = sorted(x.name for x in d.iterdir() if x.is_dir() and all((x / f"{s}.flac").is_file() for s in STEMS))
        if letters:
            tracks.append({"name": d.name, "dir": d.name, "letters": letters})
    if not tracks:
        raise SystemExit(f"No track folders with stems in {out}")
    page = out / "listen.html"
    page.write_text(PAGE.replace("__DATA__", json.dumps(tracks)).replace("__STEMS__", json.dumps(STEMS)))
    return page


if __name__ == "__main__":
    print(build(Path(sys.argv[1] if len(sys.argv) > 1 else "stem-ab/out")))
