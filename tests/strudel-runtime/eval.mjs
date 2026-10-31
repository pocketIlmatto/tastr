// Test-only: evaluates Strudel programs with Strudel itself and prints the events they play, for
// tests/test_strudel_runtime.py. Input (stdin): [{ code, from, to }], cycles to query. Output: one
// { events: [[begin, end, value], ...] } or { error } per program. Needs `npm ci` in this folder first.
import { registerHooks } from "node:module";

// Strudel logs as it loads and evaluates; only the JSON result goes to stdout.
console.log = console.info = () => {};

// @strudel/core imports kabelsalat's browser REPL, which Node can't load and pattern queries never use.
registerHooks({
  resolve(specifier, context, next) {
    return specifier === "@kabelsalat/web"
      ? { url: "data:text/javascript,export class SalatRepl {}", shortCircuit: true }
      : next(specifier, context);
  },
});

const core = await import("@strudel/core");
const mini = await import("@strudel/mini");
const tonal = await import("@strudel/tonal");
const { transpiler } = await import("@strudel/transpiler");
await core.evalScope(core, mini, tonal);
mini.miniAllStrings();
// What strudel.cc adds around the core: sliders (the value they start at), sample loading, the punchcard widget.
await core.evalScope({ sliderWithID: (_id, value) => core.pure(value), samples: () => {} });
core.Pattern.prototype._punchcard = function () { return this; };

let input = "";
for await (const chunk of process.stdin) input += chunk;
const out = [];
for (const { code, from = 0, to = 1 } of JSON.parse(input)) {
  try {
    const repl = core.repl({ transpiler, defaultOutput: () => {}, getTime: () => 0 });
    const pattern = await repl.evaluate(code, false);
    if (!pattern) throw repl.state.evalError || new Error("No pattern returned");
    const events = pattern.queryArc(from, to).filter((h) => h.hasOnset())
      .map((h) => [h.whole.begin.valueOf(), h.whole.end.valueOf(), h.value])
      .sort((a, b) => a[0] - b[0] || JSON.stringify(a[2]).localeCompare(JSON.stringify(b[2])));
    out.push({ events });
  } catch (e) {
    out.push({ error: String(e && e.message ? e.message : e) });
  }
}
process.stdout.write(JSON.stringify(out) + "\n");
