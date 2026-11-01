// Try this in › Strudel: templates (built-in and your own), the template editor, the mash-up pick and "Ask Claude to
// change it" (TAS-67). The fill-in language and the diff live in strudel.js; strudelView (daw.js) puts these together.
// The code only ever runs in strudel.cc, never inside Tastr.

import { api, h, isNum, tracks } from "./core.js";

const JSON_BODY = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

// ---- Templates ----
export const tplState = { pick: "full", mashB: null }; // the picked template and mash-up track, kept for the session
let mine = null; // your templates from the server: [{ id, name, text }], null until loaded
let mineLoad = null;
const mineId = (t) => `u${t.id}`;
export function loadMine(repaint) {
  if (mine || mineLoad) return;
  mineLoad = api("/api/strudel/templates").then((r) => { mine = r || []; repaint(); }).catch(() => { mine = []; }).finally(() => (mineLoad = null));
}
export const findTemplate = (id) => STRUDEL_TEMPLATES.find((t) => t.id === id) || (mine || []).find((t) => mineId(t) === id) || null;
const isMine = (t) => !!t && (mine || []).includes(t);

export function templatesBar(onPick, onEdit) {
  const chip = (t, id, own) => h("button", { type: "button", class: "st-tpl" + (own ? " mine" : ""), role: "radio", "aria-checked": String(tplState.pick === id),
    title: t.tip || "Your template", onclick: () => onPick(id) }, t.name);
  const cur = findTemplate(tplState.pick);
  return h("div", { class: "st-tpls" },
    h("div", { class: "st-tpl-row", role: "radiogroup", "aria-label": "Template" }, h("span", { class: "muted st-tpl-lbl" }, "Template:"),
      ...STRUDEL_TEMPLATES.map((t) => chip(t, t.id, false))),
    h("div", { class: "st-tpl-row", role: "radiogroup", "aria-label": "Your templates" }, h("span", { class: "muted st-tpl-lbl" }, "Yours:"),
      ...(mine == null ? [h("span", { class: "muted st-tpl-none" }, "Loading…")]
        : mine.length ? mine.map((t) => chip(t, mineId(t), true)) : [h("span", { class: "muted st-tpl-none" }, "None yet.")]),
      h("button", { type: "button", class: "linklike st-tpl-new", onclick: () => onEdit(null) }, "+ New template"),
      h("button", { type: "button", class: "linklike st-tpl-new", onclick: () => onEdit(cur) }, isMine(cur) ? "Edit this one" : "Copy this one to edit")));
}

const unknownLine = (unknown) => `Not a fill-in: ${unknown.map((u) => `{${u}}`).join(", ")}. It stays in the code as written. Write {{name}} to keep braces as they are.`;
export const tplWarnText = (unknown) => (unknown.length ? `This template has fill-ins Tastr doesn't know: ${unknown.map((u) => `{${u}}`).join(", ")}. They stay in the code as written.` : "");

