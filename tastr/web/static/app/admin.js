// Admin (hosted mode, admins only).

import { $, api, h, svg } from "./core.js";
import { fmtUpLong } from "./library.js";
import { stat } from "./card.js";
import { serverCfg } from "./daw.js";

// GET /api/admin/usage?range= and /api/admin/events, fetched each time the tab opens (no polling). Days are UTC days, like the limits.
// Cloud cost is an estimate: Modal bills about $0.001 per analysis and $0.008 per stem job (T4, ~50 s), against the
// friends environment's monthly budget, which is set in Modal and not known to the server.
const ADM_TRACK_COST = 0.001, ADM_STEM_COST = 0.008, ADM_BUDGET = 10;
// The activity log's filters: event types and where they come from (db.EVENT_TYPES and db.EVENT_SOURCES on the server).
const ACT_TYPES = { upload: "Uploads", analysis: "Analysis", stems: "Stem jobs", limit: "Limits hit", error: "Errors", signin: "Sign-ins",
  blocked: "Blocked requests", deploy: "Deploys and CI", backup: "Backups", admin: "Admin changes" };
const ACT_SRC = { app: "Tastr", modal: "Modal", flyio: "Fly.io", github: "GitHub", cloudflare: "Cloudflare", r2: "R2 (Litestream)" };
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
  // One row per person, with 30 days of [tracks, stem jobs] (index 29 is today) from the usage history.
  const all = A.users.map((u) => ({ ...u, admin: A.admins.includes(u.email), days: Array.from({ length: 30 }, () => [0, 0]),
    active: false, lastDay: null, bytes: u.audio_bytes + u.stems_bytes }));
  const P = all.filter((p) => p.email), byEmail = Object.fromEntries(P.map((p) => [p.email, p]));
  for (const x of A.history) {
    const p = byEmail[x.user], i = 29 - Math.round((today - Date.parse(x.day + "T00:00:00Z")) / dayMs);
    if (!p || i < 0 || i > 29) continue;
    if (x.kind === "tracks") p.days[i][0] += x.n;
    if (x.kind === "stems") p.days[i][1] += x.n;
    if (i >= 23) p.active = true;
    p.lastDay = Math.max(p.lastDay ?? 0, i);
  }
  for (const p of all) {
    p.tracks30 = p.days.reduce((a, d) => a + d[0], 0); p.stems30 = p.days.reduce((a, d) => a + d[1], 0);
    p.cost = p.tracks30 * ADM_TRACK_COST + p.stems30 * ADM_STEM_COST;
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
    tile(`$${cost.toFixed(2)}`, "cloud cost, 30 days (est.)", admMeter(cost / ADM_BUDGET, cost / ADM_BUDGET >= 0.9)));
  renderAdmChart(A, P);
  renderPeople(A, all, P);
  const kv = [["Upload size", `${L.max_upload_mb} MB`, "TASTR_MAX_UPLOAD_MB"], ["Track length", `${L.max_minutes} min`, "TASTR_MAX_MINUTES"], ["Files per upload", L.max_files, "TASTR_MAX_FILES"],
    ["Tracks per person per day", L.daily_tracks, "TASTR_DAILY_TRACKS"], ["Stem jobs per person per day", L.daily_stems, "TASTR_DAILY_STEMS"],
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
    if (admKind === "cost") return s.tracks[i] * ADM_TRACK_COST + s.stems[i] * ADM_STEM_COST;
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
    : admKind === "cost" ? `Estimated from Modal run time: ~$${ADM_TRACK_COST} per analysis, ~$${ADM_STEM_COST} per stem job. Not from Modal's billing.`
    : admKind === "visits" ? "A visit is a person's first request after 30 minutes without one." : null;
  $("adm-legend").replaceChildren(...P.map((p, j) => h("span", {}, h("i", { style: `background:${admColor(j)}` }), p.email)),
    ...(note ? [h("span", { class: "muted adm-note" }, note)] : []));
}
// People, with today's quotas against each person's own caps (an admin's change, or the server's), and the ✎ to change them.
function renderPeople(A, all, P) {
  const L = A.limits;
  const quota = (p, k, n) => {
    const capN = p.caps[k], o = p.override, def = k === "tracks" ? L.daily_tracks : L.daily_stems, raised = o && o[`daily_${k}`] != null;
    const tip = raised ? `Changed from the default of ${def}${o.until_day ? ", today only (until 00:00 UTC)" : ", until an admin changes it"}` : "";
    return h("div", { class: "adm-quota" }, h("span", { title: tip }, `${n} / ${capN}`, raised ? h("span", { class: "adm-raised" }, o.until_day ? " ↑ today" : " ↑") : null),
      admMeter(capN ? n / capN : 1, n >= capN),
      n >= capN && !raised ? h("button", { type: "button", class: "linklike adm-raise", onclick: (e) => { e.stopPropagation(); admEdit = p.email; renderAdmin(); } }, "Raise") : null);
  };
  const seen = (p) => (p.last_seen ? admAgo(p.last_seen).replace(/^(\d.*)$/, "$1 ago") : p.lastDay != null ? admDaysAgo(29 - p.lastDay) : "–");
  $("adm-people").replaceChildren(
    h("thead", {}, h("tr", {}, ...["Person", "Last seen", "Visits today", "Tracks today", "Stems today", "Tracks, 30 d", "Library", "Storage", "Cost, 30 d", ""]
      .map((t, i) => h("th", { class: i === 2 || (i > 4 && i < 9) ? "num" : "" }, t, i === 9 ? h("span", { class: "sr-only" }, "Limits") : null)))),
    h("tbody", {}, ...all.flatMap((p) => {
      if (!p.email) return [h("tr", { class: "adm-none" }, h("td", {}, "No owner", h("span", { class: "muted" }, " · added before sharing")), h("td", { colspan: "5" }),
        h("td", { class: "num" }, String(p.tracks)), h("td", { class: "num", title: "audio + stems" }, admGB(p.bytes)), h("td"), h("td"))];
      const j = P.indexOf(p), pick = () => { admFocus = admFocus === j ? null : j; renderAdmin(); };
      const tr = h("tr", { class: admFocus === j ? "sel" : "", tabindex: "0", title: "Pick out in the chart", onclick: pick,
        onkeydown: (e) => { if (e.target === tr && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); pick(); } } },
        h("td", {}, h("span", { class: "adm-who", style: `background:${admColor(j)}` }), p.email, p.admin ? h("span", { class: "muted" }, " · admin") : null),
        h("td", {}, seen(p)), h("td", { class: "num" }, String(p.today.visits)),
        h("td", {}, quota(p, "tracks", p.today.tracks)), h("td", {}, quota(p, "stems", p.today.stems)),
        h("td", { class: "num" }, String(p.tracks30)), h("td", { class: "num" }, String(p.tracks)),
        h("td", { class: "num", title: "audio + stems" }, admGB(p.bytes)), h("td", { class: "num" }, `$${p.cost.toFixed(2)}`),
        h("td", {}, h("button", { type: "button", class: "adm-lim-btn", "aria-expanded": String(admEdit === p.email), title: `Change ${p.email}'s daily limits`,
          "aria-label": `Change ${p.email}'s daily limits`, onclick: (e) => { e.stopPropagation(); admEdit = admEdit === p.email ? null : p.email; renderAdmin(); } }, "✎")));
      return admEdit === p.email ? [tr, limitRow(p, L)] : [tr];
    })));
}
// The inline row under a person: their daily caps (empty = the server default), today only or until changed.
function limitRow(p, L) {
  const o = p.override || {};
  const tIn = h("input", { type: "number", min: "0", max: "10000", value: o.daily_tracks ?? "", placeholder: String(L.daily_tracks), "aria-label": "Tracks per day" });
  const sIn = h("input", { type: "number", min: "0", max: "1000", value: o.daily_stems ?? "", placeholder: String(L.daily_stems), "aria-label": "Stem jobs per day" });
  const until = h("select", { "aria-label": "How long" }, h("option", { value: "today" }, "Today only (resets at 00:00 UTC)"), h("option", { value: "standing" }, "Until I change it"));
  until.value = p.override && !o.until_day ? "standing" : "today";
  const err = h("span", { class: "error", role: "alert" });
  const url = `/api/admin/limits/${encodeURIComponent(p.email)}`;
  const send = async (opts) => {
    try { await api(url, opts); admEdit = null; await loadAdmin(); }
    catch (e) { err.textContent = e.message; }
  };
  const save = () => {
    const t = tIn.value === "" ? null : +tIn.value, st = sIn.value === "" ? null : +sIn.value;
    if ([t, st].some((v) => v != null && (!Number.isInteger(v) || v < 0))) { err.textContent = "Limits are whole numbers, 0 or more."; return; }
    send({ method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ daily_tracks: t, daily_stems: st, scope: until.value }) });
  };
  setTimeout(() => tIn.focus(), 0);
  return h("tr", { class: "adm-lim-row" }, h("td", { colspan: "10" },
    h("div", { class: "adm-lim", onkeydown: (e) => { if (e.key === "Enter" && e.target.tagName === "INPUT") save(); if (e.key === "Escape") { admEdit = null; renderAdmin(); } } },
      h("b", {}, `Daily limits for ${p.email}`),
      h("label", {}, "Tracks per day", tIn), h("label", {}, "Stem jobs per day", sIn), h("label", {}, "For", until),
      h("button", { type: "button", class: "primary", onclick: save }, "Save"),
      p.override ? h("button", { type: "button", class: "linklike", onclick: () => send({ method: "DELETE" }) }, "Back to defaults") : null,
      h("button", { type: "button", class: "linklike", onclick: () => { admEdit = null; renderAdmin(); } }, "Cancel"),
      h("span", { class: "muted" }, `Empty means the server default (${L.daily_tracks} tracks, ${L.daily_stems} stem jobs, from TASTR_DAILY_*). Only this person changes.`), err)));
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
  const linkEls = () => [links.fly && h("a", { href: links.fly, target: "_blank", rel: "noopener" }, "Fly logs"),
    links.modal && h("a", { href: links.modal, target: "_blank", rel: "noopener" }, "Modal")].filter(Boolean);
  // A deploy links to its GitHub Actions run; a problem links to the Fly and Modal logs.
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
