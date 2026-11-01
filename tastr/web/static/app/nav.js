// The top navigation: Library, Explore, Glossary, Files and Admin views.

import { $ } from "./core.js";
import { renderExplore } from "./explore.js";
import { loadAdmin } from "./admin.js";
import { loadGlossaryData } from "./glossary.js";
import { loadFiles } from "./files.js";


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
    })
  );
}
