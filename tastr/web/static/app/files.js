// The Files view: the audio you uploaded and the stems made from it, their sizes, downloads (one file or a zip),
// and deleting stems to free space. GET /api/files has the sizes; the rest of each track comes from the library.

import { $, api, cap, fmtLeft, h, httpError, mmss, progBar, tracks } from "./core.js";
import { LIB_CLEAR, SORTS, filtersOn, fmtUp, fmtUpLong, keyRank, matchTracks } from "./library.js";
import { openTrack, switchTo } from "./card.js";
import { onStems, pollStems, setStems, startStems, stemStatus, stemsBusy, stemsEta, stemsLabel } from "./menu.js";

const STEMS = ["vocals", "bass", "other", "drums"];
export const fmtBytes = (b) => (b >= 1024 ** 3 ? `${(b / 1024 ** 3).toFixed(b >= 9.995 * 1024 ** 3 ? 0 : 2)} GB` : b >= 1024 ** 2 ? `${(b / 1024 ** 2).toFixed(1)} MB` : `${Math.max(1, Math.round((b || 0) / 1024))} KB`);
const sum = (xs, g) => xs.reduce((a, x) => a + g(x), 0);

const flState = { sort: "new", show: "", ...LIB_CLEAR() }, flSel = new Set();
let flSelecting = false, data = null, loading = null;
const watched = new Set(); // track ids whose stem job the view follows

const stemBytes = (f) => (f.stems && f.stems.bytes ? sum(Object.values(f.stems.bytes), (v) => v) : 0);
const audioBytes = (f) => (f.audio ? f.audio.bytes : 0);
// The stems state: the poller's while a job runs (it updates every few seconds), else what /api/files said.
function stemState(f) {
  const live = stemStatus.get(f.id);
  return live && stemsBusy(live) ? live : f.stems || { state: "none" };
}

export async function loadFiles() {
  if (!loading) loading = api("/api/files").finally(() => { loading = null; });
  try { data = await loading; } catch (e) { $("fl-list").replaceChildren(h("li", { class: "error" }, e.message)); return; }
  for (const f of data.tracks) {
    const st = f.stems;
    if (st && stemsBusy(st)) { setStems(f.id, st); pollStems(); }
    follow(f.id);
  }
  fillFilters(); renderFiles();
}
// Repaint a track's row as its stem job moves on, and reload the sizes once it ends.
function follow(id) {
  if (watched.has(id)) return;
  watched.add(id);
  onStems($("fl-list"), id, (st, was) => {
    if ($("view-files").hidden || !data) return;
    if (was && stemsBusy(was) && !stemsBusy(st)) { if (st.state === "ready") say(`✓ Stems ready for ${titleOf(id)}.`, false); loadFiles(); return; }
    const li = $("fl-list").querySelector(`li[data-id="${id}"]`), f = data.tracks.find((x) => x.id === id);
    if (li && f) li.replaceWith(rowEl(f));
  });
}
const titleOf = (id) => (data && data.tracks.find((x) => x.id === id) || {}).title || `track ${id}`;

// The status line under the tools: a spinner while something runs. A new download replaces the line.
function say(text, spin = true, error = false) {
  const el = $("fl-say");
  el.classList.toggle("error", error);
  el.replaceChildren(...[spin ? h("span", { class: "spin", "aria-hidden": "true" }) : null, text].filter(Boolean));
}