// The editor: name, text, fill-ins (click to insert), live preview on the open track, save/delete, export/import.
// start: a template to edit or copy (built-in: a copy; yours: edited in place), { name, text } for Claude's version, or null.
// fillsFor(text): the open track's fill-ins for that text. done(stay): close (stay: reopen on the picked template).
export function templateEditor(c, fillsFor, start, done) {
  const own = isMine(start), builtIn = STRUDEL_TEMPLATES.includes(start);
  const name = h("input", { type: "text", class: "st-ed-name", "aria-label": "Template name", maxlength: "40",
    value: own ? start.name : builtIn ? `${start.name} (my copy)` : start ? start.name : "My template" });
  const ta = h("textarea", { class: "st-ed-text", rows: "14", wrap: "off", spellcheck: "false", "aria-label": "Template code" });
  ta.value = start ? start.text : STRUDEL_NEW_TEXT;
  const prev = h("pre", { class: "st-code st-ed-prev", tabindex: "0", "aria-label": "Preview on this track" });
  const warn = h("p", { class: "st-ed-warn", role: "status", "aria-live": "polite" });
  const say = h("span", { class: "muted st-say", role: "status", "aria-live": "polite" });
  const insert = (k) => { const a = ta.selectionStart ?? ta.value.length, b = ta.selectionEnd ?? a; ta.setRangeText(`{${k}}`, a, b, "end"); ta.focus(); upd(); };
  const short = (v) => { const one = v.split("\n")[0]; return one.length > 34 ? one.slice(0, 33) + "…" : one + (v.includes("\n") ? " …" : ""); };
  const list = h("ul", { class: "st-fills", "aria-label": "Fill-ins" });
  const paintList = (f) => list.replaceChildren(...Object.entries(f).map(([k, x]) =>
    h("li", {}, h("button", { type: "button", class: "linklike st-fill", title: x.d, onclick: () => insert(k) }, `{${k}}`),
      h("span", { class: "muted" }, ` ${x.d}`), h("code", { class: "st-fill-v" }, short(x.v) || "(empty)"))));
  let f = null;
  const upd = () => {
    f = fillsFor(ta.value);
    const r = strudelFill(ta.value, f);
    prev.textContent = r.code;
    warn.textContent = r.unknown.length ? unknownLine(r.unknown) : "";
    if (list.childElementCount !== Object.keys(f).length) paintList(f);
  };
  ta.addEventListener("input", upd);
  const busy = (on) => [save, del, imp].forEach((b) => b && (b.disabled = on));
  const save = h("button", { type: "button", class: "primary", onclick: async () => {
    const body = { name: name.value.trim() || "My template", text: ta.value };
    busy(true);
    try {
      if (own) Object.assign(mine.find((t) => t.id === start.id), await api(`/api/strudel/templates/${start.id}`, JSON_BODY("PUT", body)));
      else { const t = await api("/api/strudel/templates", JSON_BODY("POST", body)); (mine ||= []).push(t); tplState.pick = mineId(t); }
      done();
    } catch (e) { say.className = "error st-say"; say.textContent = e.message; busy(false); }
  } }, "Save");
  const del = own ? h("button", { type: "button", class: "danger", onclick: async () => {
    if (del.dataset.sure !== "1") { del.dataset.sure = "1"; del.textContent = `Delete "${start.name}"?`; return; }
    busy(true);
    try { await api(`/api/strudel/templates/${start.id}`, { method: "DELETE" }); mine = mine.filter((t) => t.id !== start.id); tplState.pick = "full"; done(); }
    catch (e) { say.className = "error st-say"; say.textContent = e.message; busy(false); }
  } }, "Delete") : null;
  const unfill = h("button", { type: "button", onclick: () => { ta.value = strudelUnfill(ta.value, f, false); upd(); say.textContent = "Swapped this track's title, tempo and key for fill-ins"; } },
    "Turn this track's values into fill-ins");
  const exp = h("button", { type: "button", onclick: () => {
    const list = (mine || []).map(({ name, text }) => ({ name, text }));
    const url = URL.createObjectURL(new Blob([JSON.stringify({ tastr_strudel_templates: 1, templates: list }, null, 2)], { type: "application/json" }));
    const a = h("a", { href: url, download: "tastr-strudel-templates.json" });
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    say.textContent = `Exported ${plural(list.length, "template")}`;
  } }, "Export all");
  const file = h("input", { type: "file", accept: ".json,application/json", hidden: "" });
  file.addEventListener("change", async () => {
    let got;
    try {
      const j = JSON.parse(await file.files[0].text());
      got = (j && j.tastr_strudel_templates && Array.isArray(j.templates) ? j.templates : []).filter((t) => t && typeof t.text === "string" && t.text);
      if (!got.length) throw new Error("no templates");
    } catch (_) { say.className = "error st-say"; say.textContent = "That file isn't a Tastr templates file"; file.value = ""; return; }
    busy(true);
    let n = 0;
    try {
      for (const t of got) { (mine ||= []).push(await api("/api/strudel/templates", JSON_BODY("POST", { name: String(t.name || "Imported").slice(0, 40).trim() || "Imported", text: t.text }))); n++; }
      say.className = "muted st-say"; say.textContent = `Imported ${plural(n, "template")}`;
    } catch (e) { say.className = "error st-say"; say.textContent = `Imported ${n} of ${got.length}: ${e.message}`; }
    file.value = ""; busy(false); done(true);
  });
  const imp = h("button", { type: "button", onclick: () => file.click() }, "Import…");
  upd();
  const title = own ? "Edit your template" : builtIn ? `Your copy of "${start.name}"` : start ? "Save Claude's version as a template" : "New template";
  return h("div", { class: "st-ed", role: "group", "aria-label": "Template editor" },
    h("div", { class: "st-ed-head" }, h("b", {}, title), name),
    h("div", { class: "st-ed-grid" },
      h("div", { class: "st-ed-main" }, ta, warn,
        h("details", { class: "st-fills-wrap", open: "" }, h("summary", {}, "Fill-ins: click one to add it at the cursor"), list)),
      h("div", { class: "st-ed-side" }, h("div", { class: "muted st-ed-cap" }, `Preview on ${(c.source && c.source.title) || "this track"}`), prev)),
    h("div", { class: "lb-acts" }, save, h("button", { type: "button", onclick: () => done() }, "Cancel"), unfill, del,
      h("span", { class: "st-ed-gap" }), exp, imp, file, say),
    h("p", { class: "muted st-note" }, "Your templates are saved with your account on this Tastr server. Export all / Import… move them as a .json file."));
}

