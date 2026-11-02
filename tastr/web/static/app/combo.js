// A track picker you can type in (Explore › Blend's From and To, TAS-79): focus shows every track by title, typing
// filters by title or artist, the arrows and Enter (or a click) pick, and Escape or leaving without a pick puts the
// old value back. Replaces a <datalist>, which only ever showed matches for the text already in the box.

import { h, isNum } from "./core.js";
import { comboHits, comboName } from "./playlist-core.js";

// list(): the tracks to pick from; getId(): the picked track's id; onPick(track) when one is picked.
export function trackCombo(inp, list, getId, onPick) {
  const pop = h("ul", { class: "tc-pop", role: "listbox", id: `${inp.id}-list`, hidden: "" });
  const wrap = h("span", { class: "tc-wrap" });
  inp.replaceWith(wrap);
  wrap.append(inp, h("button", { type: "button", class: "tc-x", tabindex: "-1", "aria-label": "Clear", title: "Clear and show every track",
    onclick: () => { inp.value = ""; inp.focus(); open(""); } }, "×"), pop);
  inp.removeAttribute("list");
  inp.autocomplete = "off";
  inp.placeholder = "Type to find a track…";
  for (const [k, v] of Object.entries({ role: "combobox", "aria-autocomplete": "list", "aria-controls": pop.id, "aria-expanded": "false" })) inp.setAttribute(k, v);
  let r = { hits: [], shown: [], at: -1 };
  const meta = (t) => [isNum(t.bpm) ? `${t.bpm.toFixed(0)} BPM` : null, t.camelot].filter(Boolean).join(" · ");
  function open(q) {
    r = comboHits(list(), q, getId());
    const rows = r.shown.map((t, i) => {
      const nm = comboName(t), k = r.s ? nm.toLowerCase().indexOf(r.s) : -1, title = t.title || `Track ${t.id}`;
      // the match highlighted; the artist in muted text after the title
      const label = k >= 0 ? [nm.slice(0, k), h("mark", {}, nm.slice(k, k + r.s.length)), nm.slice(k + r.s.length)] : [title, t.artist ? h("span", { class: "muted" }, ` — ${t.artist}`) : null];
      const li = h("li", { role: "option", id: `${pop.id}-${i}`, "aria-selected": String(i === r.at), class: t.id === getId() ? "cur" : "" },
        h("span", { class: "tc-t" }, ...label), h("span", { class: "tc-m" }, meta(t)));
      li.addEventListener("pointerdown", (e) => { e.preventDefault(); pick(t); });  // before the box loses focus
      return li;
    });
    if (r.more) rows.push(h("li", { class: "tc-more", role: "presentation" }, `${r.more} more: keep typing to narrow it down`));
    if (!r.hits.length) rows.push(h("li", { class: "tc-more", role: "presentation" }, `No track matches “${String(q).trim()}”`));
    pop.replaceChildren(...rows);
    pop.hidden = false; inp.setAttribute("aria-expanded", "true"); mark();
  }
  const mark = () => {
    pop.querySelectorAll("[role=option]").forEach((li, i) => li.setAttribute("aria-selected", String(i === r.at)));
    const li = pop.querySelector('[aria-selected="true"]');
    if (li) { inp.setAttribute("aria-activedescendant", li.id); li.scrollIntoView({ block: "nearest" }); } else inp.removeAttribute("aria-activedescendant");
  };
  const close = () => { pop.hidden = true; inp.setAttribute("aria-expanded", "false"); inp.removeAttribute("aria-activedescendant"); };
  const restore = () => { inp.value = comboName(list().find((t) => t.id === getId())); };
  function pick(t) { inp.value = comboName(t); close(); onPick(t); }
  inp.addEventListener("focus", () => { inp.select(); open(""); });  // the whole list, even with a track picked
  inp.addEventListener("click", () => { if (pop.hidden) open(""); });
  inp.addEventListener("input", () => open(inp.value));
  inp.addEventListener("keydown", (e) => {
    const n = r.shown.length;
    if (e.key === "ArrowDown") { e.preventDefault(); if (pop.hidden) open(""); else { r.at = Math.min(n - 1, r.at + 1); mark(); } }
    else if (e.key === "ArrowUp") { e.preventDefault(); r.at = Math.max(0, r.at - 1); mark(); }
    else if (e.key === "Enter") { e.preventDefault(); if (!pop.hidden && r.shown[r.at]) pick(r.shown[r.at]); }
    else if (e.key === "Escape" && !pop.hidden) { e.preventDefault(); e.stopPropagation(); close(); restore(); inp.select(); }
  });
  inp.addEventListener("blur", () => setTimeout(() => { if (!wrap.contains(document.activeElement)) { close(); restore(); } }, 0));
  return { restore };
}