// Downloads go through fetch, so the line can show progress and say when the file is saved. Zips are streamed by the
// server as it writes them (X-Zip-Bytes is the files' total), so "zipping" and receiving are the same bytes.
let dlRun = 0;
const nameFrom = (cd) => { const m = /filename\*=utf-8''([^;]+)/i.exec(cd || ""); try { return m ? decodeURIComponent(m[1]) : null; } catch (_) { return null; } };
// `out`: where the progress goes instead of the Files view's line, as out(text, spin, error) (Library › List › Export).
export async function download(url, name, bytes, opts, out) {
  const run = out ? 0 : ++dlRun, show = (...a) => { if (out) out(...a); else if (run === dlRun) say(...a); };
  const zip = /\.zip$/.test(name) || /\.zip(\?|$)/.test(url) || (opts && opts.method === "POST");
  show(zip ? `Zipping ${name}…` : `Downloading ${name}${bytes ? ` (${fmtBytes(bytes)})` : ""}…`);
  try {
    const r = await fetch(url, opts);
    if (!r.ok) { let d = null; try { d = (await r.json()).detail; } catch (_) {} throw new Error(httpError(r.status, d, r.statusText)); }
    name = nameFrom(r.headers.get("Content-Disposition")) || name;
    const total = +r.headers.get("X-Zip-Bytes") || +r.headers.get("Content-Length") || bytes || 0;
    const reader = r.body.getReader(), parts = [], t0 = performance.now();
    let got = 0, last = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parts.push(value); got += value.length;
      const now = performance.now();
      if (now - last > 250 && total) {
        last = now;
        const frac = Math.min(1, got / total), secs = (now - t0) / 1000, left = frac > 0.02 ? (secs * (1 - frac)) / frac : null;
        show(`${zip ? "Zipping" : "Downloading"} ${name} · ${Math.round(frac * 100)}%${left != null ? ` · ${fmtLeft(left)}` : ""}`);
      }
    }
    show(`Downloading ${name} (${fmtBytes(got)})…`);
    const href = URL.createObjectURL(new Blob(parts, { type: r.headers.get("Content-Type") || "application/octet-stream" }));
    const a = h("a", { href, download: name, hidden: "" });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 60_000);
    show(`✓ ${name} downloaded`, false);
  } catch (e) {
    show(`Couldn't download ${name}: ${e.message}`, false, true);
  }
}
const getAudio = (f) => download(`/api/tracks/${f.id}/audio?download=1`, f.audio.name, f.audio.bytes);
const getStem = (f, n) => download(`/api/tracks/${f.id}/stems/${n}?download=1`, `${f.base} (${n}).flac`, f.stems.bytes[n]);
const getStemsZip = (f) => download(`/api/tracks/${f.id}/stems.zip`, `${f.base} stems.zip`, stemBytes(f));
const postZip = (ids, kind, bytes) => download("/api/files/zip", `Tastr ${kind}, ${ids.length} track${ids.length === 1 ? "" : "s"}.zip`, bytes,
  { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids, kind }) });

async function deleteStems(f) {
  const r = await api(`/api/tracks/${f.id}/stems`, { method: "DELETE" });
  setStems(f.id, { state: "none" });
  return r.freed_bytes || 0;
}
// Start separations one after another; the server runs them one at a time. Stops at the first refusal (the daily limit).
async function separate(list) {
  let n = 0;
  for (const f of list) {
    try { setStems(f.id, await api(`/api/tracks/${f.id}/stems`, { method: "POST" })); n++; } catch (e) {
      say(list.length > 1 ? `Queued ${n} of ${list.length}. ${e.message}` : e.message, false, true);
      break;
    }
  }
  pollStems();
  if (n === list.length) say(n > 1 ? `Separating stems for ${n} tracks, one at a time. They count toward today's stems limit.` : `Separating stems for ${list[0].title || "the track"}…`, n > 0);
  renderFiles();
}

// Two clicks within 4 s: the first says how much space goes, the second deletes.
function confirmButton(attrs, label, sureLabel, run) {
  const b = h("button", { type: "button", ...attrs }, label);
  b.addEventListener("click", async () => {
    if (!b.dataset.sure) {
      b.dataset.sure = "1"; b.textContent = sureLabel;
      setTimeout(() => { if (b.isConnected && b.dataset.sure) { delete b.dataset.sure; b.textContent = label; } }, 4000);
      return;
    }
    b.disabled = true;
    await run();
  });
  return b;
}

