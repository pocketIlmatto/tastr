// The label bar at the bottom of the card head: is the analysis right? Closed, a one-line summary; open, the label panel
// (BPM, key, section starts and a note), saved as your label (tastr/labels.py). The label state lives in labels.js.

import { api, h, isNum, labels, reviewRows } from "./core.js";
import { camelotNear } from "./library.js";
import { hearChordDb, hearDrums, hearKey, hearing, setHearChordDb, setHearDrums } from "./card.js";
import { accid, keyName } from "./daw.js";
import { audioCtx, dragNumber } from "./sequencer.js";
import { setStatus } from "./upload.js";
import { ALL_CAMS, CAM_NAME, JSON_PUT, camLabel, charts, countReviewed, endMarking, fmtMark, labelOpen, loadReview, markHook, nameToCam, openForReview,
  parseTimes, repaintLabel, reviewOn, reviewRow, setInReview, setLabelOpen, setMarkHook, setRepaintLabel, snapBar } from "./labels.js";

const flash = (el) => { el.classList.remove("lb-flash"); void el.offsetWidth; el.classList.add("lb-flash"); };
const BPM_HINT = "Tastr often lands on half or double time. Tap along if you're not sure.";

// ---- closed: a one-line summary ----
function summary(lb) {
  const { c } = lb, lab = labels.get(c.id), why = reviewRow(c.id);
  const open = () => { setLabelOpen(c.id); lb.paint(); };
  const rvBtn = why ? h("button", { type: "button", class: "linklike", title: "Take it out of the review list", onclick: () => setInReview([c.id], false) }, "Remove from review")
    : h("button", { type: "button", class: "linklike", title: "Put it in the review list to check later", onclick: () => setInReview([c.id], true) }, "Add to review");
  if (!lab) return [h("span", {}, why ? (why.mine ? "You added this to review. " : "Tastr isn't sure about this one. ") : "Is this analysis right? "),
    h("button", { type: "button", onclick: open }, why ? "Check it" : "Label it"),
    lb.canKeep ? h("button", { type: "button", title: `Label it as right: ${lb.tastrSay}`, onclick: () => keepAsIs(lb, "") }, "✓ Keep as is") : null, rvBtn];
  const lcam = lab.key_name ? nameToCam(lab.key_name) : null;
  const bits = [lab.bpm ? `${lab.bpm} BPM` : null, lab.key_name ? (lcam ? camLabel(lcam) : lab.key_name) : null, lab.boundaries ? `${lab.boundaries.split(" ").length} section starts` : null].filter(Boolean);
  return [h("span", { class: "lb-done" }, lab.kept ? "✓ Kept as is" : "✓ Your label"), h("span", {}, bits.join(" · ") || "note only"),
    h("button", { type: "button", onclick: open }, "Edit"), why ? rvBtn : null];
}

// After a save: refresh the list and log, then open the next track in review (Save & next, or Keep as is in Review) or show the summary.
async function saved(lb, next) {
  const { c } = lb;
  setLabelOpen(null); endMarking(c.id);
  await loadReview();
  const nxt = next ? reviewRows.find((r) => r.track_id !== c.id) : null;
  if (nxt) openForReview(nxt.track_id); else lb.paint();
}
// "Keep as is": Tastr got it right. Stored as a label with Tastr's own BPM and key, so it scores future engine changes too.
async function keepAsIs(lb, note, err) {
  const { c, hasBpm, tBpm, cam0 } = lb, had = labels.has(c.id);
  try { await api(`/api/tracks/${c.id}/labels`, JSON_PUT({ ...(hasBpm ? { bpm: tBpm } : {}), ...(cam0 ? { key_name: CAM_NAME(cam0) } : {}), ...(note ? { note } : {}), kept: true })); }
  catch (e) { if (err) err.textContent = e.message; else setStatus(e.message, true); return; }
  if (!had) countReviewed();
  saved(lb, reviewOn);
}

