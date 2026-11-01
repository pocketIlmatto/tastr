// Saved lists of tracks, kept per person on the server (/api/lists). Library › Actions saves to them and Explore › Blend
// compares them; each listens with onListsChange.

import { api } from "./core.js";

export let lists = []; // [{ id, name, created_at, track_ids }], oldest first
const listeners = new Set();
export const onListsChange = (fn) => listeners.add(fn);
const changed = () => listeners.forEach((fn) => fn(lists));
const json = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const plural = (n) => `${n} track${n === 1 ? "" : "s"}`;

export async function loadLists() {
  lists = await api("/api/lists");
  changed();
  return lists;
}
// Save tracks under a name; a name you already have (any case) adds them to that list. Resolves to the line to show.
export async function saveList(name, ids) {
  const n = (name || "").trim() || `List ${lists.length + 1}`;
  const r = await api("/api/lists", json("POST", { name: n, track_ids: ids }));
  await loadLists();
  return r.created ? `Saved “${r.name}” with ${plural(r.track_ids.length)}.` : `Added ${r.added} to “${r.name}”.`;
}
export async function addToList(id, ids) {
  const r = await api(`/api/lists/${id}/tracks`, json("POST", { track_ids: ids }));
  await loadLists();
  return `Added ${r.added} to “${r.name}”.`;
}
export async function deleteList(id) {
  await api(`/api/lists/${id}`, { method: "DELETE" });
  await loadLists();
}
