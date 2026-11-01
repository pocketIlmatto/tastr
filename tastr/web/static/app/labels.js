// Your labels and the review list (tastr/labels.py), and the label bar on the card.

import { $, api, cap, currentId, h, isNum, labels, patternMarks, reviewRows, setLabels, setPatternMarks, setReviewRows, tracks } from "./core.js";
import { LIB_CLEAR, camelotNear, fmtUp, fmtUpLong, lib, libCollapsed, renderList, setLibCollapsed, syncTools } from "./library.js";
import { hearChordDb, hearDrums, hearKey, hearing, openTrack, setHearChordDb, setHearDrums } from "./card.js";
import { accid, keyName } from "./daw.js";
import { audioCtx, downloadBytes, dragNumber } from "./sequencer.js";
import { setStatus } from "./upload.js";

// GET /api/review is the list (tracks you flagged first, then the most suspect), GET /api/labels the log; a label is
// PUT /api/tracks/{id}/labels, and PUT /api/tracks/{id}/review {state: "flagged" | "skipped" | null} puts a track in
// the list or takes it out. Labels are kept apart from the analysis: re-analyzing never changes them.
export let reviewOn = false, labelOpen = null, reviewDone = 0, reviewTab = "todo";
export const JSON_PUT = (body) => ({ method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export async function loadReview(paint = true) {
  try {
    const [q, labs, pats] = await Promise.all([api("/api/review?limit=1000"), api("/api/labels"), api("/api/pattern-reviews").catch(() => [])]);
    setReviewRows(q); setLabels(new Map(labs.map((l) => [l.track_id, l]))); setPatternMarks(pats);
  } catch (_) { /* an older server, or offline: keep what we had */ }
  if (paint) renderList();
}
const reviewRow = (id) => reviewRows.find((r) => r.track_id === id);
export const inReview = (id) => !!reviewRow(id);
let repaintLabel = null; // the open card's label bar repaints itself through this
export async function setInReview(ids, on) {
  try { await Promise.all(ids.map((id) => api(`/api/tracks/${id}/review`, JSON_PUT({ state: on ? "flagged" : "skipped" })))); }
  catch (e) { setStatus(e.message, true); }
  await loadReview();
  if (repaintLabel && ids.includes(repaintLabel.id)) repaintLabel.paint();
}
// Camelot code <-> key: 8B = C major, 8A = A minor, each step round the wheel = up a fifth. Names are plain text ("Bb major").
const MAJ_ASCII = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"], MIN_ASCII = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"];
export const camPc = (cam) => { const m = /^(\d+)([AB])$/.exec(cam || ""); return m ? ((((m[2] === "B" ? 0 : 9) + (+m[1] - 8) * 7) % 12) + 12) % 12 : null; };
const CAM_NAME = (cam) => { const m = /^(\d+)([AB])$/.exec(cam || ""); if (!m) return cam; const pc = camPc(cam); return m[2] === "A" ? `${MIN_ASCII[pc]} minor` : `${MAJ_ASCII[pc]} major`; };
const ALL_CAMS = [...Array(12)].flatMap((_, i) => [`${i + 1}A`, `${i + 1}B`]);
const PC_OF = { c: 0, "b#": 0, "c#": 1, db: 1, d: 2, "d#": 3, eb: 3, e: 4, fb: 4, "e#": 5, f: 5, "f#": 6, gb: 6, g: 7, "g#": 8, ab: 8, a: 9, "a#": 10, bb: 10, b: 11, cb: 11 };
const camOf = (pc, minor) => `${((7 * (minor ? (pc + 3) % 12 : pc)) % 12 + 7) % 12 + 1}${minor ? "A" : "B"}`;
// Any key spelling the server takes ("A minor", "Am", "F# major", "8A") -> its Camelot code, or null.
function nameToCam(n) {
  const s = String(n || "").trim().replace(/♯/g, "#").replace(/♭/g, "b");
  let m = /^(1[0-2]|[1-9])\s*([ab])$/i.exec(s);
  if (m) return `${+m[1]}${m[2].toUpperCase()}`;
  m = /^([a-g])([#b]?)\s*:?\s*(major|minor|maj|min|m)?$/i.exec(s);
  if (!m) return null;
  const pc = PC_OF[(m[1] + m[2]).toLowerCase()];
  return pc == null ? null : camOf(pc, /^(minor|min|m)$/i.test(m[3] || ""));
}
const camLabel = (cam) => `${accid(CAM_NAME(cam))} (${cam})`;
export function renderReview() {
  const q = reviewRows;
  $("lib-review-n").textContent = q.length ? String(q.length) : "";
  $("lib-review").setAttribute("aria-selected", String(reviewOn)); $("lib-tracks").setAttribute("aria-selected", String(!reviewOn));
  $("review").hidden = !reviewOn; $("track-list").hidden = reviewOn; $("lib-tools").hidden = reviewOn || !tracks.length;
  if (!reviewOn) return;
  $("rv-n-todo").textContent = `(${q.length})`; $("rv-n-done").textContent = `(${reviewedLog().length})`;
  $("rv-tab-todo").setAttribute("aria-selected", String(reviewTab === "todo")); $("rv-tab-done").setAttribute("aria-selected", String(reviewTab === "done"));
  $("review").querySelector(".rv-intro").hidden = reviewTab !== "todo";
  if (reviewTab === "done") { renderReviewLog(); return; }
  const ul = $("review-list"), top = ul.scrollTop;
  ul.replaceChildren(); ul.setAttribute("aria-label", "Tracks to review");
  if (!q.length) { ul.append(h("li", { class: "empty" }, reviewDone ? `All caught up: you labeled ${reviewDone} this session.` : "Nothing to review: every track looks right or is labeled.")); return; }
  for (const r of q.slice(0, 20)) {
    const pct = isNum(r.chance_right) ? Math.round(r.chance_right * 100) : null, title = r.title || `Track ${r.track_id}`;
    const open = () => openForReview(r.track_id);
    ul.append(h("li", { class: currentId === r.track_id ? "active" : "", "data-id": r.track_id, tabindex: "0", role: "button", "aria-label": `${title}: ${r.reasons.join(", ")}`,
        onclick: open, onkeydown: (e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); open(); } } },
      h("div", { class: "rv-top" }, h("span", { class: "rv-t", title }, title),
        r.mine ? h("span", { class: "rv-chance", title: "You added this one" }, "added by you")
          : pct != null ? h("span", { class: "rv-chance" + (pct < 40 ? " low" : ""), title: "How likely Tastr's BPM and key are right (the lower of the two)" }, `${pct}% likely right`) : null),
      h("div", { class: "rv-s" }, [isNum(r.bpm) ? `${r.bpm.toFixed(0)} BPM` : null, r.camelot ? `${r.camelot} ${accid(CAM_NAME(r.camelot))}` : null].filter(Boolean).join(" · ")),
      h("ul", { class: "rv-why" }, r.reasons.map((x) => h("li", {}, accid(x)))),
      h("button", { type: "button", class: "rv-x", title: "Take it out of review", "aria-label": `Take ${title} out of review`, onclick: (e) => { e.stopPropagation(); setInReview([r.track_id], false); } }, "×")));
  }
  if (q.length > 20) ul.append(h("li", { class: "empty" }, `${q.length - 20} more after these`));
  ul.scrollTop = top;
}
// What a label changed from Tastr's reading when it was saved: [kind, text], kind same | chg | note.
function labelDiffs(lab) {
  const out = [], tb = lab.tastr_bpm, tcam = nameToCam(lab.tastr_key), tk = tcam ? accid(CAM_NAME(tcam)) : lab.tastr_key || "no key";
  const note = lab.note ? [["note", `“${lab.note}”`]] : [];
  if (lab.kept) return [["same", `Kept as is: ${[isNum(tb) ? `${tb.toFixed(1)} BPM` : null, tk].filter(Boolean).join(" · ")}`], ...note];
  if (isNum(lab.bpm)) out.push(isNum(tb) && Math.abs(lab.bpm - tb) < 0.06 ? ["same", `BPM ✓ ${tb.toFixed(1)}`] : ["chg", `BPM ${isNum(tb) ? tb.toFixed(1) : "–"} → ${lab.bpm}`]);
  if (lab.key_name) { const cam = nameToCam(lab.key_name); out.push(cam && cam === tcam ? ["same", `Key ✓ ${tk}`] : ["chg", `Key ${tk} → ${cam ? accid(CAM_NAME(cam)) : lab.key_name}`]); }
  if (lab.boundaries) out.push(["chg", `${lab.boundaries.split(" ").length} section starts`]);
  return [...out, ...note];
}
// One pattern review as a log line: [kind, text], a Right mark "same", anything else "chg" (or "note" once stale).
function patternDiff(m) {
  const s = m.section, where = s ? ` in ${cap(s.label || "section")}${s.letter ? " " + s.letter : ""}` : m.end_bar ? ` in bars ${m.start_bar}-${m.end_bar}` : "";
  const word = { right: "right", close: "close", way_off: "way off" }[m.verdict], name = cap(m.part);
  const text = [word ? `${name} ${word}${where}` : `${name}${where}`, m.truth ? "right pattern saved" : null].filter(Boolean).join(": ");
  return [m.stale ? "note" : m.verdict === "right" && !m.truth ? "same" : "chg", m.stale ? `${text} (older analysis)` : text];
}
// The log: every track with a label or a pattern review, newest first.
function reviewedLog() {
  const byId = new Map();
  const at = (id, title, when) => { const r = byId.get(id) || { track_id: id, title, updated_at: "", diffs: [] }; if (String(when) > r.updated_at) r.updated_at = String(when); byId.set(id, r); return r; };
  for (const lab of labels.values()) at(lab.track_id, lab.title, lab.updated_at).diffs.push(...labelDiffs(lab));
  for (const m of patternMarks) at(m.track_id, m.title, m.updated_at).diffs.push(patternDiff(m));
  return [...byId.values()].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
}
function renderReviewLog() {
  const ul = $("review-list"), top = ul.scrollTop;
  ul.replaceChildren(); ul.setAttribute("aria-label", "Tracks you reviewed");
  const rows = reviewedLog();
  if (!rows.length) { ul.append(h("li", { class: "empty" }, "Nothing reviewed yet. Labels you save and patterns you mark show up here.")); return; }
  const fixed = rows.filter((r) => r.diffs.some(([k]) => k === "chg")).length;
  ul.append(h("li", { class: "empty rv-logsum" }, `${rows.length} reviewed · Tastr was off on ${fixed}. `,
    h("button", { type: "button", class: "linklike", onclick: () => { Object.assign(lib, LIB_CLEAR(), { lab: "yes" }); syncTools(); setReview(false); } }, "Show them in the library"), " · ",
    h("button", { type: "button", class: "linklike", onclick: exportLabels }, "Download CSV")));
  for (const r of rows) {
    const id = r.track_id, title = r.title || `Track ${id}`, at = Date.parse(r.updated_at);
    ul.append(h("li", { class: currentId === id ? "active" : "", "data-id": id, tabindex: "0", role: "button", onclick: () => openTrack(id),
        onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openTrack(id); } } },
      h("div", { class: "rv-top" }, h("span", { class: "rv-t", title }, title), at ? h("time", { class: "rv-when", title: fmtUpLong(at) }, fmtUp(at)) : null),
      h("ul", { class: "rv-diff" }, r.diffs.map(([k, t]) => h("li", { class: k }, t)))));
  }
  ul.scrollTop = top;
}
// Your labels as CSV, Tastr's reading when each was saved next to it.
function exportLabels() {
  const esc = (v) => (v == null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
  const cols = ["track_id", "title", "artist", "tastr_bpm", "bpm", "tastr_key", "key_name", "boundaries", "note", "updated_at", "kept"];
  const lines = [cols.join(","), ...[...labels.values()].map((lab) => cols.map((k) => esc(k === "kept" ? (lab.kept ? 1 : 0) : lab[k])).join(","))];
  downloadBytes(new TextEncoder().encode(lines.join("\n") + "\n"), "tastr-labels.csv", "text/csv");
}
function openForReview(id) { labelOpen = id; openTrack(id); renderReview(); }
function setReview(on) { reviewOn = on; if (on && libCollapsed) setLibCollapsed(false); renderList(); }

// Section-start times: m:ss with a tenth when it isn't a whole second (bar lines rarely land on one). Also reads seconds.
export const fmtMark = (t) => { const r = Math.round(t * 10) / 10, s = r % 60; return `${Math.floor(r / 60)}:${(Number.isInteger(s) ? String(s) : s.toFixed(1)).padStart(Number.isInteger(s) ? 2 : 4, "0")}`; };
const parseTimes = (str) => { const parts = String(str || "").trim().split(/[;\s,]+/).filter(Boolean); const ts = parts.map((x) => { const m = /^(?:(\d+):)?(\d+(?:\.\d+)?)$/.exec(x); return m ? (+(m[1] || 0)) * 60 + +m[2] : NaN; }); return ts.some((t) => !isNum(t)) ? null : ts; };
// Marking section starts on the chart: the open label panel sets this; the card's chart reads it, draws the marks and edits them.
export let markHook = null;
export const snapBar = (c, t) => { const bars = (c.core && c.core.downbeat_times) || []; let best = t, bd = Infinity; for (const b of bars) { const d = Math.abs(b - t); if (d < bd) { bd = d; best = b; } } return bd < 4 ? best : t; };
export const charts = new Map(); // card id -> {now, draw, mode, setMode, el, big} for the chart on screen
function endMarking(id) {
  if (!markHook || markHook.cardId !== id) return;
  markHook = null;
  const ch = charts.get(id); if (ch) { ch.big(false); ch.draw(); }
}

// The label bar at the bottom of the card head: is the analysis right? Closed, a one-line summary; open, the label panel.
export function labelBar(c) {
  const core = c.core || {}, key = core.key, cam0 = key && key.camelot, bar = h("div", { class: "lb-bar" });
  const hasBpm = isNum(core.bpm), tBpm = hasBpm ? +core.bpm.toFixed(1) : null;
  const tastrSay = [hasBpm ? `${tBpm} BPM` : null, cam0 ? keyName(key) : null].filter(Boolean).join(", ");
  const canKeep = hasBpm || !!cam0;
  const summary = () => {
    const lab = labels.get(c.id), why = reviewRow(c.id);
    const rvBtn = why ? h("button", { type: "button", class: "linklike", title: "Take it out of the review list", onclick: () => setInReview([c.id], false) }, "Remove from review")
      : h("button", { type: "button", class: "linklike", title: "Put it in the review list to check later", onclick: () => setInReview([c.id], true) }, "Add to review");
    if (!lab) return [h("span", {}, why ? (why.mine ? "You added this to review. " : "Tastr isn't sure about this one. ") : "Is this analysis right? "),
      h("button", { type: "button", onclick: () => { labelOpen = c.id; paint(); } }, why ? "Check it" : "Label it"),
      canKeep ? h("button", { type: "button", title: `Label it as right: ${tastrSay}`, onclick: () => keepAsIs("") }, "✓ Keep as is") : null, rvBtn];
    const lcam = lab.key_name ? nameToCam(lab.key_name) : null;
    const bits = [lab.bpm ? `${lab.bpm} BPM` : null, lab.key_name ? (lcam ? camLabel(lcam) : lab.key_name) : null, lab.boundaries ? `${lab.boundaries.split(" ").length} section starts` : null].filter(Boolean);
    return [h("span", { class: "lb-done" }, lab.kept ? "✓ Kept as is" : "✓ Your label"), h("span", {}, bits.join(" · ") || "note only"),
      h("button", { type: "button", onclick: () => { labelOpen = c.id; paint(); } }, "Edit"), why ? rvBtn : null];
  };
  // After a save: refresh the list and log, then open the next track in review (Save & next, or Keep as is in Review) or show the summary.
  async function saved(next) {
    labelOpen = null; endMarking(c.id);
    await loadReview();
    const nxt = next ? reviewRows.find((r) => r.track_id !== c.id) : null;
    if (nxt) openForReview(nxt.track_id); else paint();
  }
  // "Keep as is": Tastr got it right. Stored as a label with Tastr's own BPM and key, so it scores future engine changes too.
  async function keepAsIs(note, err) {
    const had = labels.has(c.id);
    try { await api(`/api/tracks/${c.id}/labels`, JSON_PUT({ ...(hasBpm ? { bpm: tBpm } : {}), ...(cam0 ? { key_name: CAM_NAME(cam0) } : {}), ...(note ? { note } : {}), kept: true })); }
    catch (e) { if (err) err.textContent = e.message; else setStatus(e.message, true); return; }
    if (!had) reviewDone++;
    saved(reviewOn);
  }
  function paint() {
    if (labelOpen !== c.id) endMarking(c.id);
    if (!bar.isConnected && repaintLabel && repaintLabel.id === c.id && repaintLabel.bar !== bar) return;
    bar.replaceChildren(...(labelOpen === c.id ? [panel()] : summary()).filter(Boolean));
  }
  function panel() {
    const lab = labels.get(c.id), why = reviewRow(c.id);
    const st = { bpm: lab?.bpm ?? "", key: lab?.key_name ?? "", bounds: lab?.boundaries ?? "", note: lab?.note ?? "" };
    const err = h("p", { class: "lb-err", role: "alert" });
    const flash = (el) => { el.classList.remove("lb-flash"); void el.offsetWidth; el.classList.add("lb-flash"); };
    // what Save will store, kept in view so every pick, drag or tap shows where it lands
    const sumEl = h("p", { class: "lb-sum", "aria-live": "polite" });
    function sum() {
      const cam = nameToCam(st.key), nB = String(st.bounds).trim() ? String(st.bounds).trim().split(/[;\s,]+/).length : 0;
      const parts = [st.bpm !== "" ? h("b", {}, `${st.bpm} BPM`) : null, cam ? h("b", {}, camLabel(cam)) : st.key ? h("b", {}, st.key) : null,
        nB ? h("b", {}, `${nB} section start${nB === 1 ? "" : "s"}`) : null, st.note.trim() ? "a note" : null].filter(Boolean);
      sumEl.replaceChildren(...(parts.length ? ["Will save: ", ...parts.flatMap((p, i) => (i ? [" · ", p] : [p]))] : ["Nothing set yet. Pick, type, drag or tap a value."]));
    }
    // BPM: confirm, half/double, type or drag it, or tap it
    const bpmIn = h("input", { type: "number", min: "30", max: "300", step: "0.1", value: String(st.bpm), "aria-label": "Your BPM", placeholder: "BPM" });
    const bpmVals = hasBpm ? [core.bpm, core.bpm / 2, core.bpm * 2] : [];
    const bpmBtns = bpmVals.map((v, i) => h("button", { type: "button", "aria-pressed": "false", title: i ? `${v.toFixed(1)} BPM` : `Keep Tastr's ${v.toFixed(1)}`,
      onclick: () => setBpm(+v.toFixed(1)) }, i ? `${["", "½", "×2"][i]} ${v.toFixed(1)}` : `✓ ${v.toFixed(1)}`));
    const syncBpm = () => bpmBtns.forEach((b, i) => b.setAttribute("aria-pressed", String(st.bpm !== "" && Math.abs(+st.bpm - bpmVals[i]) < 0.06)));
    const BPM_HINT = "Tastr often lands on half or double time. Tap along if you're not sure.";
    const bpmHint = h("span", { class: "lb-hint", "aria-live": "polite" }, BPM_HINT);
    function setBpm(v, from) { st.bpm = v; bpmIn.value = String(v); syncBpm(); if (from !== "tap") bpmHint.replaceChildren(BPM_HINT); flash(bpmIn); sum(); }
    bpmIn.addEventListener("input", () => { st.bpm = bpmIn.value; syncBpm(); sum(); });
    dragNumber(bpmIn, { coarse: 1, from: () => tBpm ?? 120 }); // the drum player's drag box: a whole BPM per notch, Shift for 0.1
    let taps = [], tapTimer = 0;
    const tap = h("button", { type: "button", class: "lb-tap", title: "Tap along to the beat" }, "Tap");
    const tapDone = () => { if (taps.length > 2) { bpmHint.replaceChildren(h("b", {}, `Set to ${st.bpm} BPM from your ${taps.length} taps.`), " Tap again to start over."); tap.textContent = "Tap"; } };
    tap.onclick = () => {
      const now = performance.now(); if (taps.length && now - taps[taps.length - 1] > 2000) taps = []; taps.push(now); taps = taps.slice(-9);
      clearTimeout(tapTimer); tapTimer = setTimeout(tapDone, 2000);
      if (taps.length > 2) {
        const iv = (taps[taps.length - 1] - taps[0]) / (taps.length - 1), v = +(60000 / iv).toFixed(1);
        setBpm(v, "tap"); tap.textContent = `${v} ♩`;
        bpmHint.replaceChildren(h("b", {}, `Tapping: ${v} BPM → your label BPM.`), ` Keep tapping to steady it (${taps.length} taps).`);
      } else { tap.textContent = "Tap…"; bpmHint.replaceChildren(taps.length === 1 ? "Keep tapping on the beat…" : "One more…"); }
    };
    // Key: confirm, the usual near misses, or pick any of the 24; 🔊 plays each against the track
    const near = cam0 ? camelotNear(cam0) : [];
    const keyCams = cam0 ? [cam0, near[3], near[2], near[1]] : [];
    const keyBtns = keyCams.map((cam, i) => h("button", { type: "button", "aria-pressed": "false", title: `${cam} · ${["Keep Tastr's key", "Same notes, other mode", "Tastr's most common miss", "Tastr's most common miss"][i]}`,
      onclick: () => setKey(CAM_NAME(cam)) }, i ? `${["", "Relative", "Fifth up", "Fifth down"][i]}: ${accid(CAM_NAME(cam))}` : `✓ ${keyName(key)}`,
      why && why.catalog_key === cam ? h("span", { class: "lb-cat", title: "What the catalog lists" }, "catalog") : null));
    const hearBtn = (camOf, label) => { const b = h("button", { type: "button", class: "lb-hear", title: `Hear ${label()} against the track`, "aria-label": `Hear ${label()} against the track` }, "🔊"); b.onclick = () => hearKey(c, camOf(), b); return b; };
    const drumsBtn = h("button", { type: "button", class: "lb-drums", "aria-pressed": String(!hearDrums), title: "Leave the drums out of 🔊 so the chords are easier to hear" }, hearDrums ? "🥁 Drums on" : "🥁 Drums off");
    drumsBtn.onclick = () => {
      setHearDrums(!hearDrums); drumsBtn.setAttribute("aria-pressed", String(!hearDrums)); drumsBtn.textContent = hearDrums ? "🥁 Drums on" : "🥁 Drums off";
      if (hearing) { const hh = hearing; hh.stop(); hearKey(hh.c, hh.cam, hh.btn); } // replay what's playing, now with or without drums
    };
    // the chord's level against the track; changes what's playing right away
    const volOut = h("output", {}, ""), volIn = h("input", { type: "range", min: "-18", max: "12", step: "1", value: String(hearChordDb), "aria-label": "Chord volume against the track" });
    const volTxt = () => { volOut.textContent = hearChordDb === 0 ? "even" : `${hearChordDb > 0 ? "+" : ""}${hearChordDb} dB`; };
    volIn.addEventListener("input", () => { setHearChordDb(+volIn.value); volTxt(); if (hearing?.lvl) hearing.lvl.gain.setTargetAtTime(Math.pow(10, hearChordDb / 20), audioCtx().currentTime, 0.03); });
    volIn.addEventListener("dblclick", () => { volIn.value = "0"; volIn.dispatchEvent(new Event("input")); });
    volTxt();
    const chordVol = h("label", { class: "lb-vol", title: "How loud the 🔊 chord is against the track (double-click to reset)" }, "Chord", h("span", { class: "muted" }, "quieter"), volIn, h("span", { class: "muted" }, "louder"), volOut);
    const keySel = h("select", { "aria-label": "Your key" }, h("option", { value: "" }, "Other key…"), ALL_CAMS.map((cam) => h("option", { value: CAM_NAME(cam) }, `${cam} · ${accid(CAM_NAME(cam))}`)));
    const otherHear = hearBtn(() => nameToCam(keySel.value), () => "the picked key");
    const syncKey = () => {
      const cam = nameToCam(st.key);
      keyBtns.forEach((b, i) => b.setAttribute("aria-pressed", String(cam === keyCams[i])));
      keySel.value = cam && !keyCams.includes(cam) ? CAM_NAME(cam) : "";
      otherHear.disabled = !keySel.value;
      const tt = keySel.value ? `Hear ${accid(keySel.value)} against the track` : "Pick a key in the list first, then hear it against the track";
      otherHear.title = tt; otherHear.setAttribute("aria-label", tt);
    };
    function setKey(v) { st.key = v; syncKey(); sum(); }
    keySel.addEventListener("change", () => setKey(keySel.value));
    // Section starts: type them, start from the detected ones, or mark them on the chart
    const detected = (c.sections || []).map((x) => fmtMark(x.start)).join(" ");
    const bIn = h("input", { type: "text", value: st.bounds ? st.bounds.split(" ").map((t) => fmtMark(+t)).join(" ") : "", placeholder: "e.g. 0:00 0:32 1:04", "aria-label": "Section start times" });
    const redrawChart = () => { if (markHook && markHook.cardId === c.id) charts.get(c.id)?.draw(); };
    bIn.addEventListener("input", () => { st.bounds = bIn.value; sum(); redrawChart(); });
    const useDet = h("button", { type: "button", title: "Fill in Tastr's detected section starts, then fix any that are off" }, "Start from detected");
    useDet.onclick = () => { bIn.value = detected; st.bounds = detected; bIn.focus(); sum(); redrawChart(); };
    const setTimes = (ts, sort = true) => { const v = (sort ? [...ts].sort((a, b) => a - b) : ts).map(fmtMark).join(" "); bIn.value = v; st.bounds = v; sum(); };
    const en = c.series && c.series.energy, hasChart = !!(en && en.times && en.times.length > 1) || !!(c.sections || []).length; // the card shows the chart
    const markBtn = h("button", { type: "button", class: "lb-mark", "aria-pressed": "false", title: "Click the chart below to add section starts", ...(hasChart ? {} : { hidden: "" }) }, "✎ Mark on chart");
    const atHead = h("button", { type: "button", hidden: "", title: "Add a start where the playhead is (or press M)" }, "＋ At playhead");
    const markHint = h("span", { class: "lb-hint", hidden: "" }, "Click the waveform or energy chart to add a start; it snaps to the nearest bar line (hold Alt for the exact spot). Drag a mark to move it, click it to remove it. While it plays, press M to drop one on the beat.");
    const added = []; // marks added this time, newest last, for Undo
    function setMarking(on) {
      if (markHook && markHook.cardId !== c.id) markHook = null;
      markHook = on ? { cardId: c.id, get: () => (parseTimes(bIn.value) || []).sort((a, b) => a - b), set: setTimes,
        add: (t, snap) => { const ts = parseTimes(bIn.value) || [], tt = +(snap ? snapBar(c, t) : t).toFixed(2); if (!ts.some((q) => Math.abs(q - tt) < 0.3)) { ts.push(tt); added.push(tt); } setTimes(ts); redrawChart(); flash(bIn); },
        undo: () => { const ts = parseTimes(bIn.value) || []; let i = -1; while (added.length && i < 0) { const t = added.pop(); i = ts.findIndex((q) => Math.abs(q - t) < 0.06); } if (i < 0) i = ts.length - 1; ts.splice(i, 1); setTimes(ts); redrawChart(); },
        done: () => setMarking(false) } : null;
      markBtn.setAttribute("aria-pressed", String(on)); markBtn.textContent = on ? "✓ Done marking" : "✎ Mark on chart"; atHead.hidden = markHint.hidden = !on;
      const ch = charts.get(c.id);
      // marking opens the chart expanded on desktop (more room to place marks); Done or Close puts it back
      if (ch) {
        if (on && ch.mode() === "live") ch.setMode("wave"); else ch.draw();
        ch.big(on && innerWidth > 700);
        if (!on) markBtn.scrollIntoView({ block: "nearest" }); else if (innerWidth <= 700) ch.el.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    }
    markBtn.onclick = () => setMarking(!(markHook && markHook.cardId === c.id));
    atHead.onclick = () => { const ch = charts.get(c.id); if (ch && markHook) markHook.add(ch.now(), true); };
    const noteIn = h("input", { type: "text", value: st.note, maxlength: "500", placeholder: "Optional, e.g. half-time feel in the breakdown", "aria-label": "Note" });
    noteIn.addEventListener("input", () => { st.note = noteIn.value; sum(); });
    setTimeout(() => { syncBpm(); syncKey(); sum(); }, 0);
    // PUT the label; the server checks it and answers 400 with a readable message
    async function save(next) {
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
      if (!had && Object.keys(out).length) reviewDone++;
      saved(next);
    }
    const nextUp = reviewRows.find((r) => r.track_id !== c.id);
    return h("div", { class: "lb-panel", role: "group", "aria-label": "Your label for this track" },
      why ? h("p", { class: "lb-why" }, h("b", {}, "Why it's listed: "), accid(why.reasons.join("; ")), ".") : null,
      h("div", { class: "lb-row" }, h("b", {}, "BPM"), h("div", { class: "lb-ctl" }, ...bpmBtns, bpmIn, tap, bpmHint)),
      h("div", { class: "lb-row" }, h("b", {}, "Key"), h("div", { class: "lb-ctl" },
        ...keyBtns.map((b, i) => h("span", { class: "lb-kpair" }, b, c.has_audio ? hearBtn(() => keyCams[i], () => accid(CAM_NAME(keyCams[i]))) : null)),
        h("span", { class: "lb-kpair" }, keySel, c.has_audio ? otherHear : null), ...(c.has_audio ? [drumsBtn, chordVol,
        h("span", { class: "lb-hint" }, "🔊 plays that key's chord alone for 2 seconds, then brings the track in under it. The right key blends in; a wrong one clashes. Try two in a row to compare.")] : []))),
      h("div", { class: "lb-row" }, h("b", {}, "Sections"), h("div", { class: "lb-ctl" }, bIn, detected ? useDet : null, markBtn, atHead, markHint, h("span", { class: "lb-hint" }, "Where each section starts, in m:ss or seconds. Leave empty to skip."))),
      h("div", { class: "lb-row" }, h("b", {}, "Note"), h("div", { class: "lb-ctl" }, noteIn)),
      sumEl, err,
      h("div", { class: "lb-acts" },
        h("button", { type: "button", class: "primary", onclick: () => save(false) }, "Save label"),
        nextUp ? h("button", { type: "button", onclick: () => save(true), title: `Then open ${nextUp.title || "the next one"}` }, "Save & next ▸") : null,
        canKeep ? h("button", { type: "button", title: `Tastr is right: label it ${tastrSay}${reviewOn ? ", then open the next one" : ""}`, onclick: () => keepAsIs(st.note.trim(), err) }, "✓ Keep as is") : null,
        lab ? h("button", { type: "button", class: "linklike", onclick: () => { st.bpm = ""; st.key = ""; st.bounds = ""; st.note = ""; save(false); } }, "Remove label") : null,
        h("button", { type: "button", class: "linklike", onclick: () => { labelOpen = null; paint(); } }, "Cancel"),
        h("span", { class: "muted lb-hint", style: "flex-basis:auto" }, "Kept apart from the analysis: re-analyzing never changes it.")));
  }
  repaintLabel = { id: c.id, bar, paint };
  paint();
  return bar;
}

export function setLabelOpen(v) { return (labelOpen = v); }
export function setMarkHook(v) { return (markHook = v); }

// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  $("rv-tab-todo").addEventListener("click", () => { reviewTab = "todo"; renderReview(); });
  $("rv-tab-done").addEventListener("click", () => { reviewTab = "done"; renderReview(); });
  $("lib-review").addEventListener("click", (e) => { e.stopPropagation(); if (libCollapsed) setLibCollapsed(false); if (!reviewOn) setReview(true); });
  $("lib-tracks").addEventListener("click", (e) => { if (libCollapsed) return; e.stopPropagation(); if (reviewOn) setReview(false); });
  document.addEventListener("keydown", (e) => {
    if (!markHook || e.key.toLowerCase() !== "m" || e.metaKey || e.ctrlKey || e.altKey || /INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName || "")) return;
    const ch = charts.get(markHook.cardId); if (!ch) return;
    e.preventDefault(); markHook.add(ch.now(), true);
  });
}
