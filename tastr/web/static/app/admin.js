// Admin (hosted mode, admins only).

import { $, api, h, svg } from "./core.js";
import { fmtUpLong } from "./library.js";
import { LOOKUP_MARK, stat, switchTo } from "./card.js";
import { fmtBytes } from "./files.js";
import { loadReview, reviewOn } from "./labels.js";
import { serverCfg } from "./daw.js";
import { startViewAs } from "./viewas.js";

// GET /api/admin/usage?range= and /api/admin/events, fetched each time the tab opens (no polling). Days are UTC days, like the limits.
// Cloud cost is an estimate: Modal bills about $0.001 per analysis and $0.008 per stem job (T4, ~50 s), against the
// friends environment's monthly budget, which is set in Modal and not known to the server.
const ADM_TRACK_COST = 0.001, ADM_STEM_COST = 0.008, ADM_BUDGET = 10;
// Ask Claude (Strudel): tokens counted per person per day, priced at the model's list prices the server sends (claude).
let admClaude = { price_in: 2, price_out: 10 };
const askCost = (tin, tout) => (tin * admClaude.price_in + tout * admClaude.price_out) / 1e6;
// The activity log's filters: event types and where they come from (db.EVENT_TYPES and db.EVENT_SOURCES on the server).
const ACT_TYPES = { upload: "Uploads", analysis: "Analysis", stems: "Stem jobs", limit: "Limits hit", error: "Errors", signin: "Sign-ins",
  blocked: "Blocked requests", deploy: "Deploys and CI", backup: "Backups", admin: "Admin changes" };
