// Your edits to a track's drums, bass and chords, kept per card and autosaved with the track on the server.

import { api } from "./core.js";

const partEdits = new Map(), EDIT_PARTS = ["drums", "bass", "chords", "drumsrc"];
export const savedEdits = new Map(), saveTimers = new Map(); // track id -> JSON last loaded or saved; pending autosaves
export const allEdits = (card) => { if (!partEdits.has(card.id)) partEdits.set(card.id, {}); return partEdits.get(card.id); };
export const editsFor = (card, part) => (allEdits(card)[part] ||= {});
// Section keys (section idx, or "all") that have any edit.
// JSON of the parts that hold edits, so empty parts don't count as a change.
const editsJson = (e) => JSON.stringify(Object.fromEntries(Object.entries(e || {}).filter(([, v]) => v && Object.keys(v).length)));
export const editedKeys = (edits) => { const ks = new Set(); for (const p of EDIT_PARTS) Object.keys((edits || {})[p] || {}).forEach((k) => ks.add(k)); return [...ks]; };
export async function loadEdits(id) {
  if (savedEdits.has(id)) return;
  const e = await api(`/api/tracks/${id}/edits`).catch(() => null);
  if (!e || savedEdits.has(id)) return; // unloaded edits never autosave, so a failed load can't overwrite them
  partEdits.set(id, e);
  savedEdits.set(id, editsJson(e));
}
export function putEdits(id, keepalive) {
  clearTimeout(saveTimers.get(id));
  saveTimers.delete(id);
  const body = editsJson(partEdits.get(id));
  const prev = savedEdits.get(id);
  if (body === prev) return Promise.resolve();
  savedEdits.set(id, body);
  return fetch(`/api/tracks/${id}/edits`, { method: "PUT", headers: { "Content-Type": "application/json" }, body, keepalive })
    .then((r) => { if (!r.ok) throw new Error(r.statusText); })
    .catch(() => { if (savedEdits.get(id) === body) savedEdits.set(id, prev); }); // the next change tries again
}
export function saveEdits(card) {
  if (!savedEdits.has(card.id) || editsJson(allEdits(card)) === savedEdits.get(card.id)) return;
  clearTimeout(saveTimers.get(card.id));
  saveTimers.set(card.id, setTimeout(() => putEdits(card.id), 1000));
}
// Send pending autosaves now: before re-analyzing (which remaps the saved edits) and when the page goes away.
export const flushEdits = (id) => (saveTimers.has(id) ? putEdits(id) : Promise.resolve());
// Forget a track's edits so the next render loads them again (after re-analyzing or deleting it).
export const dropEdits = (id) => { clearTimeout(saveTimers.get(id)); saveTimers.delete(id); partEdits.delete(id); savedEdits.delete(id); };
// Replace a card's edits (restoring a version, or discarding them all).
export const setAllEdits = (card, edits) => { partEdits.set(card.id, edits); };
