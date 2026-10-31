// Lint for the web UI scripts (tastr/web/static). They are plain browser scripts that share globals across
// files (app.js calls strudelCode from strudel.js), so no-undef stays off; these rules catch real mistakes.
// CI runs it with `npx eslint@10.12.0` only when a script changes; see .github/workflows/test.yml.
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
    },
  },
];
