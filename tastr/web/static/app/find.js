// Find tracks: the Simple builder and SQL.

import { $, api, h, isNum, mmss, tracks } from "./core.js";
import { refresh } from "./library.js";
import { openTrack, switchTo } from "./card.js";
import { setServerCfg } from "./daw.js";
import { setStatus } from "./upload.js";

// Simple builds the SQL itself and runs it through /api/query (read-only, and only your own library when hosted);
// "Edit as SQL" carries a Simple search over, so people can learn from it.
const Q_CAM = { "1A": "A♭m", "2A": "E♭m", "3A": "B♭m", "4A": "Fm", "5A": "Cm", "6A": "Gm", "7A": "Dm", "8A": "Am", "9A": "Em", "10A": "Bm", "11A": "F♯m", "12A": "C♯m",
  "1B": "B", "2B": "F♯", "3B": "D♭", "4B": "A♭", "5B": "E♭", "6B": "B♭", "7B": "F", "8B": "C", "9B": "G", "10B": "D", "11B": "A", "12B": "E" };
const Q_SECTIONS = ["intro", "verse", "build", "chorus", "drop", "breakdown", "outro"]; // the labels tastr/engine/stages/sections.py gives
const qMixes = (code) => { const n = parseInt(code), l = code.slice(-1), w = (k) => ((k + 11) % 12) + 1; return [code, `${w(n - 1)}${l}`, `${w(n + 1)}${l}`, `${n}${l === "A" ? "B" : "A"}`]; };
const qTags = () => [...new Set(tracks.flatMap((t) => t.tags || []))].sort();
const qEsc = (v) => String(v).replace(/'/g, "''");
const NUM_OPS = [["between", "is between"], ["min", "is at least"], ["max", "is at most"]];
// Each field: how to pick a value, and the SQL it stands for
const Q_FIELDS = {
  bpm: { label: "BPM", ops: [...NUM_OPS, ["about", "is about (±3)"]], kind: "num", step: 1, def: [120, 128], col: "bpm" },
  key: { label: "Key", ops: [["mixes", "mixes with"], ["is", "is"]], kind: "key", def: ["8A"] },
  energy: { label: "Energy", ops: NUM_OPS, kind: "num", step: 0.05, min: 0, max: 1, def: [0.6, 1], col: "energy" },
  vibe: { label: "Vibe", ops: [["is", "is"], ["not", "isn't"]], kind: "tag", def: ["dark"] },
  length: { label: "Length (min)", ops: NUM_OPS, kind: "num", step: 0.5, min: 0, def: [4, 7], col: "duration", scale: 60 },
  section: { label: "Section", ops: [["has", "has a"], ["none", "has no"]], kind: "label", def: ["drop"] },
  text: { label: "Title, artist or album", ops: [["has", "contains"]], kind: "text", def: [""] },
};
// [label, ORDER BY for low to high]; Camelot sorts by number, then A before B
const Q_SORTS = { bpm: ["BPM", ["bpm"]], energy: ["Energy", ["energy"]], length: ["Length", ["duration"]], key: ["Key", ["CAST(camelot AS INTEGER)", "camelot"]], title: ["Title", ["title COLLATE NOCASE"]] };
// A search is groups of conditions: inside a group they join with the group's AND/OR; groups join with qJoin.
// One level of grouping covers "(A or B) and (C or D)" without a nesting UI.
export let qMode = "simple", qGroups = [], qJoin = "and", qSort = ["bpm", "asc"];
const qGroup = (conds, join = "and") => ({ join, conds: conds.map((q) => ({ ...q, v: q.v.slice() })) });
const qNew = (field, op, vals) => ({ field, op: op || Q_FIELDS[field].ops[0][0], v: (vals || Q_FIELDS[field].def).slice() });
function qSqlOf(q) {
  const F = Q_FIELDS[q.field]; if (!F) return null;
  const [a, b] = q.v, k = F.scale || 1, n = (x) => +((+x || 0) * k).toFixed(2);
  if (F.kind === "num") return q.op === "between" ? `${F.col} BETWEEN ${n(Math.min(+a, +b))} AND ${n(Math.max(+a, +b))}` : q.op === "min" ? `${F.col} >= ${n(a)}` : q.op === "max" ? `${F.col} <= ${n(a)}` : `${F.col} BETWEEN ${n(a) - 3} AND ${n(a) + 3}`;
  if (q.field === "key") { if (!Q_CAM[a]) return null; return q.op === "is" ? `camelot = '${a}'` : `camelot IN (${qMixes(a).map((x) => `'${x}'`).join(", ")})`; }
  if (q.field === "vibe") return a ? `id ${q.op === "not" ? "NOT IN" : "IN"} (SELECT track_id FROM tags WHERE tag = '${qEsc(a)}')` : null;
  if (q.field === "section") return a ? `id ${q.op === "none" ? "NOT IN" : "IN"} (SELECT track_id FROM sections WHERE label = '${qEsc(a)}')` : null;
  const t = qEsc(String(a || "").trim()); return t ? `(title LIKE '%${t}%' OR artist LIKE '%${t}%' OR album LIKE '%${t}%')` : null;
}
// The columns the results table shows; vibes are each track's top three tags
const Q_COLS = "id, title, artist, bpm, camelot, key_name, energy, duration,\n  (SELECT group_concat(tag, ', ') FROM (SELECT tag FROM tags WHERE track_id = tracks.id ORDER BY score DESC LIMIT 3)) AS vibes";
const qSql = () => {
  const gs = qGroups.map((g) => [g.join, g.conds.map(qSqlOf).filter(Boolean)]).filter(([, w]) => w.length);
  const part = ([j, w]) => (w.length > 1 && gs.length > 1 ? `(${w.join(` ${j.toUpperCase()} `)})` : w.join(` ${j.toUpperCase()} `));
  const where = gs.map(part).join(`\n  ${qJoin.toUpperCase()} `);
  const order = Q_SORTS[qSort[0]][1].map((c) => `${c} ${qSort[1].toUpperCase()}`).join(", ");
  return `SELECT ${Q_COLS}\nFROM tracks${where ? "\nWHERE " + where : ""}\nORDER BY ${order}`;
};
const sql = (text, mine = false) => api("/api/query", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sql: text, mine }) });
let qTimer = 0, qSeq = 0;
function qRun() {  // results follow the builder as you type, a moment after the last change
  clearTimeout(qTimer);
  qTimer = setTimeout(qFetch, 200);
}
async function qFetch(retry = true) {
  const seq = ++qSeq, text = qSql();
  let r;
  try { r = await sql(text, true); } catch (e) {
    if (seq !== qSeq) return;
    // A hosted server runs one query at a time; try once more before saying so
    if (retry && /Another SQL query/.test(e.message)) { setTimeout(() => { if (seq === qSeq) qFetch(false); }, 800); return; }
    $("sql-error").textContent = e.message; return;
  }
  if (seq !== qSeq || qMode !== "simple") return;
  const col = Object.fromEntries(r.columns.map((c, i) => [c, i])), get = (row, c) => row[col[c]];
  $("sql-error").textContent = "";
  $("q-count").textContent = `${r.rows.length} of ${tracks.length} tracks. Click a row to open its card.`;
  $("sql-result").replaceChildren(h("tr", {}, ["Title", "Artist", "BPM", "Key", "Energy", "Length", "Vibes"].map((t) => h("th", {}, t))),
    ...r.rows.map((row) => {
      const id = get(row, "id"), bpm = get(row, "bpm"), en = get(row, "energy"), cam = get(row, "camelot");
      const tr = h("tr", { class: "q-row", tabindex: "0" }, h("td", {}, get(row, "title") || ""), h("td", {}, get(row, "artist") || ""), h("td", {}, isNum(bpm) ? bpm.toFixed(1) : ""),
        h("td", {}, cam ? `${cam} · ${Q_CAM[cam] || get(row, "key_name") || ""}` : ""), h("td", {}, isNum(en) ? en.toFixed(2) : ""), h("td", {}, mmss(get(row, "duration"))), h("td", {}, get(row, "vibes") || ""));
      tr.onclick = () => openTrack(id); tr.onkeydown = (e) => { if (e.key === "Enter") openTrack(id); };
      return tr;
    }));
}
function qValueEls(q) {
  const F = Q_FIELDS[q.field], set = (i) => (e) => { q.v[i] = e.target.value; qRun(); };
  const opts = (list, cur) => list.map(([v, t]) => h("option", { value: v, ...(v === cur ? { selected: "" } : {}) }, t));
  if (F.kind === "num") {
    const box = (i) => { const el = h("input", { type: "number", step: String(F.step), ...(F.min != null ? { min: String(F.min) } : {}), ...(F.max != null ? { max: String(F.max) } : {}), value: String(q.v[i] ?? q.v[0]), "aria-label": `${F.label} ${i ? "to" : "from"}` }); el.addEventListener("input", set(i)); return el; };
    if (q.v[1] == null) q.v[1] = F.def[1];
    return q.op === "between" ? [box(0), h("span", { class: "muted" }, "and"), box(1)] : [box(0)];
  }
  if (F.kind === "text") { const el = h("input", { type: "text", value: q.v[0], placeholder: "e.g. remix", "aria-label": "Text to find" }); el.addEventListener("input", set(0)); return [el]; }
  const list = F.kind === "key" ? Object.entries(Q_CAM).sort((x, y) => parseInt(x[0]) - parseInt(y[0]) || x[0].localeCompare(y[0])).map(([k, n]) => [k, `${k} · ${n}`])
    : (F.kind === "tag" ? qTags() : Q_SECTIONS).map((t) => [t, t]);
  // A vibe none of your tracks has (say, from an example) stays picked, so the search still means what it says
  if (F.kind === "tag" && q.v[0] && !list.some(([v]) => v === q.v[0])) list.push([q.v[0], `${q.v[0]} (no tracks)`]);
  if (!list.some(([v]) => v === q.v[0])) q.v[0] = list[0] ? list[0][0] : "";
  const sel = h("select", { "aria-label": F.label }, opts(list, q.v[0])); sel.addEventListener("change", set(0));
  const els = [sel];
  if (q.field === "key" && q.op === "mixes") els.push(h("span", { class: "muted" }, `(${qMixes(q.v[0]).slice(1).join(", ")})`));
  return els;
}
export function renderSimple() {
  const host = $("q-simple"), conds = h("div", { class: "q-conds" + (qGroups.length > 1 ? " grouped" : "") });
  // Clicking an AND/OR word flips it for its whole group (or between all groups), so there's never a mix to puzzle over.
  const joinBtn = (cur, flip, what, k) => { const b = h("button", { type: "button", class: "q-join", "data-k": k, title: `Click to switch to ${cur === "and" ? "OR" : "AND"} ${what}`, "aria-label": `${cur.toUpperCase()}: click to switch to ${cur === "and" ? "OR" : "AND"} ${what}` }, cur); b.onclick = () => { flip(); renderSimple(); host.querySelector(`.q-join[data-k="${b.dataset.k}"]`)?.focus(); }; return b; };
  qGroups.forEach((g, gi) => {
    if (gi) conds.append(h("div", { class: "q-between" }, joinBtn(qJoin, () => { qJoin = qJoin === "and" ? "or" : "and"; }, "between groups", `b${gi}`)));
    const box = h("div", { class: "q-group" });
    g.conds.forEach((q, i) => {
      const field = h("select", { "aria-label": "Field" }, Object.entries(Q_FIELDS).map(([k, F]) => h("option", { value: k, ...(k === q.field ? { selected: "" } : {}) }, F.label)));
      field.onchange = () => { g.conds[i] = qNew(field.value); renderSimple(); };
      const op = h("select", { "aria-label": "Condition" }, Q_FIELDS[q.field].ops.map(([v, t]) => h("option", { value: v, ...(v === q.op ? { selected: "" } : {}) }, t)));
      op.onchange = () => { q.op = op.value; renderSimple(); };
      const del = h("button", { type: "button", class: "q-del", "aria-label": "Remove this condition", title: "Remove" }, "✕");
      del.onclick = () => { g.conds.splice(i, 1); if (!g.conds.length && qGroups.length > 1) qGroups.splice(gi, 1); renderSimple(); };
      const lead = i ? joinBtn(g.join, () => { g.join = g.join === "and" ? "or" : "and"; }, qGroups.length > 1 ? "in this group" : "", `g${gi}c${i}`)
        : h("span", { class: "q-and" }, gi === 0 ? "where" : "");
      box.append(h("div", { class: "q-cond" }, h("span", { class: "q-lead" }, lead), field, op, ...qValueEls(q), del));
    });
    if (!g.conds.length) box.append(h("p", { class: "muted q-empty" }, "No conditions: every track is listed. Add one, or start from an example above."));
    const add = h("button", { type: "button", class: "q-add" }, qGroups.length > 1 ? "+ Condition in this group" : "+ Add condition");
    add.onclick = () => { const used = new Set(g.conds.map((q) => q.field)); g.conds.push(qNew(Object.keys(Q_FIELDS).find((k) => !used.has(k)) || "bpm")); renderSimple(); host.querySelectorAll(".q-group")[gi].querySelector(".q-cond:last-of-type select").focus(); };
    box.append(add);
    conds.append(box);
  });
  const addGroup = h("button", { type: "button", class: "q-add", title: "A group's conditions are checked together, like brackets: (A or B) and (C or D)" }, "+ Add group");
  addGroup.onclick = () => { qGroups.push(qGroup([qNew("vibe")], "or")); renderSimple(); const gs = host.querySelectorAll(".q-group"); gs[gs.length - 1].querySelector("select").focus(); };
  const sortSel = h("select", { "aria-label": "Sort by" }, Object.entries(Q_SORTS).map(([k, [t]]) => h("option", { value: k, ...(k === qSort[0] ? { selected: "" } : {}) }, t)));
  const dirSel = h("select", { "aria-label": "Sort direction" }, [["asc", "low to high"], ["desc", "high to low"]].map(([v, t]) => h("option", { value: v, ...(v === qSort[1] ? { selected: "" } : {}) }, t)));
  sortSel.onchange = () => { qSort[0] = sortSel.value; qRun(); }; dirSel.onchange = () => { qSort[1] = dirSel.value; qRun(); };
  const toSql = h("button", { type: "button", class: "linklike", title: "Open this search in the SQL editor, to tweak it or see how it works" }, "Edit as SQL");
  toSql.onclick = () => { $("sql").value = qSql(); setQMode("sql"); runSql(); };
  host.replaceChildren(conds, h("div", { class: "q-bar" }, addGroup, h("label", { class: "q-sort" }, "Sort by", sortSel, dirSel), saveBtn(), $("q-mode").hidden ? null : toSql));
  qRun();
}
// Examples: Simple ones fill in conditions; SQL ones also cover what the builder can't (counts, averages, joins)
const SIMPLE_EXAMPLES = [  // [label, groups, sort]
  ["Mixes with 8A, 120–128 BPM", [qGroup([qNew("key", "mixes", ["8A"]), qNew("bpm", "between", [120, 128])])], ["bpm", "asc"]],
  ["Dark, over 120 BPM", [qGroup([qNew("vibe", "is", ["dark"]), qNew("bpm", "min", [120])])], ["energy", "desc"]],
  ["Wind-down: slow or calm, and moody", [qGroup([qNew("bpm", "max", [118]), qNew("energy", "max", [0.5])], "or"), qGroup([qNew("vibe", "is", ["chill"]), qNew("vibe", "is", ["melancholic"])], "or")], ["energy", "asc"]],
  ["Peak time: 124–130 BPM, high energy", [qGroup([qNew("bpm", "between", [124, 130]), qNew("energy", "min", [0.7])])], ["energy", "desc"]],
  ["No drop", [qGroup([qNew("section", "none", ["drop"])])], ["title", "asc"]],
  ["Longest first", [qGroup([])], ["length", "desc"]],
];
const SQL_EXAMPLES = [
  ["Mixes with 8A, 120–128 BPM", "SELECT title, bpm, camelot FROM tracks WHERE camelot IN ('7A','8A','9A','8B') AND bpm BETWEEN 120 AND 128"],
  ["Dark, bass-heavy", "SELECT t.title FROM tracks t JOIN tags g ON g.track_id = t.id WHERE g.tag = 'dark'\n  AND t.id IN (SELECT track_id FROM features WHERE name = 'low_end' AND value > 0.3)"],
  ["Average drop length (bars)", "SELECT avg(end_bar - start_bar + 1) AS avg_drop_bars FROM sections WHERE label = 'drop'"],
  ["Most common keys", "SELECT camelot, key_name, count(*) AS tracks FROM tracks\nGROUP BY camelot ORDER BY tracks DESC"],
  ["Tempo by vibe", "SELECT g.tag, count(*) AS tracks, round(avg(t.bpm), 1) AS avg_bpm\nFROM tags g JOIN tracks t ON t.id = g.track_id\nGROUP BY g.tag ORDER BY tracks DESC"],
  ["Longest intros", "SELECT t.title, s.end_bar - s.start_bar + 1 AS intro_bars\nFROM sections s JOIN tracks t ON t.id = s.track_id\nWHERE s.label = 'intro' ORDER BY intro_bars DESC LIMIT 10"],
  ["Drops per track", "SELECT t.title, count(s.idx) AS drops FROM tracks t\nLEFT JOIN sections s ON s.track_id = t.id AND s.label = 'drop'\nGROUP BY t.id ORDER BY drops DESC"],
  ["Recently added", "SELECT title, artist, analyzed_at FROM tracks ORDER BY analyzed_at DESC LIMIT 10"],
];
// Saved searches: the person's own are the quick chips; the built-in examples sit in an "Examples ▾" menu.
// The server keeps them per person (GET/PUT/DELETE /api/searches), so they follow you to another device.
let qSaved = { simple: [], sql: [] };
export async function loadSaved() {
  try {
    const list = await api("/api/searches");
    qSaved = { simple: [], sql: [] };
    for (const x of list) if (qSaved[x.mode]) qSaved[x.mode].push({ id: x.id, name: x.name, ...x.data });
  } catch (_) {}
  renderExamples();
}
const qSnap = () => (qMode === "simple" ? { groups: qGroups.map((g) => qGroup(g.conds, g.join)), join: qJoin, sort: qSort.slice() } : { sql: $("sql").value });
function qApply(x) {
  if (qMode === "simple") {
    qGroups = (x.groups || []).map((g) => qGroup((g.conds || []).filter((q) => Q_FIELDS[q.field] && Array.isArray(q.v)), g.join === "or" ? "or" : "and"));
    if (!qGroups.length) qGroups = [qGroup([])];
    qJoin = x.join === "or" ? "or" : "and";
    qSort = Array.isArray(x.sort) && Q_SORTS[x.sort[0]] ? [x.sort[0], x.sort[1] === "desc" ? "desc" : "asc"] : ["bpm", "asc"];
    renderSimple();
  } else { $("sql").value = x.sql || ""; runSql(); }
}
function saveBtn() {
  // "Save search": an inline name box, so it works the same on a phone
  const wrap = h("span", { class: "q-save" }), go = h("button", { type: "button", class: "linklike" }, "Save search");
  go.onclick = () => {
    const name = h("input", { type: "text", maxlength: "40", placeholder: "Name, e.g. Warm-up set", "aria-label": "Name for this search" });
    const ok = h("button", { type: "button" }, "Save"), no = h("button", { type: "button", class: "linklike" }, "Cancel");
    const err = h("span", { class: "error", role: "status" });
    const done = () => { wrap.replaceChildren(go); go.focus(); };
    const save = async () => {
      const n = name.value.trim(); if (!n) { name.focus(); return; }
      ok.disabled = true;
      try { await api("/api/searches", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: qMode, name: n, data: qSnap() }) }); }
      catch (e) { ok.disabled = false; err.textContent = e.message; return; }
      done(); loadSaved();  // the same name replaces the old search
    };
    ok.onclick = save; no.onclick = done;
    // preventDefault: focus moves to the Save search link here, and the browser would otherwise turn this same Enter into a click on it, reopening the box
    name.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save(); } if (e.key === "Escape") { e.preventDefault(); done(); } });
    wrap.replaceChildren(name, ok, no, err); name.focus();
  };
  wrap.append(go);
  return wrap;
}
function renderExamples() {
  const list = qMode === "simple" ? SIMPLE_EXAMPLES : SQL_EXAMPLES, mine = qSaved[qMode];
  const chips = mine.map((x) => {
    const del = h("button", { type: "button", class: "q-chip-del", "aria-label": `Delete saved search ${x.name}`, title: "Delete" }, "✕");
    del.onclick = async () => {
      try { await api(`/api/searches/${x.id}`, { method: "DELETE" }); } catch (e) { $("sql-error").textContent = e.message; return; }
      loadSaved();
    };
    return h("span", { class: "q-chip" }, h("button", { type: "button", class: "q-chip-go", onclick: () => qApply(x) }, x.name), del);
  });
  const exBtn = h("button", { type: "button", class: "ver-btn q-ex-btn", "aria-haspopup": "true", "aria-expanded": "false" }, "Examples ▾");
  const pop = h("div", { class: "ver-pop q-ex-pop", hidden: "", role: "menu" });
  const exWrap = h("span", { class: "ver-wrap" }, exBtn, pop);
  const close = (refocus) => { pop.hidden = true; exBtn.setAttribute("aria-expanded", "false"); document.removeEventListener("pointerdown", outside, true); if (refocus) exBtn.focus(); };
  const outside = (e) => { if (!exWrap.contains(e.target)) close(false); };
  pop.append(h("p", { class: "muted ver-note" }, qMode === "simple" ? "Start from one, change it, then Save search to keep your version." : "Things the Simple builder can't do: counts, averages, joins."),
    ...list.map(([label, a, b]) => h("button", { type: "button", role: "menuitem", class: "menu-item", onclick: () => { close(true); qApply(qMode === "simple" ? { groups: a, join: "and", sort: b } : { sql: a }); } }, label)));
  pop.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); close(true); } });
  exBtn.onclick = () => {
    if (!pop.hidden) { close(true); return; }
    pop.hidden = false; exBtn.setAttribute("aria-expanded", "true"); pop.style.left = "0px";
    // Keep the menu inside the viewport.
    const r = pop.getBoundingClientRect(), over = r.right - (document.documentElement.clientWidth - 8); if (over > 0) pop.style.left = `${-Math.min(over, r.left - 8)}px`;
    pop.querySelector(".menu-item").focus(); document.addEventListener("pointerdown", outside, true);
  };
  $("examples").replaceChildren(
    mine.length ? h("span", { class: "q-saved-label muted" }, "Saved:") : h("span", { class: "muted q-saved-hint" }, "Your saved searches show up here."),
    ...chips, exWrap);
  $("q-save-sql").replaceChildren(qMode === "sql" ? saveBtn() : "");
}
// The tables the SQL box can read, as in tastr/db.py (internal tables such as cards, track_edits, user_usage and events
// are left out). Columns marked true aren't in a friend's copy of their library on a hosted server.
const Q_SCHEMA = [
  ["tracks", "One row per analyzed track.", [["id", "Track number, used by the other tables as track_id"], ["title, artist, album", "From the file's tags"], ["source_kind", "file or url"], ["source_uri", "Where it came from", true],
    ["local_path", "Where the audio is stored", true], ["sha256", "Fingerprint of the audio file"], ["duration", "Seconds"], ["sample_rate", "Hz"], ["bpm, bpm_confidence", "Tempo, and 0–1 confidence"],
    ["key_name, key_tonic, key_mode", "e.g. A minor; tonic 0–11 (C = 0); major or minor"], ["key_scale", "Scale name"], ["camelot", "e.g. 8A"], ["key_confidence", "0–1"], ["energy", "0–1, whole track"],
    ["loudness_db", "Integrated loudness"], ["stages_json, errors_json", "Which analysis steps ran, and what failed", true], ["analyzed_at", "ISO time of the last analysis"], ["owner", "Who uploaded it (hosted only)", true]]],
  ["tags", "Vibe tags, one row per tag on a track.", [["track_id", "→ tracks.id"], ["tag", "e.g. dark, chill, house tempo"], ["score", "0–1, how strongly it applies"]]],
  ["sections", "Intro, build, drop and so on, in order.", [["track_id, idx", "Track, and position (0 = first)"], ["label, letter", "e.g. drop, and A/B/C for repeats"], ["start, \"end\"", "Seconds (quote \"end\", it's a keyword)"], ["start_bar, end_bar", "Bar numbers, 1-based, inclusive"], ["energy", "0–1 for that section"], ["confidence", "0–1"]]],
  ["features", "Single measurements per track.", [["track_id", "→ tracks.id"], ["name", "e.g. low_end, percussiveness, danceability"], ["value", "Number"]]],
  ["series", "Measurements over time, for the charts.", [["track_id, name", "Track, and e.g. energy"], ["idx, t", "Point number, and seconds"], ["value", "Number"]]],
  ["suggestions", "The ideas on each card.", [["track_id, idx", "Track, and position"], ["category", "e.g. drums, bass, harmony"], ["title, body", "What the card shows"], ["data_json", "Details as JSON (use json_extract)"]]],
  ["lookups", "What online catalogs list for a track, if lookups are on.", [["track_id, provider", "Track, and catalog name"], ["status", "found, not_found, pending, no_tags or error"], ["bpm, key_name", "As the catalog lists them"],
    ["key_tonic, key_mode, camelot", "Parsed from key_name, to compare with tracks"], ["fetched_at", "ISO time"], ["result_json", "The full answer as JSON"]]],
];
function renderSchema(mine) {
  $("q-schema-body").replaceChildren(...Q_SCHEMA.map(([t, about, cols]) => h("div", { class: "q-tbl" },
    h("button", { type: "button", title: `Put SELECT * FROM ${t} in the box`, onclick: () => { $("sql").value = `SELECT * FROM ${t} LIMIT 20`; $("sql").focus(); } }, t),
    h("p", { class: "muted" }, about), h("dl", {}, cols.filter(([, , priv]) => !(mine && priv)).flatMap(([c, d]) => [h("dt", {}, c), h("dd", {}, d)])))));
}
export function setQMode(mode) {
  qMode = mode;
  try { localStorage.setItem("tastr-query-mode", mode); } catch (_) {}
  document.querySelectorAll("#q-mode button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.mode === mode)));
  $("q-simple").hidden = mode !== "simple"; $("q-sql").hidden = mode !== "sql";
  $("q-count").textContent = ""; $("sql-result").replaceChildren(); $("sql-error").textContent = "";
  renderExamples();
  if (mode === "simple") renderSimple();
}
export async function runSql() {
  ++qSeq;  // a Simple search still in flight must not overwrite these results
  $("sql-error").textContent = ""; $("q-count").textContent = "";
  const table = $("sql-result");
  table.replaceChildren();
  const text = $("sql").value;
  try {
    const r = await sql(text);
    table.append(h("tr", {}, r.columns.map((c) => h("th", {}, c))));
    for (const row of r.rows) table.append(h("tr", {}, row.map((v) => h("td", {}, v == null ? "" : String(v)))));
    addHist(text, r.rows.length);
  } catch (e) { $("sql-error").textContent = e.message; addHist(text, null, e.message); }
}

// SQL history: every query you run in SQL mode, newest first, deduplicated; click one to run it again. Simple isn't
// recorded (it re-runs on every change); saved searches keep those. Kept in this browser, per signed-in person.
const HIST_MAX = 30;
const qNorm = (q) => q.replace(/\s+/g, " ").trim().replace(/;$/, "");
let qHist = [], histKey = "tastr-sql-hist";
function loadHist(user) {
  histKey = user ? `tastr-sql-hist:${user}` : "tastr-sql-hist";  // people sharing a browser on a hosted server don't see each other's
  try { const v = JSON.parse(localStorage.getItem(histKey) || "[]"); qHist = Array.isArray(v) ? v.filter((x) => x && typeof x.sql === "string") : []; } catch (_) { qHist = []; }
  renderHist();
}
const saveHist = () => { try { localStorage.setItem(histKey, JSON.stringify(qHist)); } catch (_) {} };
const histAgo = (t) => { const m = Math.round((Date.now() - t) / 60e3); return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`; };
function addHist(text, rows, err) {
  const q = text.trim(); if (!q) return;
  qHist = [{ sql: q, t: Date.now(), rows, err: err || null }, ...qHist.filter((x) => qNorm(x.sql) !== qNorm(q))].slice(0, HIST_MAX);
  saveHist(); renderHist();
}
function renderHist() {
  $("q-hist-n").textContent = qHist.length ? `(${qHist.length})` : "";
  if (!qHist.length) { $("q-hist-body").replaceChildren(h("p", { class: "muted" }, "Queries you run show up here. Click one to run it again.")); return; }
  $("q-hist-body").replaceChildren(h("ol", { class: "q-hist-list" }, ...qHist.map((x, i) => h("li", {},
    h("button", { type: "button", class: "q-hist-run", title: "Run this query again", onclick: () => { $("sql").value = x.sql; runSql(); $("sql").focus(); } }, h("code", {}, x.sql)),
    h("span", { class: "muted q-hist-meta" }, `${histAgo(x.t)} · `, x.err ? h("span", { class: "q-hist-err", title: x.err }, "error") : `${x.rows} row${x.rows === 1 ? "" : "s"}`),
    h("button", { type: "button", class: "q-hist-del", "aria-label": "Remove from history", title: "Remove from history", onclick: () => { qHist.splice(i, 1); saveHist(); renderHist(); } }, "×")))),
  h("button", { type: "button", class: "linklike q-hist-clear", onclick: () => { qHist = []; saveHist(); renderHist(); } }, "Clear history"));
}

const startView = location.hash.slice(1);  // read before anything can change the hash

// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  try { if (localStorage.getItem("tastr-query-mode") === "sql") qMode = "sql"; } catch (_) {}
  renderSchema(false);
  loadHist(null);
  document.querySelectorAll("#q-mode button").forEach((b) => b.addEventListener("click", () => setQMode(b.dataset.mode)));
  $("run-sql").addEventListener("click", runSql);
  qGroups = SIMPLE_EXAMPLES[0][1].map((g) => qGroup(g.conds, g.join));
  refresh().catch((e) => setStatus(e.message, true));
  // A shared (hosted) server turns off link analysis; hide it rather than let it fail. Its SQL box reads only your own library.
  // Admins of a hosted server also get the Admin tab; everyone else never sees it, and #admin stays on the library.
  api("/api/config").then((cfg) => {
    setServerCfg(cfg);
    if (!cfg.links) $("url-form").hidden = true;
    if (!cfg.query) { for (const id of ["q-mode", "help-sql"]) $(id).hidden = true; if (qMode === "sql") setQMode("simple"); }
    renderSchema(cfg.query_scope === "mine");
    if (cfg.hosted) loadHist(cfg.user || "");
    $("sql-scope").textContent = cfg.query_scope === "mine"
      ? "Runs on a copy of your own library: tracks, tags, sections, features, series, suggestions, lookups. One SELECT, stopped after 2 seconds."
      : `One read-only SELECT, stopped after ${cfg.hosted ? 2 : 5} seconds.${cfg.hosted ? " As an admin you query the whole database, everyone's tracks included." : ""}`;
    if (cfg.admin) {
      document.querySelector("nav button[data-view=admin]").hidden = false;
      if (startView === "admin") switchTo("admin");
    }
  }).catch(() => {});
}