// ---- open: the label panel. p is the panel's state: st holds the values Save will store. ----
// What Save will store, kept in view so every pick, drag or tap shows where it lands.
function paintSum(p) {
  const { st } = p, cam = nameToCam(st.key), nB = String(st.bounds).trim() ? String(st.bounds).trim().split(/[;\s,]+/).length : 0;
  const parts = [st.bpm !== "" ? h("b", {}, `${st.bpm} BPM`) : null, cam ? h("b", {}, camLabel(cam)) : st.key ? h("b", {}, st.key) : null,
    nB ? h("b", {}, `${nB} section start${nB === 1 ? "" : "s"}`) : null, st.note.trim() ? "a note" : null].filter(Boolean);
  p.sumEl.replaceChildren(...(parts.length ? ["Will save: ", ...parts.flatMap((x, i) => (i ? [" · ", x] : [x]))] : ["Nothing set yet. Pick, type, drag or tap a value."]));
}

// Tap along to the beat: from the third tap the BPM follows the taps.
function tapButton(p, setBpm, bpmHint) {
  let taps = [], tapTimer = 0;
  const tap = h("button", { type: "button", class: "lb-tap", title: "Tap along to the beat" }, "Tap");
  const tapDone = () => { if (taps.length > 2) { bpmHint.replaceChildren(h("b", {}, `Set to ${p.st.bpm} BPM from your ${taps.length} taps.`), " Tap again to start over."); tap.textContent = "Tap"; } };
  tap.onclick = () => {
    const now = performance.now(); if (taps.length && now - taps[taps.length - 1] > 2000) taps = []; taps.push(now); taps = taps.slice(-9);
    clearTimeout(tapTimer); tapTimer = setTimeout(tapDone, 2000);
    if (taps.length > 2) {
      const iv = (taps[taps.length - 1] - taps[0]) / (taps.length - 1), v = +(60000 / iv).toFixed(1);
      setBpm(v, "tap"); tap.textContent = `${v} ♩`;
      bpmHint.replaceChildren(h("b", {}, `Tapping: ${v} BPM → your label BPM.`), ` Keep tapping to steady it (${taps.length} taps).`);
    } else { tap.textContent = "Tap…"; bpmHint.replaceChildren(taps.length === 1 ? "Keep tapping on the beat…" : "One more…"); }
  };
  return tap;
}

// BPM: confirm, half/double, type or drag it, or tap it
function bpmRow(p) {
  const { st, lb } = p, { core, hasBpm, tBpm } = lb;
  const bpmIn = h("input", { type: "number", min: "30", max: "300", step: "0.1", value: String(st.bpm), "aria-label": "Your BPM", placeholder: "BPM" });
  const bpmVals = hasBpm ? [core.bpm, core.bpm / 2, core.bpm * 2] : [];
  const bpmBtns = bpmVals.map((v, i) => h("button", { type: "button", "aria-pressed": "false", title: i ? `${v.toFixed(1)} BPM` : `Keep Tastr's ${v.toFixed(1)}`,
    onclick: () => setBpm(+v.toFixed(1)) }, i ? `${["", "½", "×2"][i]} ${v.toFixed(1)}` : `✓ ${v.toFixed(1)}`));
  p.syncBpm = () => bpmBtns.forEach((b, i) => b.setAttribute("aria-pressed", String(st.bpm !== "" && Math.abs(+st.bpm - bpmVals[i]) < 0.06)));
  const bpmHint = h("span", { class: "lb-hint", "aria-live": "polite" }, BPM_HINT);
  function setBpm(v, from) { st.bpm = v; bpmIn.value = String(v); p.syncBpm(); if (from !== "tap") bpmHint.replaceChildren(BPM_HINT); flash(bpmIn); p.sum(); }
  bpmIn.addEventListener("input", () => { st.bpm = bpmIn.value; p.syncBpm(); p.sum(); });
  dragNumber(bpmIn, { coarse: 1, from: () => tBpm ?? 120 }); // the drum player's drag box: a whole BPM per notch, Shift for 0.1
  const tap = tapButton(p, setBpm, bpmHint);
  return h("div", { class: "lb-row" }, h("b", {}, "BPM"), h("div", { class: "lb-ctl" }, ...bpmBtns, bpmIn, tap, bpmHint));
}