function renderStore() {
  const u = data.used, audioB = u.audio_bytes, stemB = u.stem_bytes, used = audioB + stemB;
  const n = data.tracks.length, nStems = data.tracks.filter((f) => stemBytes(f)).length;
  const counts = `${n} track${n === 1 ? "" : "s"} · stems for ${nStems}`;
  if (data.cap_bytes == null) { // your own machine: no cap, so no bar
    $("fl-store").replaceChildren(h("div", { class: "fl-store-top" }, h("b", {}, fmtBytes(used)), h("span", { class: "muted" }, `used · ${counts}`)),
      h("div", { class: "fl-key" }, h("span", { class: "a" }, `Uploaded audio ${fmtBytes(audioB)}`), h("span", { class: "s" }, `Stems ${fmtBytes(stemB)}`)));
    return;
  }
  // A shared server's cap is everyone's, so the bar is your files against the space the server has left.
  const free = data.free_bytes || 0, whole = Math.max(1, used + free);
  $("fl-store").replaceChildren(
    h("div", { class: "fl-store-top" }, h("b", {}, fmtBytes(used)), h("span", { class: "muted" }, `Your files ${fmtBytes(used)} · Tastr has ${fmtBytes(free)} free · ${counts}`)),
    h("div", { class: "fl-bar", role: "img", "aria-label": `Your audio ${fmtBytes(audioB)}, your stems ${fmtBytes(stemB)}, free on the server ${fmtBytes(free)}` },
      h("i", { class: "a", style: `width:${(audioB / whole) * 100}%` }), h("i", { class: "s", style: `width:${(stemB / whole) * 100}%` })),
    h("div", { class: "fl-key" }, h("span", { class: "a" }, `Your audio ${fmtBytes(audioB)}`), h("span", { class: "s" }, `Your stems ${fmtBytes(stemB)}`), h("span", { class: "f" }, `Free on the server ${fmtBytes(free)}`)));
}

// The list joins /api/files with the library's tracks, so search, sort and filters work as in the sidebar.
function joined() {
  const byId = new Map(tracks.map((t) => [t.id, t]));
  return data.tracks.map((f) => ({ ...(byId.get(f.id) || { id: f.id, title: f.title, artist: f.artist, duration: f.duration, tags: [], uploaded: Date.parse(f.created_at) || 0 }), f }));
}
const FSORT = { ...SORTS, big: (a, b) => audioBytes(b.f) + stemBytes(b.f) - audioBytes(a.f) - stemBytes(a.f) };

export function renderFiles() {
  if (!data) return;
  renderStore();
  const all = joined();
  const rows = matchTracks(flState, all).filter((t) => { const s = stemBytes(t.f) > 0; return (flState.show !== "stems" || s) && (flState.show !== "nostems" || !s); })
    .sort(FSORT[flState.sort] || SORTS.new);
  const active = filtersOn(flState) || !!flState.show;
  $("fl-n").textContent = active ? `${rows.length} of ${all.length} tracks` : `${all.length} track${all.length === 1 ? "" : "s"}`;
  $("fl-clear").hidden = !active;
  renderBulk(rows);
  const ul = $("fl-list");
  if (!rows.length) {
    ul.replaceChildren(h("li", { class: "empty" }, !all.length ? "No files yet. Upload audio in the Library."
      : flState.show === "stems" && !filtersOn(flState) ? "No stems yet. Make them from a track's ⋯ menu, from \"Try this in › Strudel\", or with \"Separate stems\" in Select mode." : "No tracks match."));
    return;
  }
  ul.replaceChildren(...rows.map((t) => rowEl(t.f, t)));
}

function rowEl(f, t) {
  t = t || joined().find((x) => x.id === f.id) || { uploaded: 0 };
  const title = f.title || `Track ${f.id}`, picked = flSel.has(f.id), st = stemState(f);
  const pick = flSelecting ? h("label", { class: "fl-pick" },
    h("input", { type: "checkbox", class: "sr-only", "aria-label": `Select ${title}`, ...(picked ? { checked: "" } : {}),
      onchange: (e) => { if (e.target.checked) flSel.add(f.id); else flSel.delete(f.id); renderFiles(); } }),
    h("span", { class: "pick" + (picked ? " on" : ""), "aria-hidden": "true" })) : null;
  const total = audioBytes(f) + stemBytes(f);
  return h("li", { "data-id": f.id, class: picked ? "picked" : "" },
    h("div", { class: "fl-t" }, pick,
      h("button", { type: "button", class: "linklike", title: "Open this track in the Library", onclick: () => { switchTo("library"); openTrack(f.id); } }, title),
      h("span", { class: "muted" }, [f.artist, t.uploaded ? `uploaded ${fmtUp(t.uploaded)}` : null].filter(Boolean).join(" · ")),
      h("span", { class: "fl-tot", title: t.uploaded ? `Uploaded ${fmtUpLong(t.uploaded)}` : null }, fmtBytes(total))),
    h("div", { class: "fl-row" }, h("span", { class: "fl-lab" }, "Audio"),
      f.audio ? h("span", { class: "fl-file" }, `${f.audio.format} · ${mmss(f.duration)}`, h("span", { class: "sz" }, fmtBytes(f.audio.bytes)),
        h("button", { type: "button", class: "fl-dl", title: `Download ${f.audio.name}`, "aria-label": `Download the audio of ${title}`, onclick: () => getAudio(f) }, "↓"))
        : h("span", { class: "muted" }, "Not on the server any more (the card stays)")),
    h("div", { class: "fl-row" }, h("span", { class: "fl-lab" }, "Stems"), ...stemCells(f, st, title)));
}

