// The help dialog and the 808 / Technics looks.

import { $, currentId } from "./core.js";
import { renderCard, shownCard } from "./card.js";
import { runSql } from "./find.js";

// "?" opens the shortcuts dialog unless the user is typing; the dialog closes on Esc, its ✕ or a click outside it.
const help = $("help");
const openHelp = () => { if (!help.open) help.showModal(); };

// Looks: data-look on <html> (808 by default; the inline script in index.html sets it before first paint).
// To add a look: a :root[data-look="x"] block in style.css, a button in .look-pick, and the id here and in that script.
const LOOKS = ["808", "technics"];
function setLook(v, rerender = true) {
  if (!LOOKS.includes(v)) v = LOOKS[0];
  document.documentElement.dataset.look = v;
  document.querySelectorAll(".look-pick button").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.look === v)));
  try { localStorage.setItem("tastr-look", v); } catch (_) {}
  // Re-render the open card so drawn charts pick up the new colors.
  if (rerender && shownCard && currentId === shownCard.id) renderCard(shownCard);
}

// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  $("help-btn").addEventListener("click", openHelp);
  $("help-close").addEventListener("click", () => help.close());
  help.addEventListener("click", (e) => { if (e.target === help) help.close(); });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "?" || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t.closest && t.closest("input, textarea, select, [contenteditable]")) return;
    e.preventDefault();
    openHelp();
  });
  $("sql").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runSql(); } });
  document.querySelectorAll(".look-pick button").forEach((b) => b.addEventListener("click", () => setLook(b.dataset.look)));
  setLook(document.documentElement.dataset.look, false);
}