// Leave the drums out of 🔊; replays what's playing, now with or without drums.
function drumsToggle() {
  const drumsBtn = h("button", { type: "button", class: "lb-drums", "aria-pressed": String(!hearDrums), title: "Leave the drums out of 🔊 so the chords are easier to hear" }, hearDrums ? "🥁 Drums on" : "🥁 Drums off");
  drumsBtn.onclick = () => {
    setHearDrums(!hearDrums); drumsBtn.setAttribute("aria-pressed", String(!hearDrums)); drumsBtn.textContent = hearDrums ? "🥁 Drums on" : "🥁 Drums off";
    if (hearing) { const hh = hearing; hh.stop(); hearKey(hh.c, hh.cam, hh.btn); }
  };
  return drumsBtn;
}
// the chord's level against the track; changes what's playing right away
function chordVolume() {
  const volOut = h("output", {}, ""), volIn = h("input", { type: "range", min: "-18", max: "12", step: "1", value: String(hearChordDb), "aria-label": "Chord volume against the track" });
  const volTxt = () => { volOut.textContent = hearChordDb === 0 ? "even" : `${hearChordDb > 0 ? "+" : ""}${hearChordDb} dB`; };
  volIn.addEventListener("input", () => { setHearChordDb(+volIn.value); volTxt(); if (hearing?.lvl) hearing.lvl.gain.setTargetAtTime(Math.pow(10, hearChordDb / 20), audioCtx().currentTime, 0.03); });
  volIn.addEventListener("dblclick", () => { volIn.value = "0"; volIn.dispatchEvent(new Event("input")); });
  volTxt();
  return h("label", { class: "lb-vol", title: "How loud the 🔊 chord is against the track (double-click to reset)" }, "Chord", h("span", { class: "muted" }, "quieter"), volIn, h("span", { class: "muted" }, "louder"), volOut);
}

// Key: confirm, the usual near misses, or pick any of the 24; 🔊 plays each against the track
function keyRow(p) {
  const { st, lb, c, why } = p, { key, cam0 } = lb;
  const near = cam0 ? camelotNear(cam0) : [];
  const keyCams = cam0 ? [cam0, near[3], near[2], near[1]] : [];
  const keyBtns = keyCams.map((cam, i) => h("button", { type: "button", "aria-pressed": "false", title: `${cam} · ${["Keep Tastr's key", "Same notes, other mode", "Tastr's most common miss", "Tastr's most common miss"][i]}`,
    onclick: () => setKey(CAM_NAME(cam)) }, i ? `${["", "Relative", "Fifth up", "Fifth down"][i]}: ${accid(CAM_NAME(cam))}` : `✓ ${keyName(key)}`,
    why && why.catalog_key === cam ? h("span", { class: "lb-cat", title: "What the catalog lists" }, "catalog") : null));
  const hearBtn = (camOf, label) => { const b = h("button", { type: "button", class: "lb-hear", title: `Hear ${label()} against the track`, "aria-label": `Hear ${label()} against the track` }, "🔊"); b.onclick = () => hearKey(c, camOf(), b); return b; };
  const drumsBtn = drumsToggle(), chordVol = chordVolume();
  const keySel = h("select", { "aria-label": "Your key" }, h("option", { value: "" }, "Other key…"), ALL_CAMS.map((cam) => h("option", { value: CAM_NAME(cam) }, `${cam} · ${accid(CAM_NAME(cam))}`)));
  const otherHear = hearBtn(() => nameToCam(keySel.value), () => "the picked key");
  p.syncKey = () => {
    const cam = nameToCam(st.key);
    keyBtns.forEach((b, i) => b.setAttribute("aria-pressed", String(cam === keyCams[i])));
    keySel.value = cam && !keyCams.includes(cam) ? CAM_NAME(cam) : "";
    otherHear.disabled = !keySel.value;
    const tt = keySel.value ? `Hear ${accid(keySel.value)} against the track` : "Pick a key in the list first, then hear it against the track";
    otherHear.title = tt; otherHear.setAttribute("aria-label", tt);
  };
  function setKey(v) { st.key = v; p.syncKey(); p.sum(); }
  keySel.addEventListener("change", () => setKey(keySel.value));
  return h("div", { class: "lb-row" }, h("b", {}, "Key"), h("div", { class: "lb-ctl" },
    ...keyBtns.map((b, i) => h("span", { class: "lb-kpair" }, b, c.has_audio ? hearBtn(() => keyCams[i], () => accid(CAM_NAME(keyCams[i]))) : null)),
    h("span", { class: "lb-kpair" }, keySel, c.has_audio ? otherHear : null), ...(c.has_audio ? [drumsBtn, chordVol,
    h("span", { class: "lb-hint" }, "🔊 plays that key's chord alone for 2 seconds, then brings the track in under it. The right key blends in; a wrong one clashes. Try two in a row to compare.")] : [])));
}

