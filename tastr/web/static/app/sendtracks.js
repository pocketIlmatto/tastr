// Send copies of selected tracks to someone else's library (T27; sandbox v190, ui-sandbox/send-tracks-spec.md).
// GET /api/admin/copy-tracks previews, POST copies (admins only, tastr/library_copy.py). They get the same analysis
// and share the audio file; your edits, labels, cues and lists stay yours. Tracks they already have, and tracks
// without audio on the server, are skipped.

import { api, h } from "./core.js";

const plural = (n) => `${n} track${n === 1 ? "" : "s"}`;
const valid = (v) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v);

export function ago(iso) {
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (!isFinite(s)) return "–";
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return s < 172800 ? "yesterday" : `${Math.floor(s / 86400)} days ago`;
}

const row = (r, status) => h("li", { class: status.skip ? "skip" : "" },
  h("span", { class: "snd-t" }, r.title || `Track ${r.id}`, r.artist ? h("span", { class: "muted" }, ` — ${r.artist}`) : null),
  h("span", { class: "snd-st" }, status.text));

// Opens the dialog for `ids`; `say(line)` gets the result line for the bulk bar once something was sent.
export function askSendTracks(ids, me, say) {
  const dlg = h("dialog", { class: "ln-dlg snd-dlg", "aria-labelledby": "snd-title" });
  const inp = h("input", { type: "email", list: "snd-people", placeholder: "friend@example.com", "aria-label": "Their email address", autocomplete: "off", spellcheck: "false" });
  const people = h("datalist", { id: "snd-people" });
  const quick = h("div", { class: "snd-quick" });
  const who = h("p", { class: "snd-who", role: "status", "aria-live": "polite" });
  const list = h("ul", { class: "snd-list" });
  const sum = h("p", { class: "snd-sum" });
  const err = h("p", { class: "error", role: "alert" });
  const go = h("button", { type: "submit", class: "primary", disabled: "" }, "Send");
  let pv = null, seq = 0, timer = null;

  const toSend = () => (pv ? pv.tracks.filter((t) => !t.they_have && t.has_audio) : []);
  const reset = (cls, ...text) => {
    pv = null; list.replaceChildren(); sum.textContent = ""; err.textContent = ""; go.disabled = true; go.textContent = "Send";
    who.className = "snd-who" + (cls ? ` ${cls}` : ""); who.replaceChildren(...text);
  };
  const paint = (v) => {
    const name = v.split("@")[0];
    who.className = "snd-who" + (pv.first_seen ? "" : " warn");
    who.replaceChildren(...(pv.first_seen
      ? [h("b", {}, name), ` · last seen ${pv.last_seen ? ago(pv.last_seen) : "–"} · has ${plural(pv.their_tracks)}`]
      : [h("b", {}, v), " hasn't used Tastr yet. They'll find the tracks when they first sign in, if they can get in (Cloudflare Access must let them)."]));
    list.replaceChildren(...pv.tracks.map((t) => row(t, t.they_have ? { skip: true, text: "they have it" } : !t.has_audio ? { skip: true, text: "no audio here" } : { text: "✓" })));
    const n = toSend().length, had = pv.tracks.filter((t) => t.they_have).length, mute = pv.tracks.filter((t) => !t.they_have && !t.has_audio).length;
    sum.textContent = n
      ? `${n} to send${had ? `, ${had} they already have` : ""}${mute ? `, ${mute} without audio on the server` : ""}.`
      : had === pv.tracks.length ? "Nothing to send: they already have all of these." : "Nothing to send: none of these can be sent.";
    go.disabled = !n; go.textContent = n ? `Send ${n} cop${n === 1 ? "y" : "ies"}` : "Send";
  };
  const upd = () => {
    clearTimeout(timer);
    const v = inp.value.trim().toLowerCase(), mine = ++seq;
    if (!v) return reset("muted", "Whose library should get copies?");
    if (!valid(v)) return reset("muted", "Type their whole email address.");
    if (v === me) return reset("err", "That's you. Give someone else's address.");
    reset("muted", "Checking…");
    timer = setTimeout(async () => {
      try {
        const p = await api(`/api/admin/copy-tracks?to=${encodeURIComponent(v)}&ids=${ids.join(",")}`);
        if (mine !== seq) return;
        pv = p; paint(v);
      } catch (e) { if (mine === seq) reset("err", e.message); }
    }, 250);
  };
  inp.addEventListener("input", upd);

  const form = h("form", { method: "dialog" },
    h("h3", { id: "snd-title" }, `Send copies of ${plural(ids.length)}`),
    h("label", { class: "snd-to" }, h("span", {}, "To"), inp), people, quick, who, list, sum,
    h("p", { class: "muted snd-note" }, "They get the same analysis and can play the audio. Your edits, labels, cue points and lists stay yours, and they don't see the rest of your library."),
    err,
    h("div", { class: "menu-confirm-btns" }, h("button", { type: "button", onclick: () => dlg.close() }, "Cancel"), go));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const send = toSend(); if (!pv || !send.length) return;
    const n = send.length, name = pv.to.split("@")[0], known = !!pv.first_seen, before = pv.their_tracks, rows = pv.tracks;
    go.disabled = true; inp.disabled = true; err.textContent = "";
    go.replaceChildren(h("span", { class: "spin", "aria-hidden": "true" }), `Sending ${n}…`);
    let res;
    try {
      res = await api("/api/admin/copy-tracks", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ to: pv.to, track_ids: send.map((t) => t.id) }) });
    } catch (ex) {
      err.textContent = ex.message; inp.disabled = false; paint(pv.to); return;
    }
    const sent = new Set(res.copied.map((c) => c.from)), k = sent.size;
    const had = rows.filter((t) => !sent.has(t.id) && t.they_have).length, mute = rows.filter((t) => !sent.has(t.id) && !t.they_have).length;
    say(`Sent ${plural(k)} to ${name}.` + (had ? ` Skipped ${had} they already had.` : "") + (mute ? ` Skipped ${mute} without audio on the server.` : ""));
    const done = h("button", { type: "button", class: "primary", onclick: () => dlg.close() }, "Done");
    form.replaceChildren(...[
      h("div", { class: "snd-ok", role: "status" }, h("span", { class: "snd-okmark", "aria-hidden": "true" }, "✓"),
        h("div", {}, h("h3", { id: "snd-title" }, `Sent ${plural(k)} to ${name}`),
          h("p", { class: "muted" }, known ? `They're in ${name}'s library now (${plural(before + k)}). They'll see them next time they open Tastr.`
            : `They're waiting in ${pv.to}'s library for when they first sign in.`))),
      h("ul", { class: "snd-list" }, ...rows.map((t) => row(t, sent.has(t.id) ? { text: "sent ✓" } : { skip: true, text: t.they_have ? "skipped: they had it" : "skipped: no audio" }))),
      rows.length > k ? h("p", { class: "snd-sum" }, `${k} sent, ${rows.length - k} skipped.`) : null,
      h("p", { class: "muted snd-note" }, "It's also in Admin › Activity."),
      h("div", { class: "menu-confirm-btns" }, done)].filter(Boolean));
    done.focus();
  });

  dlg.append(form);
  dlg.addEventListener("close", () => { clearTimeout(timer); seq++; dlg.remove(); });
  document.body.append(dlg);
  upd(); dlg.showModal(); inp.focus();

  // People who already use this server, to pick from (the admin usage list).
  api("/api/admin/usage?range=30").then((u) => {
    const others = (u.users || []).filter((p) => p.email && p.email !== me);
    people.replaceChildren(...others.map((p) => h("option", { value: p.email }, p.last_seen ? `seen ${ago(p.last_seen)}` : "")));
    quick.replaceChildren(...(others.length ? [h("span", { class: "muted" }, "People here: "),
      ...others.map((p) => h("button", { type: "button", class: "linklike", onclick: () => { inp.value = p.email; upd(); inp.focus(); } }, p.email.split("@")[0]))] : []));
  }, () => { /* typing an address still works */ });
}