// ---- Mash-up: the second track ----
const bCards = new Map(); // track id -> its full card (null while it loads)
const fitOf = (c, t) => strudelMashFit({ bpm: c.core && c.core.bpm, camelot: c.core && c.core.key && c.core.key.camelot }, { bpm: t.bpm, camelot: t.camelot });
// Other tracks with a tempo, best fit first: Camelot-compatible after the speed change, then the smallest pitch shift.
export const mashChoices = (c) => tracks.filter((t) => t.id !== c.id && isNum(t.bpm) && t.bpm > 0)
  .map((t) => ({ t, fit: fitOf(c, t) })).sort((a, b) => a.fit.score - b.fit.score);
// The second track's card for a mash-up, fetched once (repaint when it arrives); null while it loads or with none.
export function mashCard(c, repaint) {
  const all = mashChoices(c);
  const id = all.some((x) => x.t.id === tplState.mashB) ? tplState.mashB : all.length ? all[0].t.id : null;
  if (id == null) return null;
  if (!bCards.has(id)) {
    bCards.set(id, null);
    api(`/api/tracks/${id}`).then((card) => { bCards.set(id, card); repaint(); }).catch(() => bCards.delete(id));
  }
  return bCards.get(id);
}
export function mashRow(c, b, onPick) {
  const all = mashChoices(c);
  if (!all.length) return [h("span", { class: "muted" }, "Add a second track to your library to make a mash-up.")];
  const cur = b ? b.id : all.some((x) => x.t.id === tplState.mashB) ? tplState.mashB : all[0].t.id;
  const sel = h("select", { "aria-label": "Second track" }, all.map(({ t, fit }) =>
    h("option", { value: String(t.id), ...(t.id === cur ? { selected: "" } : {}) }, `${t.title || "Untitled"} · ${t.bpm.toFixed(0)} BPM · ${t.camelot || "?"}${fit.ok ? " ✓" : ""}`)));
  sel.addEventListener("change", () => onPick(+sel.value));
  const pick = all.find((x) => x.t.id === cur), fx = pick.fit;
  const text = !b ? "Loading the second track…"
    : (Math.abs(fx.semis) < 0.05 ? `Same tempo, so it plays at normal speed. Key ${pick.t.camelot || "unknown"}: ${fx.fit}`
      : `Speed ×${fx.r} lines the bars up, moving its pitch ${fx.semis >= 0 ? "up" : "down"} ${Math.abs(fx.semis).toFixed(1)} semitones to about ${fx.cam || "an unknown key"}: ${fx.fit}`)
      + (fx.ok ? "." : ". Tracks marked ✓ fit better; they're listed first.");
  return [h("label", { class: "st-opt" }, "Mix with ", sel), h("span", { class: "st-fit " + (!b ? "" : fx.ok ? "ok" : "bad") }, text)];
}

// ---- Ask Claude to change it ----
// Each track and template keeps its own chain of asks; each changes the code from the one before. When the code
// underneath changes (template, track, audio, stems, edits), the chain no longer applies and starts again.
const chains = new Map(); // `${track id}|${template id}` -> { base, steps: [{ ask, say, code }] }
const ASK_EXAMPLES = ["make the drums half-time", "add a filter sweep into the drop", "use an 808 kit", "more reverb", "add some swing", "mute the vocals", "double-time hats"];
let askStatus = null, askLoad = null; // GET /api/strudel/ask

export function claudeCode(key, base) {
  const ch = chains.get(key);
  if (ch && ch.base !== base) chains.delete(key);
  const live = chains.get(key);
  return live && live.steps.length ? live.steps[live.steps.length - 1].code : null;
}

