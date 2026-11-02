// Saved lists of tracks, kept per person on the server (/api/lists). Library › Actions saves to them and Explore › Blend
// compares them; each listens with onListsChange.

import { api } from "./core.js";
import { LIST_NAME_MAX, fitName } from "./playlist-core.js";

export { LIST_NAME_MAX, fitName };

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
  const n = fitName(name, LIST_NAME_MAX) || `List ${lists.length + 1}`;  // never over the server's limit
  const r = await api("/api/lists", json("POST", { name: n, track_ids: ids }));
  await loadLists();
  return r.created ? `Saved “${r.name}” with ${plural(r.track_ids.length)}.` : `Added ${r.added} to “${r.name}”.`;
}
export async function addToList(id, ids) {
  const r = await api(`/api/lists/${id}/tracks`, json("POST", { track_ids: ids }));
  await loadLists();
  return `Added ${r.added} to “${r.name}”.`;
}
export const listNamed = (name) => lists.find((l) => l.name.toLowerCase() === String(name || "").trim().toLowerCase());
// Resolves to the deleted list, so an Undo can make it again (restoreList).
export async function deleteList(id) {
  const gone = lists.find((l) => l.id === id);
  await api(`/api/lists/${id}`, { method: "DELETE" });
  await loadLists();
  return gone;
}
// Undo a delete: the same name and the same tracks in the same order (the server keeps the order of track_ids).
export async function restoreList(l) {
  const r = await api("/api/lists", json("POST", { name: l.name, track_ids: l.track_ids }));
  await loadLists();
  return r;
}
