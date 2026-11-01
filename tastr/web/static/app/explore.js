// Explore: the Find tracks | Blend tabs.

import { $ } from "./core.js";
import { loadTracks } from "./library.js";
import { loadSaved, qMode, renderSimple, setQMode } from "./find.js";
import { renderBlend } from "./blend.js";

const EX_TABS = ["find", "blend"];
let exTab = "find", findReady = false;

function renderTab() {
  if (exTab === "blend") { renderBlend(); return; }
  // Find tracks: the first visit sets it up; later ones refresh Simple's results (the library may have changed)
  if (!findReady) { findReady = true; setQMode(qMode); loadSaved(); } else if (qMode === "simple") renderSimple();
}

export async function renderExplore() {
  await loadTracks();
  renderTab();
}

function setExTab(t, focus) {
  if (!EX_TABS.includes(t)) t = "find";
  exTab = t;
  try { localStorage.setItem("tastr-ex-tab", t); } catch (_) {}
  document.querySelectorAll(".ex-tabs [role=tab]").forEach((b) => {
    const on = b.dataset.ex === t;
    b.setAttribute("aria-selected", String(on)); b.tabIndex = on ? 0 : -1;
    if (on && focus) b.focus();
  });
  EX_TABS.forEach((k) => ($("ex-" + k).hidden = k !== t));
}

// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  try { exTab = localStorage.getItem("tastr-ex-tab") || "find"; } catch (_) {}
  document.querySelectorAll(".ex-tabs [role=tab]").forEach((b) => {
    b.addEventListener("click", () => { setExTab(b.dataset.ex); renderTab(); });
    b.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      const i = EX_TABS.indexOf(exTab), n = EX_TABS.length;
      setExTab(EX_TABS[(i + (e.key === "ArrowRight" ? 1 : n - 1)) % n], true); renderTab();
    });
  });
  setExTab(exTab);
}
