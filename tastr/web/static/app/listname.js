// Naming a list before it's saved (TAS-79): every "Save … as a list" in Explore › Blend opens this dialog, with the
// suggested name already cut to fit the server's 80 characters, so a long title can't make the save fail.

import { h } from "./core.js";
import { LIST_NAME_MAX, fitName, listNamed, saveList } from "./lists.js";

const plural = (n) => `${n} track${n === 1 ? "" : "s"}`;

// Resolves to the line to show after saving (saveList's), or null when cancelled. Errors reject.
export function askListName(suggest, ids) {
  return new Promise((resolve, reject) => {
    const dlg = h("dialog", { class: "ln-dlg", "aria-labelledby": "ln-title" });
    const inp = h("input", { type: "text", maxlength: String(LIST_NAME_MAX), "aria-label": "List name", spellcheck: "false", autocomplete: "off" });
    inp.value = fitName(suggest);
    const count = h("span", { class: "ln-count muted", "aria-hidden": "true" }), note = h("p", { class: "ln-note", role: "status", "aria-live": "polite" });
    const save = h("button", { type: "submit", class: "primary" }, "Save");
    let done = false;
    const upd = () => {
      const v = inp.value.trim(), have = v && listNamed(v);
      count.textContent = `${inp.value.length}/${LIST_NAME_MAX}`; count.classList.toggle("full", inp.value.length >= LIST_NAME_MAX);
      note.className = "ln-note" + (v ? "" : " err");
      note.textContent = !v ? "Give the list a name." : have ? `“${have.name}” already exists: the tracks not in it yet are added to it.` : "";
      save.textContent = have ? "Add to it" : "Save"; save.disabled = !v;
    };
    inp.addEventListener("input", upd);
    const form = h("form", { method: "dialog" },
      h("h3", { id: "ln-title" }, `Save ${plural(ids.length)} as a list`),
      h("label", { class: "ln-row" }, inp, count), note,
      h("div", { class: "menu-confirm-btns" }, h("button", { type: "button", onclick: () => dlg.close() }, "Cancel"), save));
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const v = inp.value.trim(); if (!v) return;
      done = true; dlg.close();
      saveList(v, ids).then(resolve, reject);
    });
    dlg.append(form);
    dlg.addEventListener("close", () => { dlg.remove(); if (!done) resolve(null); });
    document.body.append(dlg);
    upd(); dlg.showModal(); inp.focus(); inp.select();
  });
}
