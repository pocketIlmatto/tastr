// Selecting several tracks and running bulk actions on them.

import { $, api, currentId, fmtLeft, h, jobTitle, progBar, setCurrentId, tracks } from "./core.js";
import { libActive, loadTracks, refresh, renderList, visibleTracks } from "./library.js";
import { setInReview } from "./labels.js";
import { renderCard } from "./card.js";
import { serverCfg } from "./daw.js";
import { dropEdits, flushEdits } from "./groove.js";
import { runDrumModel, setStems, stemsBusy, stemsEta, stemsLabel } from "./menu.js";

// Selection is by track id, so it survives filtering and sorting.
export const sel = new Set();
// Per-row job state while a bulk action runs: { text, active } or { text, error } once a track failed.
export const jobs = new Map();
export let selecting = false, lastSel = null, bulkMsg = "", bulkBusy = false, bulkTimer;
export function setSelecting(on) {
  selecting = on;
  if (!on) { sel.clear(); lastSel = null; }
  $("lib-sel").setAttribute("aria-pressed", String(on)); $("lib-sel").textContent = on ? "Done" : "Select";
  renderList();
}
// Shift picks or unpicks the range from the last toggled row, in the current visible order.
export function toggleSel(id, range) {
  const ids = visibleTracks().map((t) => t.id);
  if (range && lastSel != null && ids.includes(lastSel)) {
    const [a, b] = [ids.indexOf(lastSel), ids.indexOf(id)].sort((x, y) => x - y);
    const on = !sel.has(id);
    ids.slice(a, b + 1).forEach((x) => (on ? sel.add(x) : sel.delete(x)));
  } else if (sel.has(id)) sel.delete(id); else sel.add(id);
  lastSel = id;
  renderList();
  const li = $("track-list").querySelector(`li.trk[data-id="${id}"]`);
  if (li) li.focus();
}
function sayBulk(msg, fadeMs) {
  clearTimeout(bulkTimer);
  bulkMsg = msg;
  if (fadeMs) bulkTimer = setTimeout(() => { if (!bulkBusy) { bulkMsg = ""; renderList(); } }, fadeMs);
}
export function renderBulk(shown) {
  const box = $("lib-bulk");
  box.hidden = !selecting && !bulkMsg && !jobs.size;
  if (box.hidden) return;
  const kids = [];
  if (selecting) {
    const allShown = shown.length && shown.every((t) => sel.has(t.id));
    kids.push(h("div", { class: "bulk-row" },
      h("b", {}, `${sel.size} selected`),
      h("button", { type: "button", class: "linklike", onclick: () => { shown.forEach((t) => (allShown ? sel.delete(t.id) : sel.add(t.id))); renderList(); } },
        allShown ? "Select none" : `Select all ${shown.length}${libActive() ? " shown" : ""}`),
      sel.size ? h("button", { type: "button", class: "linklike", onclick: () => { sel.clear(); renderList(); } }, "Clear") : null),
      bulkMenu());
  }
  if (bulkMsg || jobs.size) kids.push(h("div", { class: "bulk-say muted", role: "status", "aria-live": "polite" }, bulkBusy ? h("span", { class: "spin", "aria-hidden": "true" }) : null, h("span", { class: "bulk-msg" }, bulkMsg),
    // Failed rows keep their note until the next run or until dismissed here.
    !bulkBusy && jobs.size ? h("button", { type: "button", class: "linklike", onclick: () => { jobs.clear(); sayBulk(""); renderList(); } }, "Dismiss") : null));
  box.replaceChildren(...kids);
}
const plural = (n) => `${n} track${n === 1 ? "" : "s"}`;
// Each action calls the per-track endpoint; `run` resolves to the track's fresh card (or null).
const editsGone = new Set(); // tracks whose re-analysis dropped some saved edits
export let lookupProviders = []; // enabled catalogs (TASTR_LOOKUP), from the last card the server sent
const A_TYPICAL_BULK = 40; // a re-analysis, like A_TYPICAL (the upload aside)
export let drumModelOn = false; // whether the server can run the drum model, from the last card it sent
const BULK = {
  // typical: seconds per track to start the time left from, until the batch's own average is known;
  // one: run one track at a time even when the server allows more (see runBulk)
  reload: { label: "Reload metadata", doing: "Reloading metadata…", fail: "Couldn't reload metadata", typical: 2,
    done: (n) => `Reloaded metadata for ${plural(n)}.`,
    run: async (id) => (await api(`/api/tracks/${id}/retag`, { method: "POST" })).card },
  reanalyze: { label: "Re-analyze", doing: "Re-analyzing…", fail: "Couldn't re-analyze", typical: A_TYPICAL_BULK,
    done: (n) => { const d = editsGone.size; editsGone.clear(); return `Re-analyzed ${plural(n)}.` + (d ? ` Some edits no longer match the new sections in ${plural(d)}, so they were dropped.` : ""); },
    run: async (id) => {
      await flushEdits(id);
      const r = await api(`/api/tracks/${id}/reanalyze`, { method: "POST" });
      dropEdits(id);
      if (r.edits_dropped) editsGone.add(id);
      return r.card;
    } },
  lookup: { label: "Look up BPM & key online", doing: "Looking up…", fail: "Couldn't look up", typical: 3,
    when: () => lookupProviders.length > 0,
    done: (n) => `Looked up ${plural(n)}.`,
    run: async (id) => (await api(`/api/tracks/${id}/lookup`, { method: "POST" })).card },
  drums: { label: "Detect drums with the model", note: "slow", one: true, doing: "Detecting drums…", fail: "Couldn't detect drums with the model", typical: 90,
    when: () => drumModelOn,
    done: (n) => { const d = editsGone.size; editsGone.clear(); return `Detected drums with the model for ${plural(n)}.` + (d ? ` Some edits no longer match the new sections in ${plural(d)}, so they were dropped.` : ""); },
    run: async (id, say) => {
      await flushEdits(id);
      const card = await runDrumModel(id, say);
      dropEdits(id);
      return card;
    } },
  stems: { label: "Separate stems", note: "slow", one: true, doing: "Separating stems…", fail: "Couldn't separate stems", typical: 180,
    done: (n) => `Stems ready for ${plural(n)}.`,
    // The server separates in the background; wait for this track before starting the next one.
    run: async (id, say) => {
      let st = await api(`/api/tracks/${id}/stems`, { method: "POST" });
      setStems(id, st);
      while (stemsBusy(st)) {
        say(stemsLabel(st), st);
        await new Promise((r) => setTimeout(r, 2500));
        st = await api(`/api/tracks/${id}/stems`);
        setStems(id, st);
      }
      if (st.state !== "ready") throw new Error(st.error || "Stem separation failed");
      return null;
    } },
};
function bulkMenu() {
  const n = sel.size;
  const btn = h("button", { type: "button", class: "bulk-btn", "aria-haspopup": "menu", "aria-expanded": "false", disabled: n && !bulkBusy ? null : "" }, `Actions${n ? ` for ${n}` : ""} ▾`);
  const pop = h("div", { class: "menu-pop bulk-pop", role: "menu", hidden: "" });
  const wrap = h("div", { class: "menu-wrap bulk-wrap" }, btn, pop);
  const close = (refocus) => { pop.hidden = true; btn.setAttribute("aria-expanded", "false"); document.removeEventListener("pointerdown", outside, true); if (refocus) btn.focus(); };
  const outside = (e) => { if (!wrap.contains(e.target)) close(false); };
  const item = (label, onclick, o = {}) => h("button", { type: "button", role: "menuitem", class: "menu-item" + (o.danger ? " danger" : ""), onclick }, label, o.note ? h("span", { class: "menu-note" }, o.note) : null);
  const items = () => [...pop.querySelectorAll("[role=menuitem], .menu-confirm button")];
  const showMain = () => pop.replaceChildren(
    ...Object.entries(BULK).filter(([, v]) => !v.when || v.when()).map(([k, v]) => item(v.label, () => { close(false); runBulk(k, [...sel]); }, { note: v.note })),
    item("Add to review", () => { close(false); const ids = [...sel]; setInReview(ids, true).then(() => { sayBulk(`Added ${ids.length} to review.`, 4000); renderList(); }); }),
    item("Remove from review", () => { close(false); const ids = [...sel]; setInReview(ids, false).then(() => { sayBulk(`Took ${ids.length} out of review.`, 4000); renderList(); }); }),
    h("hr", { class: "menu-sep" }),
    item(`Delete ${plural(n)}…`, showConfirm, { danger: true }));
  const showConfirm = () => {
    pop.replaceChildren(h("div", { class: "menu-confirm", role: "group", "aria-label": "Confirm delete" },
      h("p", {}, h("b", {}, `Delete ${plural(n)}?`), h("br"), h("span", { class: "muted" }, "This removes them and their analysis from your library.")),
      h("div", { class: "menu-confirm-btns" },
        h("button", { type: "button", onclick: () => { showMain(); items()[0].focus(); } }, "Cancel"),
        h("button", { type: "button", class: "danger-solid", onclick: () => { close(false); deleteBulk([...sel]); } }, "Delete"))));
    pop.querySelector("button").focus();
  };
  btn.onclick = () => {
    if (!pop.hidden) { close(true); return; }
    showMain(); pop.hidden = false; btn.setAttribute("aria-expanded", "true"); items()[0].focus();
    document.addEventListener("pointerdown", outside, true);
  };
  pop.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(true); return; }
    const list = items(), i = list.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); list[(i + 1) % list.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); list[(i - 1 + list.length) % list.length].focus(); }
    else if (e.key === "Tab") close(false);
  });
  return wrap;
}
// Light jobs (reload, re-analyze, look up) run several tracks at once: /api/config's bulk_at_once, which is
// TASTR_MAX_ANALYSES on a hosted server (its analysis slots queue any extra) and about half the cores at home.
// Stems and the drum model (`one`: the GPU and 4 GB jobs) go one at a time. Each lane takes the next track from a
// shared queue until it's empty. The user can leave select mode and keep browsing meanwhile.
async function runBulk(kind, ids) {
  const b = BULK[kind];
  if (!ids.length || bulkBusy) return;
  bulkBusy = true;
  jobs.clear();
  const doing = b.doing.replace("…", "");
  const lanes = Math.max(1, Math.min(b.one ? 1 : serverCfg.bulk_at_once || 1, ids.length));
  const queue = [...ids], running = new Map();  // id -> { t0, eta }; eta is {frac, left} when the job reports its own progress (stems)
  let failed = 0, finished = 0, per = b.typical, timed = 0, painting = false;
  const runFrac = (r) => (r.eta ? r.eta.frac : Math.min(0.95, (Date.now() - r.t0) / 1000 / per));
  const runLeft = (r) => (r.eta ? r.eta.left : Math.max(0, per - (Date.now() - r.t0) / 1000));
  // The bulk line and the tab title: tracks done, and the time left for the whole batch across all lanes
  const sayLeft = () => {
    const run = [...running.values()];
    const left = (queue.length * per + run.reduce((a, r) => a + runLeft(r), 0)) / lanes;
    sayBulk(`${doing}${lanes > 1 ? ` ${lanes} at a time` : ""} · ${finished} of ${ids.length} done · ${fmtLeft(left)}${ids.length > 1 ? " in all" : ""}`);
    jobTitle("bulk", (finished + run.reduce((a, r) => a + runFrac(r), 0)) / ids.length);
  };
  const sayQueued = () => queue.forEach((x, k) => jobs.set(x, { text: k ? `Queued · ${k} ahead of it` : "Up next", active: false }));
  // Several lanes report at once, so the list is rebuilt at most once per tick
  const paint = () => { if (!painting) { painting = true; setTimeout(() => { painting = false; renderList(); }, 0); } };
  const lane = async () => {
    while (queue.length) {
      const id = queue.shift();
      if (!tracks.some((t) => t.id === id)) { jobs.delete(id); finished++; continue; }
      const r = { t0: Date.now(), eta: null };
      running.set(id, r);
      jobs.set(id, { text: b.doing, active: true });
      sayQueued(); sayLeft(); paint();
      try {
        const card = await b.run(id, (text, st) => {
          r.eta = st && st.state === "running" ? stemsEta(st) : null;
          jobs.set(id, { text, active: st ? st.state === "running" : true, frac: r.eta ? r.eta.frac : null });
          sayLeft();
          // Update the row in place so a periodic progress update doesn't steal focus from the list.
          const li = $("track-list").querySelector(`li.trk[data-id="${id}"]`), el = li && li.querySelector(".s.job"), bar = li && li.querySelector(".pbar");
          if (el && (bar || !r.eta)) {
            el.replaceChildren(h("span", { class: "spin" + (jobs.get(id).active ? "" : " idle"), "aria-hidden": "true" }), text);
            if (bar) bar.replaceWith(progBar(r.eta.frac, `${text}`));
          } else paint();
        });
        jobs.delete(id);
        per = (per * (timed + 1) + (Date.now() - r.t0) / 1000) / (timed + 2); timed++;  // running average, seeded with the typical time
        if (card && currentId === id) renderCard(card);
      } catch (e) {
        failed++;  // only this row; the other lanes keep going
        jobs.set(id, { text: b.fail, error: e.message });
      }
      running.delete(id); finished++;
      sayLeft(); paint();
    }
  };
  // Each second, only the bulk line's text changes (and only when it reads differently), so nothing else repaints
  const tick = setInterval(() => { sayLeft(); const m = $("lib-bulk").querySelector(".bulk-msg"); if (m && m.textContent !== bulkMsg) m.textContent = bulkMsg; }, 1000);
  try { await Promise.all(Array.from({ length: lanes }, lane)); } finally { clearInterval(tick); }
  bulkBusy = false;
  jobTitle("bulk", null);
  try { await loadTracks(); } catch (_) {}
  const ok = ids.length - failed;
  sayBulk(b.done(ok) + (failed ? ` ${plural(failed)} failed.` : ""), failed ? 0 : 5000);
  renderList();
}
async function deleteBulk(ids) {
  if (!ids.length || bulkBusy) return;
  bulkBusy = true;
  sayBulk(`Deleting ${plural(ids.length)}…`);
  renderList();
  const gone = [];
  for (const id of ids) {
    try { await api(`/api/tracks/${id}`, { method: "DELETE" }); gone.push(id); dropEdits(id); } catch (_) {}
  }
  bulkBusy = false;
  gone.forEach((id) => { sel.delete(id); jobs.delete(id); });
  if (gone.includes(currentId)) { setCurrentId(null); $("card").replaceChildren(h("p", { class: "muted" }, "Select a track.")); }
  const failed = ids.length - gone.length;
  sayBulk(`Deleted ${plural(gone.length)}.` + (failed ? ` Couldn't delete ${plural(failed)}.` : ""), failed ? 0 : 4000);
  await refresh();
}

export function setLookupProviders(v) { return (lookupProviders = v); }
export function setDrumModelOn(v) { return (drumModelOn = v); }

// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  $("lib-sel").addEventListener("click", () => setSelecting(!selecting));
}