// Marking section starts on the chart: the chart reads this hook, draws the marks and edits them through it.
function markingHook(c, bIn, setTimes, redrawChart, added, done) {
  return { cardId: c.id, get: () => (parseTimes(bIn.value) || []).sort((a, b) => a - b), set: setTimes,
    add: (t, snap) => { const ts = parseTimes(bIn.value) || [], tt = +(snap ? snapBar(c, t) : t).toFixed(2); if (!ts.some((q) => Math.abs(q - tt) < 0.3)) { ts.push(tt); added.push(tt); } setTimes(ts); redrawChart(); flash(bIn); },
    undo: () => { const ts = parseTimes(bIn.value) || []; let i = -1; while (added.length && i < 0) { const t = added.pop(); i = ts.findIndex((q) => Math.abs(q - t) < 0.06); } if (i < 0) i = ts.length - 1; ts.splice(i, 1); setTimes(ts); redrawChart(); },
    done };
}

// Section starts: type them, start from the detected ones, or mark them on the chart
function sectionsRow(p) {
  const { st, c } = p;
  const detected = (c.sections || []).map((x) => fmtMark(x.start)).join(" ");
  const bIn = h("input", { type: "text", value: st.bounds ? st.bounds.split(" ").map((t) => fmtMark(+t)).join(" ") : "", placeholder: "e.g. 0:00 0:32 1:04", "aria-label": "Section start times" });
  const redrawChart = () => { if (markHook && markHook.cardId === c.id) charts.get(c.id)?.draw(); };
  bIn.addEventListener("input", () => { st.bounds = bIn.value; p.sum(); redrawChart(); });
  const useDet = h("button", { type: "button", title: "Fill in Tastr's detected section starts, then fix any that are off" }, "Start from detected");
  useDet.onclick = () => { bIn.value = detected; st.bounds = detected; bIn.focus(); p.sum(); redrawChart(); };
  const setTimes = (ts, sort = true) => { const v = (sort ? [...ts].sort((a, b) => a - b) : ts).map(fmtMark).join(" "); bIn.value = v; st.bounds = v; p.sum(); };
  const en = c.series && c.series.energy, hasChart = !!(en && en.times && en.times.length > 1) || !!(c.sections || []).length; // the card shows the chart
  const markBtn = h("button", { type: "button", class: "lb-mark", "aria-pressed": "false", title: "Click the chart below to add section starts", ...(hasChart ? {} : { hidden: "" }) }, "✎ Mark on chart");
  const atHead = h("button", { type: "button", hidden: "", title: "Add a start where the playhead is (or press M)" }, "＋ At playhead");
  const added = []; // marks added while this panel is open, newest last, for Undo
  const markHint = h("span", { class: "lb-hint", hidden: "" }, "Click the waveform or energy chart to add a start; it snaps to the nearest bar line (hold Alt for the exact spot). Drag a mark to move it, click it to remove it. While it plays, press M to drop one on the beat.");
  function setMarking(on) {
    if (markHook && markHook.cardId !== c.id) setMarkHook(null);
    setMarkHook(on ? markingHook(c, bIn, setTimes, redrawChart, added, () => setMarking(false)) : null);
    markBtn.setAttribute("aria-pressed", String(on)); markBtn.textContent = on ? "✓ Done marking" : "✎ Mark on chart"; atHead.hidden = markHint.hidden = !on;
    const ch = charts.get(c.id);
    // marking opens the chart expanded on desktop (more room to place marks); Done or Close puts it back
    if (!ch) return;
    if (on && ch.mode() === "live") ch.setMode("wave"); else ch.draw();
    ch.big(on && innerWidth > 700);
    if (!on) markBtn.scrollIntoView({ block: "nearest" }); else if (innerWidth <= 700) ch.el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  markBtn.onclick = () => setMarking(!(markHook && markHook.cardId === c.id));
  atHead.onclick = () => { const ch = charts.get(c.id); if (ch && markHook) markHook.add(ch.now(), true); };
  return h("div", { class: "lb-row" }, h("b", {}, "Sections"), h("div", { class: "lb-ctl" }, bIn, detected ? useDet : null, markBtn, atHead, markHint, h("span", { class: "lb-hint" }, "Where each section starts, in m:ss or seconds. Leave empty to skip.")));
}

// PUT the label; the server checks it and answers 400 with a readable message
async function save(p, next) {
  const { st, err, c } = p;
  err.textContent = "";
  const out = {};
  if (st.bpm !== "") out.bpm = st.bpm;
  if (st.key) { const cam = nameToCam(st.key); out.key_name = cam ? CAM_NAME(cam) : st.key; }
  if (String(st.bounds).trim()) {
    const ts = parseTimes(st.bounds);
    out.boundaries = ts ? ts.sort((a, b) => a - b).map((t) => +t.toFixed(2)).join(" ") : st.bounds;
  }
  if (st.note.trim()) out.note = st.note.trim();
  const had = labels.has(c.id);
  try { await api(`/api/tracks/${c.id}/labels`, JSON_PUT(out)); } catch (e) { err.textContent = e.message; return; }
  if (!had && Object.keys(out).length) countReviewed();
  saved(p.lb, next);
}

function actions(p, lab) {
  const { st, lb, c } = p, nextUp = reviewRows.find((r) => r.track_id !== c.id);
  return h("div", { class: "lb-acts" },
    h("button", { type: "button", class: "primary", onclick: () => save(p, false) }, "Save label"),
    nextUp ? h("button", { type: "button", onclick: () => save(p, true), title: `Then open ${nextUp.title || "the next one"}` }, "Save & next ▸") : null,
    lb.canKeep ? h("button", { type: "button", title: `Tastr is right: label it ${lb.tastrSay}${reviewOn ? ", then open the next one" : ""}`, onclick: () => keepAsIs(lb, st.note.trim(), p.err) }, "✓ Keep as is") : null,
    lab ? h("button", { type: "button", class: "linklike", onclick: () => { st.bpm = ""; st.key = ""; st.bounds = ""; st.note = ""; save(p, false); } }, "Remove label") : null,
    h("button", { type: "button", class: "linklike", onclick: () => { setLabelOpen(null); lb.paint(); } }, "Cancel"),
    h("span", { class: "muted lb-hint", style: "flex-basis:auto" }, "Kept apart from the analysis: re-analyzing never changes it."));
}

function panel(lb) {
  const { c } = lb, lab = labels.get(c.id), why = reviewRow(c.id);
  const p = { lb, c, why, st: { bpm: lab?.bpm ?? "", key: lab?.key_name ?? "", bounds: lab?.boundaries ?? "", note: lab?.note ?? "" },
    err: h("p", { class: "lb-err", role: "alert" }), sumEl: h("p", { class: "lb-sum", "aria-live": "polite" }) };
  p.sum = () => paintSum(p);
  const rows = [bpmRow(p), keyRow(p), sectionsRow(p)];
  const noteIn = h("input", { type: "text", value: p.st.note, maxlength: "500", placeholder: "Optional, e.g. half-time feel in the breakdown", "aria-label": "Note" });
  noteIn.addEventListener("input", () => { p.st.note = noteIn.value; p.sum(); });
  setTimeout(() => { p.syncBpm(); p.syncKey(); p.sum(); }, 0);
  return h("div", { class: "lb-panel", role: "group", "aria-label": "Your label for this track" },
    why ? h("p", { class: "lb-why" }, h("b", {}, "Why it's listed: "), accid(why.reasons.join("; ")), ".") : null,
    ...rows,
    h("div", { class: "lb-row" }, h("b", {}, "Note"), h("div", { class: "lb-ctl" }, noteIn)),
    p.sumEl, p.err, actions(p, lab));
}

export function labelBar(c) {
  const core = c.core || {}, key = core.key, cam0 = key && key.camelot, hasBpm = isNum(core.bpm), tBpm = hasBpm ? +core.bpm.toFixed(1) : null;
  const lb = { c, core, key, cam0, hasBpm, tBpm, bar: h("div", { class: "lb-bar" }),
    tastrSay: [hasBpm ? `${tBpm} BPM` : null, cam0 ? keyName(key) : null].filter(Boolean).join(", "), canKeep: hasBpm || !!cam0 };
  lb.paint = () => {
    if (labelOpen !== c.id) endMarking(c.id);
    if (!lb.bar.isConnected && repaintLabel && repaintLabel.id === c.id && repaintLabel.bar !== lb.bar) return;
    lb.bar.replaceChildren(...(labelOpen === c.id ? [panel(lb)] : summary(lb)).filter(Boolean));
  };
  setRepaintLabel({ id: c.id, bar: lb.bar, paint: lb.paint });
  lb.paint();
  return lb.bar;
}