// getCode(): the code shown now. summary(): the track summary for Claude. key(), base(): the chain's key and the plain code.
// onChange(): repaint the view. onSave(code): open the editor with Claude's version.
export function askPanel({ getCode, summary, key, base, onChange, onSave }) {
  const inp = h("input", { type: "text", class: "st-ask-in", maxlength: "500", placeholder: "e.g. make the drums half-time and add a filter sweep into the drop", "aria-label": "Ask Claude to change the code" });
  const out = h("div", { class: "st-ask-out", role: "status", "aria-live": "polite" });
  const left = h("span", { class: "muted st-ask-left" });
  const go = h("button", { type: "button" }, "Ask");
  const off = h("p", { class: "muted st-say st-ask-off" });
  let working = false;
  const canAsk = () => askStatus && askStatus.available && !(askStatus.hosted && askStatus.left <= 0);
  const paintLeft = () => {
    if (!askStatus) { left.textContent = ""; go.disabled = true; return; }
    left.textContent = !askStatus.available ? "" : askStatus.hosted ? `${askStatus.left} of ${askStatus.limit} asks left today` : "Uses your own Anthropic key";
    off.hidden = askStatus.available;
    off.replaceChildren(...(askStatus.reason === "no-sdk" ? ["To ask Claude, install the Anthropic SDK with ", h("code", {}, "pip install 'tastr[claude]'"), ", set ",
      h("code", {}, "ANTHROPIC_API_KEY"), " to your own key, and restart ", h("code", {}, "tastr serve"), "."]
      : askStatus.reason === "no-key" ? ["To ask Claude, set ", h("code", {}, "ANTHROPIC_API_KEY"), " to your own Anthropic API key and restart ", h("code", {}, "tastr serve"), "."]
      : askStatus.reason === "hosted-off" ? ["Ask Claude isn't turned on on this server."] : []));
    go.disabled = working || !canAsk();
    inp.disabled = !askStatus.available;
  };
  if (!askStatus && !askLoad) askLoad = api("/api/strudel/ask").then((r) => (askStatus = r)).catch(() => (askStatus = { available: false, reason: "hosted-off" })).finally(() => { askLoad = null; paint(); });
  else if (askLoad) askLoad.then(() => paint());
  const chips = () => h("div", { class: "st-ask-ex" }, h("span", { class: "muted" }, "Try:"),
    ASK_EXAMPLES.map((ex) => h("button", { type: "button", class: "linklike", ...(canAsk() && !working ? {} : { disabled: "" }), onclick: () => { inp.value = ex; ask(); } }, ex)));
  const ask = async () => {
    const text = inp.value.trim();
    if (!text || working || !canAsk()) return;
    const k = key(), b = base(), from = getCode();
    working = true; paintLeft();
    out.replaceChildren(h("span", { class: "spin", "aria-hidden": "true" }), "Claude is changing the code…");
    try {
      const r = await api("/api/strudel/ask", JSON_BODY("POST", { code: from, ask: text, summary: summary() }));
      if (askStatus.hosted) askStatus.left = r.left;
      if (base() === b && key() === k) { // the view didn't move on while Claude worked
        let ch = chains.get(k);
        if (!ch || ch.base !== b) chains.set(k, (ch = { base: b, steps: [] }));
        ch.steps.push({ ask: text, say: r.explanation, code: r.code });
        inp.value = "";
      }
      working = false;
      onChange();
    } catch (e) {
      working = false;
      if (/asks/.test(e.message) && askStatus.hosted) askStatus.left = 0;
      out.replaceChildren(h("p", { class: "error st-say" }, e.message), chips());
      paintLeft();
    }
  };
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); ask(); } });
  go.onclick = ask;
  const step = (fn) => { fn(); onChange(); };
  function paint() {
    paintLeft();
    if (working) return;
    const ch = chains.get(key());
    if (!ch || ch.base !== base() || !ch.steps.length) { out.replaceChildren(chips()); return; }
    const last = ch.steps[ch.steps.length - 1], prevCode = ch.steps.length > 1 ? ch.steps[ch.steps.length - 2].code : ch.base;
    const rows = strudelHunks(strudelDiff(prevCode, last.code)).map((d) => (d ? h("div", { class: "st-df " + (d[0] === "+" ? "add" : d[0] === "-" ? "del" : "ctx") }, `${d[0]} ${d[1]}`)
      : h("div", { class: "st-df gap", "aria-hidden": "true" }, "⋯")));
    out.replaceChildren(
      h("p", { class: "st-ask-you" }, h("span", { class: "muted" }, "You: "), last.ask),
      h("p", { class: "st-ask-say" }, h("b", {}, "Claude: "), last.say),
      rows.length ? h("div", { class: "st-diff", "aria-label": "What changed" }, rows) : h("p", { class: "muted st-say" }, "No lines changed."),
      h("div", { class: "lb-acts" },
        h("button", { type: "button", onclick: () => step(() => ch.steps.pop()) }, `Undo${ch.steps.length > 1 ? ` (${ch.steps.length} changes)` : ""}`),
        h("button", { type: "button", onclick: () => step(() => chains.delete(key())) }, "Undo all"),
        h("button", { type: "button", onclick: () => onSave(last.code) }, "Save as template"),
        h("span", { class: "muted st-say" }, "The code above has Claude's changes in it.")),
      chips());
  }
  const el = h("div", { class: "st-ask" },
    h("div", { class: "st-ask-row" }, h("b", { class: "st-ask-lbl" }, "Ask Claude to change it"), left),
    h("div", { class: "st-ask-row" }, inp, go), off, out,
    h("p", { class: "muted st-note" }, "Claude sends back new code and you see what changed. It never runs here; the code only plays in strudel.cc."));
  return { el, paint };
}
