// The top navigation: Library, Explore, Glossary, Files and Admin views.

import { $, currentId } from "./core.js";
import { exTab, renderExplore, setExTab } from "./explore.js";
import { loadAdmin } from "./admin.js";
import { loadGlossaryData } from "./glossary.js";
import { loadFiles } from "./files.js";
import { openTrack, switchTo } from "./card.js";
import { routeOf, setRoute } from "./route.js";


// Wiring that touches the page or other modules, run once by main.js after every module has loaded.
export function init() {
  document.querySelectorAll("nav button").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("active", x === b));
      for (const v of ["library", "explore", "glossary", "files", "admin"]) $("view-" + v).hidden = v !== b.dataset.view;
      if (b.dataset.view === "explore") renderExplore();
      if (b.dataset.view === "glossary") loadGlossaryData();
      if (b.dataset.view === "files") loadFiles();
      if (b.dataset.view === "admin") loadAdmin();
      // Record the view in the address, unless it already names a spot in it (a track, a tab, a Glossary section).
      const v = b.dataset.view;
      if (routeOf().view !== v) setRoute(v === "library" ? (currentId ? `track/${currentId}` : "library") : v === "explore" ? `explore/${exTab}` : v);
    })
  );
  window.addEventListener("popstate", () => showRoute(routeOf()));
}

// showRoute opens the page an address names: on load (a refresh) and on Back/Forward.
function showRoute(r) {
  if (r.view === "library") {
    if (r.track && r.track !== currentId) openTrack(r.track); else switchTo("library");
  } else if (r.view === "explore") {
    if (r.sub) setExTab(r.sub);
    switchTo("explore");
  } else if (r.view === "glossary") {
    switchTo("glossary");
    const el = r.anchor && document.getElementById(r.anchor);
    if (el) el.scrollIntoView({ block: "start" });
  } else if (r.view === "admin") {
    // The Admin tab shows only once /api/config says you're an admin; find.js opens it then.
    if (!document.querySelector("nav button[data-view=admin]").hidden) switchTo("admin");
  } else switchTo(r.view);
}

// Run by main.js after every module's wiring, so the page a refresh lands on starts out set up.
export function start() {
  const r = routeOf();
  if (r.view !== "library" || r.track) showRoute(r);
}
