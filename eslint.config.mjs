// Lint for the web UI scripts (tastr/web/static). strudel.js, engine.js and hpss.js are plain browser scripts that
// share globals, so no-undef stays off for them. The UI itself (static/app/) is ES modules: there no-undef is on, with
// the browser APIs it uses and the plain scripts' globals listed, so a missing import fails lint.
// complexity caps how tangled one function can get (TAS-36 split energyChart, grooveView, labelBar, renderCard and
// strudelCode, which were up to 101); the ceiling is library.js renderList, so lower it when that one is split.
// CI runs it with `npx eslint@10.12.0` only when a script changes; see .github/workflows/test.yml.
const BROWSER = ["Audio", "Blob", "CustomEvent", "Event", "EventTarget", "FormData", "Image", "TextEncoder", "URL",
  "URLSearchParams", "Worker", "XMLHttpRequest", "atob", "cancelAnimationFrame", "clearInterval", "clearTimeout", "console",
  "document", "fetch", "innerWidth", "localStorage", "location", "matchMedia", "navigator", "performance",
  "requestAnimationFrame", "setInterval", "setTimeout", "window"];
const PLAIN_SCRIPTS = ["STRUDEL_STEMS", "strudelCode", "strudelLink", "hpssHarmonic"]; // strudel.js, hpss.js

export default [
  {
    files: ["tastr/web/static/**/*.js"],
    languageOptions: { ecmaVersion: 2023, sourceType: "script" },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: {
      "no-unused-vars": ["error", { args: "none", varsIgnorePattern: "^_", caughtErrors: "none" }],
      "no-dupe-keys": "error",
      "no-dupe-else-if": "error",
      "no-duplicate-case": "error",
      "no-unreachable": "error",
      "no-self-assign": "error",
      "no-constant-condition": ["error", { checkLoops: false }],
      "no-cond-assign": "error",
      "no-redeclare": "error",
      "no-shadow-restricted-names": "error",
      "no-sparse-arrays": "error",
      "no-unsafe-negation": "error",
      "no-unsafe-finally": "error",
      "use-isnan": "error",
      "valid-typeof": "error",
      "eqeqeq": ["error", "smart"],
      "complexity": ["error", 35],
    },
  },
  {
    files: ["tastr/web/static/app/**/*.js"],
    languageOptions: {
      sourceType: "module",
      globals: Object.fromEntries([...BROWSER, ...PLAIN_SCRIPTS].map((n) => [n, "readonly"])),
    },
    rules: { "no-undef": "error", "no-import-assign": "error" },
  },
];