function stemCells(f, st, title) {
  if (st.state === "ready" && f.stems && f.stems.bytes) {
    const freed = stemBytes(f);
    return [...STEMS.map((n) => h("span", { class: "fl-file" }, cap(n), h("span", { class: "sz" }, fmtBytes(f.stems.bytes[n])),
        h("button", { type: "button", class: "fl-dl", title: `Download ${f.base} (${n}).flac`, "aria-label": `Download the ${n} stem of ${title}`, onclick: () => getStem(f, n) }, "↓"))),
      h("button", { type: "button", class: "fl-dl", title: "All four stems in one zip", "aria-label": `Download all stems of ${title} as a zip`, onclick: () => getStemsZip(f) }, "↓ All (zip)"),
      confirmButton({ class: "linklike danger-link", title: "Frees space. You can make them again later (a few minutes)." }, "Delete stems", `Delete ${fmtBytes(freed)}? Click again`, async () => {
        try { const n = await deleteStems(f); say(`Deleted the stems of ${title}, freeing ${fmtBytes(n)}.`, false); } catch (e) { say(e.message, false, true); }
        loadFiles();
      })];
  }
  if (stemsBusy(st)) {
    return [h("span", { class: "fl-job" }, h("span", { class: "spin" + (st.state === "queued" ? " idle" : ""), "aria-hidden": "true" }), stemsLabel(st)),
      st.state === "running" ? progBar(stemsEta(st).frac, `Stem separation progress for ${title}`) : null].filter(Boolean);
  }
  const off = f.audio && !data.stems_available;
  const kids = [h("span", { class: "muted", title: off ? "Stem separation isn't installed on this server (pip install 'tastr[stems]')" : null }, st.state === "error" ? "The last separation failed" : "None yet")];
  if (f.audio && data.stems_available) {
    kids.push(h("button", { type: "button", class: "linklike", title: "Split into vocals, bass, other and drums (a few minutes, once)", onclick: async (e) => {
      e.currentTarget.disabled = true;
      try { await startStems({ id: f.id }); } catch (err) { say(err.message, false, true); }
      renderFiles();
    } }, st.state === "error" ? "Try again" : "Separate stems"));
  }
  return kids;
}

function renderBulk(rows) {
  const box = $("fl-bulk");
  box.hidden = !flSelecting;
  if (!flSelecting) return;
  const byId = new Map(data.tracks.map((f) => [f.id, f]));
  const picked = [...flSel].map((id) => byId.get(id)).filter(Boolean);
  const withA = picked.filter((f) => f.audio), withS = picked.filter((f) => stemBytes(f)), noS = picked.filter((f) => f.audio && !stemBytes(f) && !stemsBusy(stemState(f)));
  const audB = sum(withA, audioBytes), stB = sum(withS, stemBytes);
  const allShown = rows.length && rows.every((t) => flSel.has(t.id));
  const act = (label, n, fn, cls) => h("button", { type: "button", class: cls || null, disabled: n ? null : "", onclick: fn }, label);
  box.replaceChildren(...[h("b", {}, `${picked.length} selected`),
    picked.length ? h("span", { class: "fl-selsz", title: "Total size of the selected tracks' files" }, h("b", {}, fmtBytes(audB + stB)), ` (audio ${fmtBytes(audB)}${stB ? ` · stems ${fmtBytes(stB)}` : ""})`) : null,
    rows.length ? h("button", { type: "button", class: "linklike", onclick: () => { for (const t of rows) allShown ? flSel.delete(t.id) : flSel.add(t.id); renderFiles(); } },
      allShown ? "Select none" : `Select all ${rows.length}${rows.length < data.tracks.length ? " shown" : ""}`) : null,
    picked.length ? h("button", { type: "button", class: "linklike", onclick: () => { flSel.clear(); renderFiles(); } }, "Clear") : null,
    h("span", { class: "fl-acts" },
      act(`↓ Audio${withA.length ? ` (${withA.length} · ${fmtBytes(audB)})` : ""}`, withA.length, () => postZip(withA.map((f) => f.id), "audio", audB)),
      act(`↓ Stems${withS.length ? ` (${withS.length} · ${fmtBytes(stB)})` : ""}`, withS.length, () => postZip(withS.map((f) => f.id), "stems", stB)),
      data.stems_available ? act(`Separate stems${noS.length ? ` (${noS.length})` : ""}`, noS.length, (e) => { e.currentTarget.disabled = true; separate(noS); }) : null,
      withS.length ? confirmButton({ class: "danger-solid" }, `Delete stems (${withS.length})…`, `Delete ${fmtBytes(stB)} of stems? Click again`, async () => {
        let freed = 0, done = 0, why = "";
        for (const f of withS) { try { freed += await deleteStems(f); done++; } catch (e) { why = e.message; } }
        say(`Deleted stems for ${done} track${done === 1 ? "" : "s"}, freeing ${fmtBytes(freed)}.${why ? ` ${withS.length - done} kept: ${why}` : ""}`, false, !!why && !done);
        loadFiles();
      }) : act("Delete stems…", 0, null, "danger-solid"))].filter(Boolean));
}

