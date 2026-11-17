// View as (T34, hosted, admins only): see Tastr exactly as someone else sees it, read-only.
// POST /api/admin/view-as {email} starts and DELETE stops; both reload the page, which then runs as that person.
// While viewing, /api/config has viewing_as: {email, by, read_only}; the server refuses every write (core.js shows
// its message as a toast), and this module adds the banner and turns off uploads.

import { $, api, h, readOnlyToast } from "./core.js";

function reloadAt(view) {
  location.hash = `#${view}`;
  location.reload();
}

export async function startViewAs(email) {
  await api("/api/admin/view-as", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email }) });
  reloadAt("library");
}

export async function stopViewAs() {
  await api("/api/admin/view-as", { method: "DELETE" }).catch(() => {});
  reloadAt("admin");
}

// Called with /api/config's viewing_as once it's known (find.js).
export function showViewAs(va) {
  if (!va) return;
  const who = va.email.split("@")[0];
  document.documentElement.dataset.viewing = va.email;
  document.body.prepend(h("div", { id: "va-bar", role: "status", "aria-live": "polite" },
    h("span", { class: "va-eye", "aria-hidden": "true" }, "👁"), h("span", {}, "Viewing as ", h("b", {}, va.email)),
    h("span", { class: "va-ro" }, "· Read-only"),
    h("button", { type: "button", title: "Back to your own view and the Admin tab", onclick: stopViewAs }, "Exit")));
  $("drop").prepend(h("p", { id: "va-drop-note", class: "muted" },
    `This is ${who}'s library. Uploads are off while you're viewing as ${who}.`));
  const refuse = (e) => {
    if (!e.dataTransfer || ![...e.dataTransfer.types].includes("Files")) return;
    e.preventDefault(); e.stopImmediatePropagation();
    if (e.type === "drop") readOnlyToast(`Read-only while viewing as ${va.email}.`);
  };
  for (const ev of ["dragenter", "dragover", "drop"]) document.addEventListener(ev, refuse, true);
}
