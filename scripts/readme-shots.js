// Takes the README screenshots from a running Tastr server. Run through scripts/readme_shots.py,
// which builds a demo library and starts the server: python scripts/readme_shots.py
const { chromium } = require("playwright");

const [base, out] = process.argv.slice(2);
const W = 1100; // CSS pixels; shots are taken at 1.5x and shown smaller in the README

(async () => {
  const browser = await chromium.launch(process.env.TASTR_CHROMIUM ? { executablePath: process.env.TASTR_CHROMIUM } : {});
  const page = await browser.newPage({ viewport: { width: W, height: 900 }, deviceScaleFactor: 1.5, colorScheme: "light" });
  await page.addInitScript(() => localStorage.setItem("tastr-daw", "strudel"));
  await page.goto(base);
  await page.getByText("Night Shift", { exact: true }).click();
  await page.locator("details.sec").first().waitFor();
  await page.waitForTimeout(800);
  const sec = (title) => page.locator("details.sec").filter({ has: page.locator(":scope > summary", { hasText: title }) });
  const body = (title) => sec(title).locator(":scope > .sec-content");
  const shot = (name, opts) => page.screenshot({ path: `${out}/${name}.png`, animations: "disabled", ...opts });
  // An element with a margin of page background at the sides and bottom (its section bar sits right above it), so it doesn't look cut out.
  const PAD = 16;
  const shotOf = async (name, loc) => {
    await loc.scrollIntoViewIfNeeded();
    const b = await loc.boundingBox();
    const sy = await page.evaluate(() => window.scrollY);
    await shot(name, { fullPage: true, clip: { x: b.x - PAD, y: b.y + sy, width: b.width + 2 * PAD, height: b.height + PAD } });
  };

  // 1. The library with an open card: header, sidebar, stats and the energy chart.
  await shot("library", { clip: { x: 0, y: 0, width: W, height: 900 } });

  // 2. Spectrogram view of the energy chart.
  await sec("Energy & sections").getByRole("tab", { name: "Spectrogram", exact: true }).click();
  await page.waitForTimeout(1500);
  await shotOf("spectrogram", body("Energy & sections"));

  // 3. Drums, bass & chords for the busiest section.
  await sec("Drums, bass & chords").getByRole("tab", { name: /^Drop/ }).first().click();
  await page.waitForTimeout(300);
  await shotOf("drums-bass-chords", body("Drums, bass & chords"));

  // 4. The Strudel sketch.
  await shotOf("strudel", body("Try this in"));

  // 5. The Glossary's Camelot entry, with the wheel set to the open track's key.
  await page.evaluate(() => openGlossary("g-basics"));
  await page.waitForTimeout(500);
  await shotOf("glossary", page.locator("dd.gl-cam"));

  // 6. Explore > Find tracks: the Simple builder (the first example, keys that mix with 8A at 120-128 BPM) and its results.
  await page.locator("nav button[data-view=explore]").click();
  await page.locator("#q-mode button[data-mode=simple]").click();
  await page.getByRole("button", { name: "Examples ▾" }).click();
  await page.getByRole("menuitem", { name: /^Mixes with 8A/ }).click();
  await page.waitForTimeout(800);
  await page.locator(".q-head").scrollIntoViewIfNeeded();
  const top = await page.locator(".q-head").boundingBox(), end = await page.locator("#sql-result").boundingBox();
  const sy = await page.evaluate(() => window.scrollY);
  await shot("find-tracks", { fullPage: true, clip: { x: top.x - PAD, y: top.y + sy - PAD, width: W - 2 * (top.x - PAD), height: end.y + end.height - top.y + 2 * PAD } });

  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