function fillFilters() {
  const vibes = [...new Set(tracks.flatMap((t) => t.tags || []))].sort(), keys = [...new Set(tracks.map((t) => t.camelot).filter(Boolean))].sort((a, b) => keyRank(a) - keyRank(b));
  $("fl-vibe").replaceChildren(h("option", { value: "" }, "Any vibe"), ...vibes.map((v) => h("option", { value: v }, v)));
  $("fl-key").replaceChildren(h("option", { value: "" }, "Any key"), ...keys.flatMap((k) => [h("option", { value: k }, k), h("option", { value: k + "~" }, `${k} + compatible`)]));
  syncTools();
}
function syncTools() {
  $("fl-q").value = flState.q; $("fl-sort").value = flState.sort; $("fl-vibe").value = flState.vibes[0] || ""; $("fl-key").value = flState.key;
  $("fl-bmin").value = flState.bmin; $("fl-bmax").value = flState.bmax; $("fl-up").value = flState.up; $("fl-up-from").value = flState.upFrom; $("fl-up-to").value = flState.upTo;
  $("fl-up-range").hidden = flState.up !== "range"; $("fl-lab").value = flState.lab;
  $("fl-show").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.v === flState.show)));
}
function setSelecting(on) {
  flSelecting = on;
  if (!on) flSel.clear();
  $("fl-sel").setAttribute("aria-pressed", String(on)); $("fl-sel").textContent = on ? "Done" : "Select";
  renderFiles();
}

// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  const on = (id, ev, fn) => $(id).addEventListener(ev, (e) => { fn(e.target.value); renderFiles(); });
  on("fl-q", "input", (v) => { flState.q = v; });
  on("fl-sort", "change", (v) => { flState.sort = v; });
  on("fl-vibe", "change", (v) => { flState.vibes = v ? [v] : []; });
  on("fl-key", "change", (v) => { flState.key = v; });
  on("fl-bmin", "input", (v) => { flState.bmin = v; });
  on("fl-bmax", "input", (v) => { flState.bmax = v; });
  on("fl-up", "change", (v) => { flState.up = v; $("fl-up-range").hidden = v !== "range"; });
  on("fl-up-from", "change", (v) => { flState.upFrom = v; });
  on("fl-up-to", "change", (v) => { flState.upTo = v; });
  on("fl-lab", "change", (v) => { flState.lab = v; });
  $("fl-fbtn").addEventListener("click", () => { const f = $("fl-filters"); f.hidden = !f.hidden; $("fl-fbtn").setAttribute("aria-expanded", String(!f.hidden)); });
  $("fl-clear").addEventListener("click", () => { Object.assign(flState, LIB_CLEAR(), { show: "" }); syncTools(); renderFiles(); });
  $("fl-sel").addEventListener("click", () => setSelecting(!flSelecting));
  $("fl-show").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { flState.show = b.dataset.v; syncTools(); renderFiles(); }));
  $("view-files").addEventListener("keydown", (e) => { if (e.key === "Escape" && flSelecting) setSelecting(false); });
  if (location.hash === "#files") switchTo("files");
}