const ACT_SRC = { app: "Tastr", modal: "Modal", flyio: "Fly.io" /* lines from before the move off Fly */, github: "GitHub", cloudflare: "Cloudflare", r2: "R2 (Litestream)" };
const actF = { type: "", who: "", src: "", when: "", from: "", to: "" };
let admData = null, admEvents = null, admKind = "tracks", admRange = "30", admLog = "all", admFocus = null, admEdit = null;
const admColor = (i) => `var(--sec${i % 5})`;
const admMeter = (v, hot) => h("div", { class: "adm-meter" + (hot ? " hot" : ""), style: `--v:${Math.min(1, v || 0)}` }, h("i"));
const admGB = (bytes) => `${(bytes / 1024 ** 3).toFixed(bytes >= 1024 ** 3 ? 1 : 2)} GB`;
const admName = (email) => (email ? email.split("@")[0] : "");
function admAgo(iso) {
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (!isFinite(s)) return "–";
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  return s < 172800 ? "yesterday" : `${Math.floor(s / 86400)} days`;
}
const admDaysAgo = (n) => (n === 0 ? "today" : n === 1 ? "yesterday" : `${n} days ago`);
const ADM_RANGE_LABEL = { 1: "today, by hour (UTC)", 7: "last 7 days", 30: "last 30 days", all: "all time" };
export async function loadAdmin() {
  try {
    admData = await api(`/api/admin/usage?range=${admRange}`);
    $("adm-error").textContent = "";
    renderAdmin();
  } catch (e) { $("adm-error").textContent = e.message; }
  loadEvents();
  loadRb();
}
// "When": today and the last 7 or 30 days count back from today's UTC day; "Between dates" uses the From and To days.
function actQuery() {
  const q = new URLSearchParams(), dayIso = (ms) => new Date(ms).toISOString().slice(0, 10);
  if (actF.type) q.set("type", actF.type);
  if (actF.who) q.set("who", actF.who);
  if (actF.src) q.set("source", actF.src);
  if (actF.when === "range") { if (actF.from) q.set("from", actF.from); if (actF.to) q.set("to", actF.to); }
  else if (actF.when) q.set("from", dayIso(Date.now() - (+actF.when - 1) * 86400000));
  if (admLog === "problems") q.set("problems", "1");
  return q;
}
let admEventsSeq = 0;
async function loadEvents() {
  const seq = ++admEventsSeq;
  try {
    const r = await api(`/api/admin/events?${actQuery()}`);
    if (seq !== admEventsSeq) return;  // a newer filter change already asked
    admEvents = r;
    fillActFilters();
    renderActivity();
  } catch (e) { $("adm-error").textContent = e.message; }
}
function renderAdmin() {
  const A = admData;
  if (!A) return;
  const L = A.limits, dayMs = 86400000, today = Date.parse(A.today + "T00:00:00Z");
  if (A.claude) admClaude = A.claude;
  // One row per person, with 30 days of [tracks, stem jobs] (index 29 is today) from the usage history.
  const all = A.users.map((u) => ({ ...u, admin: A.admins.includes(u.email), days: Array.from({ length: 30 }, () => [0, 0, 0, 0]),
    active: false, lastDay: null, bytes: u.audio_bytes + u.stems_bytes }));
  const P = all.filter((p) => p.email), byEmail = Object.fromEntries(P.map((p) => [p.email, p]));
  for (const x of A.history) {
    const p = byEmail[x.user], i = 29 - Math.round((today - Date.parse(x.day + "T00:00:00Z")) / dayMs);
    if (!p || i < 0 || i > 29) continue;
    if (x.kind === "tracks") p.days[i][0] += x.n;
    if (x.kind === "stems") p.days[i][1] += x.n;
    if (x.kind === "ask_in") p.days[i][2] += x.n;
    if (x.kind === "ask_out") p.days[i][3] += x.n;
    if (i >= 23) p.active = true;
    p.lastDay = Math.max(p.lastDay ?? 0, i);
  }
  for (const p of all) {
    p.tracks30 = p.days.reduce((a, d) => a + d[0], 0); p.stems30 = p.days.reduce((a, d) => a + d[1], 0);
    p.claude = askCost(p.days.reduce((a, d) => a + d[2], 0), p.days.reduce((a, d) => a + d[3], 0));
    p.cost = p.tracks30 * ADM_TRACK_COST + p.stems30 * ADM_STEM_COST + p.claude;
  }
  const sum = (f) => P.reduce((a, p) => a + f(p), 0), cost = sum((p) => p.cost);
  const used = A.storage.used_bytes, capB = A.storage.cap_bytes, an = A.analyses;
  const tile = (value, label, meter) => h("div", { class: "adm-tile" }, stat(value, label), meter || null);
  $("adm-tiles").replaceChildren(
    tile(String(P.length), `people, ${P.filter((p) => p.active).length} active this week, ${sum((p) => p.today.visits)} visits today`),
    tile(String(sum((p) => p.today.tracks)), `tracks today, ${sum((p) => p.tracks30)} in 30 days`),
    tile(String(sum((p) => p.today.stems)), `stem jobs today, ${sum((p) => p.stems30)} in 30 days`),
    tile(admGB(used), `stored of ${L.storage_gb} GB`, admMeter(used / capB, used / capB >= 0.9)),
    tile(`${an.running} / ${an.max}`, `analyzing now, ${an.waiting} waiting`, admMeter(an.running / an.max, an.running / an.max >= 0.9)),
    tile(`$${cost.toFixed(2)}`, `cloud cost, 30 days (est.), $${sum((p) => p.claude).toFixed(2)} of it Claude, ${sum((p) => p.today.asks || 0)} asks today`,
      admMeter(cost / ADM_BUDGET, cost / ADM_BUDGET >= 0.9)));
  renderAdmChart(A, P);
  renderPeople(A, all, P);
  const kv = [["Upload size", `${L.max_upload_mb} MB`, "TASTR_MAX_UPLOAD_MB"], ["Track length", `${L.max_minutes} min`, "TASTR_MAX_MINUTES"], ["Files per upload", L.max_files, "TASTR_MAX_FILES"],
    ["Tracks per person per day", L.daily_tracks, "TASTR_DAILY_TRACKS"], ["Stem jobs per person per day", L.daily_stems, "TASTR_DAILY_STEMS"], ["Claude asks per person per day", L.daily_asks, "TASTR_DAILY_ASKS"],
    ["Total storage", `${L.storage_gb} GB`, "TASTR_STORAGE_GB"], ["Analyses at once", L.max_analyses, "TASTR_MAX_ANALYSES"],
    ["Modal budget (friends env)", `$${ADM_BUDGET}/mo`, "set in Modal"]];
  $("adm-limits").replaceChildren(h("tbody", {}, ...kv.map(([a, b, c]) => h("tr", {}, h("td", {}, a, h("br"), h("code", {}, c)), h("td", {}, String(b))))));
  // Cloudflare dashboard links, from TASTR_CF_ACCOUNT_ID (hidden without it)
  const cf = (serverCfg && serverCfg.cf_links) || {};
  $("adm-cf-apps").hidden = !cf.apps;
  if (cf.apps) $("adm-cf-apps").querySelector("a").href = cf.apps;
  $("adm-blocked").replaceChildren(`Blocked today: ${A.blocked_today} request${A.blocked_today === 1 ? "" : "s"} without a valid Access token. `,
    ...(cf.access_logs ? [h("a", { class: "adm-ext", href: cf.access_logs, target: "_blank", rel: "noopener",
      title: "Cloudflare Zero Trust, Logs, Access: each request with its address, country and reason" }, "See who and why, in Cloudflare ↗")] : []));
}
// Stacked bars, one color per person: by hour for Today, by day otherwise. Sized from the container so the text doesn't scale.
function renderAdmChart(A, P) {
  const S = A.series, hourly = S.hourly, n = S.labels.length;
  $("adm-range-label").textContent = admRange === "all" ? `all time (${n} day${n === 1 ? "" : "s"})` : ADM_RANGE_LABEL[admRange];
  const what = { tracks: "tracks", stems: "stem jobs", visits: "visits", storage: "GB stored", cost: "cloud cost" }[admKind];
  const val = (p, i) => {
    const s = S.people[p.email];
    if (!s || (hourly && i > S.now_hour)) return 0;
    if (admKind === "storage") return s.storage[i] / 1024 ** 3;
    if (admKind === "cost") return s.tracks[i] * ADM_TRACK_COST + s.stems[i] * ADM_STEM_COST + askCost((s.ask_in || [])[i] || 0, (s.ask_out || [])[i] || 0);
    return s[admKind][i];
  };
  const fmtV = (v) => admKind === "storage" ? `${v.toFixed(v >= 1 ? 1 : 2)} GB` : admKind === "cost" ? `$${v.toFixed(v >= 0.1 ? 2 : 3)}` : `${v} ${what}`;
  const fmtAxis = (v) => admKind === "storage" ? v.toFixed(1) : admKind === "cost" ? `$${v.toFixed(2)}` : String(Math.round(v));
  const sum = (f) => P.reduce((a, p) => a + f(p), 0);
  const totals = Array.from({ length: n }, (_, i) => sum((p) => val(p, i)));
  const W = Math.max(300, $("adm-chart").clientWidth || 600), H = 160, pad = 34, top0 = 8, bw = (W - pad) / n;
  const max = Math.max(admKind === "storage" ? 1 : admKind === "cost" ? 0.05 : 4, ...totals), y = (v) => (H - 16 - top0) * v / max;
  const when = (i) => hourly ? `${String(i).padStart(2, "0")}:00 UTC today` : admDaysAgo(n - 1 - i);
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${what} per ${hourly ? "hour today" : "day"}, ${$("adm-range-label").textContent}` });
  for (const t of [0, max / 2, max]) g.append(svg("text", { x: 0, y: H - 16 - y(t) + 3 }, fmtAxis(t)));
  const gap = bw > 4 ? 1 : 0;
  for (let i = 0; i < n; i++) {
    let top = H - 16;
    P.forEach((p, j) => {
      const v = val(p, i);
      if (!v) return;
      const hh = y(v); top -= hh;
      g.append(svg("rect", { class: "bar" + (admFocus === j ? " on" : ""), x: pad + i * bw + gap, y: top, width: Math.max(1, bw - (gap ? 2 : 0.5)), height: hh, rx: gap, fill: admColor(j) },
        svg("title", {}, `${admName(p.email)}: ${fmtV(v)}, ${when(i)}`)));
    });
    if (hourly && i === S.now_hour) g.append(svg("line", { x1: pad + (i + 1) * bw, x2: pad + (i + 1) * bw, y1: top0, y2: H - 16, stroke: "var(--muted)", "stroke-dasharray": "2 3" }));
  }
  g.append(svg("text", { x: pad, y: H - 2 }, hourly ? "00:00 UTC" : `${n - 1 ? `${n - 1} days ago` : "today"}`), svg("text", { x: W, y: H - 2, "text-anchor": "end" }, hourly ? "24:00" : "today"));
  if (hourly) g.append(svg("text", { x: pad + (S.now_hour + 1) * bw, y: H - 2, "text-anchor": "middle" }, "now"));
  $("adm-chart").classList.toggle("focus", admFocus != null);
  $("adm-chart").replaceChildren(g);
  const note = admKind === "storage" ? (hourly ? "Audio and stems each person has now, counted from the hour they were uploaded. Tracks added before sharing (no owner) aren't shown."
    : "Audio and stems stored at the end of each day (recorded once a day, and when this tab opens). Tracks added before sharing (no owner) aren't shown.")
    : admKind === "cost" ? `Estimated from Modal run time: ~$${ADM_TRACK_COST} per analysis, ~$${ADM_STEM_COST} per stem job, plus Ask Claude's tokens at ${admClaude.model || "the model"}'s list prices. Not from Modal's or Anthropic's billing.`
    : admKind === "visits" ? "A visit is a person's first request after 30 minutes without one." : null;
  $("adm-legend").replaceChildren(...P.map((p, j) => h("span", {}, h("i", { style: `background:${admColor(j)}` }), p.email)),
    ...(note ? [h("span", { class: "muted adm-note" }, note)] : []));
}
// People, with today's quotas against each person's own caps (an admin's change, or the server's), and the ✎ to change them.
function renderPeople(A, all, P) {
  const L = A.limits;
  const quota = (p, k, n) => {
    const capN = p.caps[k] ?? L[`daily_${k}`], o = p.override, def = L[`daily_${k}`], raised = o && o[`daily_${k}`] != null;
    const tip = raised ? `Changed from the default of ${def}${o.until_day ? ", today only (until 00:00 UTC)" : ", until an admin changes it"}` : "";
    return h("div", { class: "adm-quota" }, h("span", { title: tip }, `${n} / ${capN}`, raised ? h("span", { class: "adm-raised" }, o.until_day ? " ↑ today" : " ↑") : null),
      admMeter(capN ? n / capN : 1, n >= capN),
      n >= capN && !raised ? h("button", { type: "button", class: "linklike adm-raise", onclick: (e) => { e.stopPropagation(); admEdit = p.email; renderAdmin(); } }, "Raise") : null);
  };
  const keepCell = (p) => {
    if (p.admin) return h("span", { class: "muted", title: "Admins' audio is always kept" }, "Forever");
    const set = p.email && p.audio_days != null, d = set ? p.audio_days : L.audio_days;
    return set ? h("span", { title: d ? `Uploads and stems are deleted ${daysText(d).toLowerCase()} after upload. The cards stay.` : "Kept forever, set for this person" }, daysText(d))
      : h("span", { class: "muted", title: `The server default (TASTR_AUDIO_DAYS${L.audio_days ? `, ${daysText(L.audio_days).toLowerCase()}` : ", keep everything"})` }, daysText(d));
  };
  const seen = (p) => (p.last_seen ? admAgo(p.last_seen).replace(/^(\d.*)$/, "$1 ago") : p.lastDay != null ? admDaysAgo(29 - p.lastDay) : "–");
  $("adm-people").replaceChildren(
    h("thead", {}, h("tr", {}, ...["Person", "Last seen", "Visits today", "Tracks today", "Stems today", "Claude asks today", "Tracks, 30 d", "Library", "Storage", "Keep audio", "Cost, 30 d", ""]
      .map((t, i) => h("th", { class: i === 2 || (i > 5 && i < 9) || i === 10 ? "num" : "" }, t, i === 11 ? h("span", { class: "sr-only" }, "Limits and audio") : null)))),
    h("tbody", {}, ...all.flatMap((p) => {
      if (!p.email) return [h("tr", { class: "adm-none" }, h("td", {}, "No owner", h("span", { class: "muted" }, " · added before sharing")), h("td", { colspan: "6" }),
        h("td", { class: "num" }, String(p.tracks)), h("td", { class: "num", title: "audio + stems" }, admGB(p.bytes)), h("td", {}, keepCell(p)), h("td"), h("td"))];
      const j = P.indexOf(p), pick = () => { admFocus = admFocus === j ? null : j; renderAdmin(); };
      const tr = h("tr", { class: admFocus === j ? "sel" : "", tabindex: "0", title: "Pick out in the chart", onclick: pick,
        onkeydown: (e) => { if (e.target === tr && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); pick(); } } },
        h("td", {}, h("span", { class: "adm-who", style: `background:${admColor(j)}` }), p.email, p.admin ? h("span", { class: "muted" }, " · admin") : null),
        h("td", {}, seen(p)), h("td", { class: "num" }, String(p.today.visits)),
        h("td", {}, quota(p, "tracks", p.today.tracks)), h("td", {}, quota(p, "stems", p.today.stems)), h("td", {}, quota(p, "asks", p.today.asks || 0)),
        h("td", { class: "num" }, String(p.tracks30)), h("td", { class: "num" }, String(p.tracks)),
        h("td", { class: "num", title: "audio + stems" }, admGB(p.bytes)), h("td", {}, keepCell(p)), h("td", { class: "num", title: p.claude ? `$${p.claude.toFixed(2)} of it Ask Claude` : null }, `$${p.cost.toFixed(2)}`),
        h("td", {}, h("div", { class: "adm-acts" }, p.email !== serverCfg.user ? h("button", { type: "button", class: "adm-va-btn",
          title: `See Tastr exactly as ${p.email.split("@")[0]} sees it. Read-only: you can't change anything while viewing.`,
          onclick: (e) => { e.stopPropagation(); startViewAs(p.email).catch((err) => { $("adm-error").textContent = err.message; }); } }, "View as") : null,
          h("button", { type: "button", class: "adm-lim-btn", "aria-expanded": String(admEdit === p.email), title: editTitle(p), "aria-label": editTitle(p), onclick: (e) => { e.stopPropagation(); admEdit = admEdit === p.email ? null : p.email; renderAdmin(); } }, "✎"))));
      return admEdit === p.email ? [tr, limitRow(p, L)] : [tr];
    })));
}
const editTitle = (p) => `Change ${p.email.split("@")[0]}'s daily limits${p.admin ? "" : " and how long their audio is kept"}`;
// "Forever", "30 days", "1 year": how long audio is kept (0 = forever).
const daysText = (d) => (!d ? "Forever" : d % 365 === 0 ? (d === 365 ? "1 year" : `${d / 365} years`) : `${d} day${d === 1 ? "" : "s"}`);
// The inline row under a person: their daily caps (empty = the server default), today only or until changed.
function limitRow(p, L) {
  const o = p.override || {};
  const tIn = h("input", { type: "number", min: "0", max: "10000", value: o.daily_tracks ?? "", placeholder: String(L.daily_tracks), "aria-label": "Tracks per day" });
  const sIn = h("input", { type: "number", min: "0", max: "1000", value: o.daily_stems ?? "", placeholder: String(L.daily_stems), "aria-label": "Stem jobs per day" });
  const aIn = h("input", { type: "number", min: "0", max: "1000", value: o.daily_asks ?? "", placeholder: String(L.daily_asks ?? 20), "aria-label": "Claude asks per day" });
  const until = h("select", { "aria-label": "How long" }, h("option", { value: "today" }, "Today only (resets at 00:00 UTC)"), h("option", { value: "standing" }, "Until I change it"));
  until.value = p.override && !o.until_day ? "standing" : "today";
  const err = h("span", { class: "error", role: "alert" });
  const url = `/api/admin/limits/${encodeURIComponent(p.email)}`;
  const send = async (opts) => {
    try { await api(url, opts); admEdit = null; await loadAdmin(); }
    catch (e) { err.textContent = e.message; }
  };
  const save = () => {
    const t = tIn.value === "" ? null : +tIn.value, st = sIn.value === "" ? null : +sIn.value, ak = aIn.value === "" ? null : +aIn.value;
    if ([t, st, ak].some((v) => v != null && (!Number.isInteger(v) || v < 0))) { err.textContent = "Limits are whole numbers, 0 or more."; return; }
    send({ method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ daily_tracks: t, daily_stems: st, daily_asks: ak, scope: until.value }) });
  };
  setTimeout(() => tIn.focus(), 0);
  return h("tr", { class: "adm-lim-row" }, h("td", { colspan: "12" },
    h("div", { class: "adm-lim", onkeydown: (e) => { if (e.key === "Enter" && e.target.tagName === "INPUT") save(); if (e.key === "Escape") { admEdit = null; renderAdmin(); } } },
      h("b", {}, `Daily limits for ${p.email}`),
      h("label", {}, "Tracks per day", tIn), h("label", {}, "Stem jobs per day", sIn), h("label", {}, "Claude asks per day", aIn), h("label", {}, "For", until),
      h("button", { type: "button", class: "primary", onclick: save }, "Save"),
      p.override ? h("button", { type: "button", class: "linklike", onclick: () => send({ method: "DELETE" }) }, "Back to defaults") : null,
      h("button", { type: "button", class: "linklike", onclick: () => { admEdit = null; renderAdmin(); } }, "Cancel"),
      h("span", { class: "muted" }, `Empty means the server default (${L.daily_tracks} tracks, ${L.daily_stems} stem jobs, ${L.daily_asks ?? 20} Claude asks, from TASTR_DAILY_*). Only this person changes.`), err),
    keepBlock(p, L)));
}
// How long a person's uploaded audio is kept, with its own Save: the limits' "Today only / Until I change it" doesn't
// apply here. PUT /api/admin/audio-days/{email} {days} (0 = forever); DELETE = back to the server default.
function keepBlock(p, L) {
  const who = p.email.split("@")[0];
  if (p.admin) return h("div", { class: "adm-lim adm-keep" }, h("b", {}, "Keep audio"), h("span", { class: "muted" }, "Admins' audio is always kept."));
  const set = p.audio_days != null, cur = set ? p.audio_days : L.audio_days || 0;
  const PRE = [[0, "Forever"], [7, "7 days"], [30, "30 days"], [90, "90 days"], [365, "1 year"]];
  const sel = h("select", { "aria-label": `Keep ${who}'s audio for` }, ...PRE.map(([v, l]) => h("option", { value: String(v) }, l)), h("option", { value: "custom" }, "Custom…"));
  const num = h("input", { type: "number", min: "1", max: "3650", step: "1", "aria-label": "Days" });
  const daysLbl = h("span", {}, "days");
  if (PRE.some(([v]) => v === cur)) sel.value = String(cur); else { sel.value = "custom"; num.value = String(cur); }
  const warn = h("p", { class: "adm-keep-warn", role: "status", "aria-live": "polite" }), err = h("span", { class: "error", role: "alert" });
  const pick = () => (sel.value === "custom" ? (num.value === "" ? NaN : +num.value) : +sel.value);
  const upd = () => {
    num.hidden = daysLbl.hidden = sel.value !== "custom"; err.textContent = "";
    const d = pick(), shorter = Number.isInteger(d) && d > 0 && (cur === 0 || d < cur), span = daysText(d).toLowerCase();
    warn.textContent = shorter ? `${who}'s audio older than ${span} will be deleted within 6 hours. From then on it's deleted ${span} after upload; the cards stay.` : "";
    warn.hidden = !shorter;
  };
  const url = `/api/admin/audio-days/${encodeURIComponent(p.email)}`;
  const send = async (opts) => {
    try { await api(url, opts); admEdit = null; await loadAdmin(); }
    catch (e) { err.textContent = e.message; }
  };
  sel.onchange = () => { upd(); if (sel.value === "custom") num.focus(); };
  num.oninput = upd;
  const save = () => {
    const d = pick();
    if (!Number.isInteger(d) || d < 0 || d > 3650 || (sel.value === "custom" && d < 1)) { err.textContent = "Days are a whole number from 1 to 3650."; return; }
    send({ method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ days: d }) });
  };
  upd();
  return h("div", { class: "adm-lim adm-keep", onkeydown: (e) => { if (e.key === "Enter" && e.target === num) save(); } },
    h("b", {}, `Keep audio for ${p.email}`),
    h("label", {}, "Keep audio for", sel), h("label", { class: "adm-keep-days" }, num, daysLbl),
    h("button", { type: "button", class: "primary", onclick: save }, "Save"),
    set ? h("button", { type: "button", class: "linklike", onclick: () => send({ method: "DELETE" }) }, "Back to default") : null,
    warn, err,
    h("span", { class: "muted" }, `The server default is ${daysText(L.audio_days || 0).toLowerCase()} (TASTR_AUDIO_DAYS). When the time is up, their uploads and stems are deleted: playback, the waveform, stems and re-analysis go. The cards stay.`));
}
// Recent activity: filtered on the server by type, person, source and time (AND), each line with its source.
function fillActFilters() {
  const opt = (v, l) => h("option", { value: v }, l);
  $("af-type").replaceChildren(opt("", "Everything"), ...Object.entries(ACT_TYPES).map(([k, v]) => opt(k, v)));
  const whos = [...new Set([...(admEvents ? admEvents.people : []), ...(actF.who && actF.who !== "-" ? [actF.who] : [])])];
  $("af-who").replaceChildren(opt("", "Anyone"), ...whos.map((w) => opt(w, w)), opt("-", "System (no person)"));
  $("af-src").replaceChildren(opt("", "All sources"), ...Object.entries(ACT_SRC).map(([k, v]) => opt(k, v)));
  $("af-type").value = actF.type; $("af-who").value = actF.who; $("af-src").value = actF.src; $("af-when").value = actF.when;
  $("af-from").value = actF.from; $("af-to").value = actF.to; $("af-range").hidden = actF.when !== "range";
}
function renderActivity() {
  const E = admEvents, A = admData;
  if (!E) return;
  const links = (A && A.log_links) || {};
  // Worker logs · Modal, each only when the server sent its URL.
  const linkEls = () => [links.worker && h("a", { href: links.worker, target: "_blank", rel: "noopener", title: "The Cloudflare Worker's logs, in the Cloudflare dashboard" }, "Worker logs"),
    links.modal && h("a", { href: links.modal, target: "_blank", rel: "noopener" }, "Modal")].filter(Boolean);
  // A deploy links to its GitHub Actions run; a problem links to the Worker's and Modal's logs.
  const own = (e) => e.link && /^https:\/\//.test(e.link) ? [h("a", { href: e.link, target: "_blank", rel: "noopener" }, /github\.com/.test(e.link) ? "Deploy run on GitHub" : "Details")] : [];
  const dig = (e) => { const as = [...own(e), ...(e.ok || e.type === "blocked" ? [] : linkEls())]; return as.length ? h("span", { class: "adm-dig" }, ...as.flatMap((a, i) => (i ? [" · ", a] : [a]))) : null; };
  const active = Boolean(actF.type || actF.who || actF.src || (actF.when && (actF.when !== "range" || actF.from || actF.to)));
  const shown = E.events.length < E.matched ? ` (newest ${E.events.length} shown)` : "";
  $("af-n").textContent = active || admLog === "problems" ? `${E.matched} of ${E.total} events${shown}` : `${E.total} events${shown}`;
  $("af-clear").hidden = !active;
  $("adm-logseg").querySelector('[data-k="problems"]').textContent = `Problems${A && A.problems && A.problems.length ? ` (${A.problems.length})` : ""}`;
  $("adm-log").replaceChildren(...(E.events.length ? E.events.map((e) => h("li", { class: e.ok ? "" : "warn" },
    h("time", { datetime: e.at, title: fmtUpLong(Date.parse(e.at)) }, admAgo(e.at)),
    h("span", {}, e.user ? h("b", { title: e.user }, admName(e.user) + " ") : null, h("span", {}, e.detail), dig(e)),
    h("span", { class: "adm-src", "data-src": e.source, title: ACT_TYPES[e.type] || e.type }, ACT_SRC[e.source] || e.source)))
    : [h("li", { class: "adm-empty muted" }, active ? ["Nothing matches these filters. ", h("button", { type: "button", class: "linklike", onclick: clearActFilters }, "Clear filters")]
      : admLog === "problems" ? "No problems logged." : "Nothing yet.")]));
  $("adm-loglinks").replaceChildren(...(linkEls().length ? ["Full logs: ", ...linkEls().flatMap((a, i) => (i ? [" · ", a] : [a])), ". Search them for an upload's ref."] : []));
}
function clearActFilters() { Object.keys(actF).forEach((k) => (actF[k] = "")); fillActFilters(); loadEvents(); }

// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  [["af-type", "type"], ["af-who", "who"], ["af-src", "src"], ["af-from", "from"], ["af-to", "to"]].forEach(([id, k]) => $(id).addEventListener("change", (e) => { actF[k] = e.target.value; loadEvents(); }));
  $("af-when").addEventListener("change", (e) => { actF.when = e.target.value; $("af-range").hidden = actF.when !== "range"; loadEvents(); });
  $("af-clear").addEventListener("click", clearActFilters);
  document.querySelectorAll("#adm-logseg button").forEach((b) => b.addEventListener("click", () => {
    admLog = b.dataset.k;
    document.querySelectorAll("#adm-logseg button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    loadEvents();
  }));
  document.querySelectorAll("#adm-kind button").forEach((b) => b.addEventListener("click", () => {
    admKind = b.dataset.k;
    document.querySelectorAll("#adm-kind button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    renderAdmin();
  }));
  document.querySelectorAll("#adm-range button").forEach((b) => b.addEventListener("click", async () => {
    admRange = b.dataset.r;
    document.querySelectorAll("#adm-range button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    try { admData = await api(`/api/admin/usage?range=${admRange}`); renderAdmin(); } catch (e) { $("adm-error").textContent = e.message; }
  }));
  window.addEventListener("resize", () => { if (!$("view-admin").hidden) renderAdmin(); });
}

/* Admin › Rekordbox export (sandbox v203, rekordbox-upload-spec.md). POST /api/admin/rekordbox/upload (the XML as the
   body) stores it and answers the comparison without saving anything; POST /api/admin/rekordbox/load then stores
   Rekordbox's BPM and key on the matched tracks. Both match against your own library. */
let rbStatus = null, rbStep = { s: "idle" };
async function loadRb() {
  try { rbStatus = await api("/api/admin/rekordbox/status"); } catch (_) { rbStatus = null; }
  renderRb();
}
const RB_FMT = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: "UTC" });
// A compare row's marks, as the card shows them: the listing (Rekordbox) relative to ours.
const rbBpmMark = (r) => (r.acc1 === 1 ? "agrees" : r.octave === "x2" ? "half" : r.octave === "/2" ? "double" : "differs");
const rbKeyMark = (r) => ({ correct: "agrees", other: "differs" })[r.key_category] || r.key_category || null;
const rbMark = (cmp) => (cmp && LOOKUP_MARK[cmp] ? h("span", { class: `lk-mark lk-${cmp}`, title: LOOKUP_MARK[cmp][1] }, LOOKUP_MARK[cmp][0]) : null);
const rbVal = (bpm, key) => [bpm != null ? `${Math.round(bpm * 10) / 10}` : "no BPM", key || "no key"];
function renderRb() {
  const box = $("adm-rb"); if (!box) return;
  const st = rbStep, kids = [], maxMb = (rbStatus && rbStatus.max_mb) || 50;
  const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0);
  const idle = () => { rbStep = { s: "idle" }; renderRb(); };
  const loaded = rbStatus && rbStatus.tracks > 0;
  kids.push(loaded
    ? h("p", { class: "rb-now" }, h("b", {}, "Loaded "), rbStatus.loaded_at ? `${RB_FMT.format(new Date(rbStatus.loaded_at))} UTC${rbStatus.file ? ` from ${rbStatus.file}` : ""}: ` : "", `${rbStatus.tracks} of your tracks show Rekordbox's BPM and key.`)
    : h("p", { class: "rb-now" }, h("b", {}, "Not loaded yet. "), "No track shows Rekordbox's values until you load an export."));
  if (st.s === "idle" || st.s === "error") {
    const fileIn = h("input", { type: "file", accept: ".xml,application/xml,text/xml", hidden: "" });
    const start = async (file) => {
      if (file.size > maxMb * 1024 ** 2) { rbStep = { s: "error", msg: `${file.name} is ${fmtBytes(file.size)}. The limit is ${maxMb} MB: export a playlist or a smaller collection.` }; return renderRb(); }
      rbStep = { s: "busy", say: `Uploading ${file.name} (${fmtBytes(file.size)})…` }; renderRb();
      try {
        const up = fetch(`/api/admin/rekordbox/upload?name=${encodeURIComponent(file.name)}`, { method: "POST", body: file, headers: { "Content-Type": "application/xml" } });
        setTimeout(() => { if (rbStep.s === "busy") { rbStep = { s: "busy", say: "Reading the export and matching it to your library…" }; renderRb(); } }, 600);
        const r = await up;
        if (!r.ok) { let d = null; try { d = (await r.json()).detail; } catch (_) {} throw new Error(d || `The upload failed (${r.status} ${r.statusText}).`); }
        rbStep = { s: "preview", p: await r.json() };
      } catch (e) { rbStep = { s: "error", msg: e.message }; }
      renderRb();
    };
    fileIn.addEventListener("change", () => { if (fileIn.files[0]) start(fileIn.files[0]); });
    const pick = h("div", { class: "rb-drop", tabindex: "0", role: "button", "aria-label": "Choose a Rekordbox export (.xml)",
      onclick: () => fileIn.click(), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileIn.click(); } },
      ondragenter: (e) => { e.preventDefault(); e.stopPropagation(); }, ondragover: (e) => { e.preventDefault(); e.stopPropagation(); e.currentTarget.classList.add("over"); },
      ondragleave: (e) => e.currentTarget.classList.remove("over"),
      ondrop: (e) => { e.preventDefault(); e.stopPropagation(); e.currentTarget.classList.remove("over"); const f = e.dataTransfer.files[0]; if (f) start(f); } },
      h("b", {}, "Drop your export here"), " or ", h("span", { class: "link" }, "choose the .xml file"), fileIn);
    kids.push(h("p", { class: "muted rb-how" }, "In Rekordbox: ", h("b", {}, "File › Export Collection in xml format"), ". Tastr reads each track's BPM and key and matches it to your library by file name, or by artist and title. Your export isn't shared with anyone."), pick);
    if (st.s === "error") kids.push(h("p", { class: "rb-err", role: "alert" }, st.msg));
  } else if (st.s === "busy") {
    kids.push(h("p", { class: "rb-busy", role: "status" }, h("span", { class: "spin", "aria-hidden": "true" }), st.say));
  } else if (st.s === "preview") {
    const p = st.p, m = p.rows, byFile = p.matched_by["file name"] || 0;
    const okB = m.filter((x) => x.acc1 === 1).length, okK = m.filter((x) => x.key_correct === 1).length;
    if (!m.length) {
      kids.push(h("div", { class: "rb-pre" }, h("h3", {}, `Nothing to load from ${p.file}`),
        h("p", {}, `None of the ${p.rekordbox_tracks.toLocaleString()} tracks in the export match a track in your Tastr library, by file name or by artist and title.`),
        h("p", { class: "muted" }, "Check it's the right Rekordbox library, or upload those tracks to Tastr first."),
        h("div", { class: "menu-confirm-btns" }, h("button", { type: "button", onclick: idle }, "Choose another file"))));
    } else {
      const list = h("details", { class: "rb-list" }, h("summary", {}, `Show the ${m.length} matches`),
        h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Track", "Tastr", "Rekordbox", "Matched by"].map((t) => h("th", {}, t)))),
          h("tbody", {}, ...m.map((x) => {
            const [tb, tk] = rbVal(x.tastr_bpm, x.tastr_key), [rbb, rbk] = rbVal(x.rekordbox_bpm, x.rekordbox_key);
            return h("tr", { class: x.acc1 === 0 || x.key_correct === 0 ? "rb-dis" : "" }, h("td", {}, x.title), h("td", {}, `${tb} · ${tk}`),
              h("td", {}, rbb, rbMark(x.rekordbox_bpm != null ? rbBpmMark(x) : null), ` · ${rbk}`, rbMark(rbKeyMark(x))), h("td", { class: "muted" }, x.matched_by));
          })))));
      const load = h("button", { type: "button", class: "primary" }, `Load Rekordbox values onto ${m.length} tracks`);
      load.onclick = async () => {
        load.disabled = true; load.replaceChildren(h("span", { class: "spin", "aria-hidden": "true" }), "Loading…");
        try {
          const r = await api(`/api/admin/rekordbox/load?name=${encodeURIComponent(p.file)}`, { method: "POST" });
          rbStep = { s: "done", n: r.saved, dis: r.disagree };
          try { rbStatus = await api("/api/admin/rekordbox/status"); } catch (_) {}
          loadReview(); loadEvents();
        } catch (e) { rbStep = { s: "error", msg: e.message }; }
        renderRb();
      };
      const before = p.status && p.status.tracks;
      kids.push(h("div", { class: "rb-pre" }, h("h3", {}, `Ready to load ${p.file}`),
        h("ul", { class: "rb-sum" },
          h("li", {}, h("b", {}, p.rekordbox_tracks.toLocaleString()), " tracks in the export"),
          h("li", {}, h("b", {}, String(m.length)), ` are in your Tastr library (${byFile} by file name, ${m.length - byFile} by artist and title). The other ${(p.rekordbox_tracks - m.length).toLocaleString()} aren't, so they're skipped.`),
          h("li", {}, `Rekordbox agrees with Tastr on BPM for ${okB} (${pct(okB, m.length)}%) and on key for ${okK} (${pct(okK, m.length)}%).`),
          h("li", {}, h("b", {}, String(p.disagree)), " disagree. They'll show in your Review list with Rekordbox's values.")),
        list,
        h("p", { class: "muted" }, before ? `This replaces the Rekordbox values from your last load (${before} tracks). Tracks that aren't in this export lose theirs.` : "Nothing has been loaded before, so nothing is replaced."),
        h("div", { class: "menu-confirm-btns" }, h("button", { type: "button", onclick: idle }, "Cancel"), load)));
    }
  } else if (st.s === "done") {
    kids.push(h("div", { class: "rb-pre rb-done", role: "status" }, h("h3", {}, h("span", { class: "rb-tick", "aria-hidden": "true" }, "✓"), `Loaded Rekordbox values onto ${st.n} tracks`),
      h("p", {}, "Their cards show Rekordbox under Listed elsewhere, in the line under the stats and in Metadata.", st.dis ? ` ${st.dis} that disagree are in your Review list.` : ""),
      h("div", { class: "menu-confirm-btns" },
        st.dis ? h("button", { type: "button", onclick: () => { switchTo("library"); if (!reviewOn) $("lib-review").click(); } }, "Open Review") : null,
        h("button", { type: "button", class: "primary", onclick: idle }, "Done"))));
  }
  box.replaceChildren(...kids);
}
