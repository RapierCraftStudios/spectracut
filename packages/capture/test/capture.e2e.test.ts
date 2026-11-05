// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * End-to-end capture in real headless Chromium (playwright-core). Runs only with
 * SPECTRACUT_E2E=1 so the plain `vitest run` needs no browser.
 */
import { mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

import { chromium, type Browser } from "playwright-core";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { launchAndCapture, pngColorType, type Action, type LaunchOptions } from "../src/index.js";

const E2E = process.env.SPECTRACUT_E2E === "1";

const LOGO_SVG =
  "data:image/svg+xml," +
  encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60"><rect width="120" height="60" fill="#00ff01"/></svg>');

const FIXTURE = `<!doctype html>
<html><head><meta charset="utf-8"><style>
  body { margin: 0; font: 20px/1.4 sans-serif; background: #ffffff; color: #111111; }
  header { display: flex; gap: 16px; align-items: center; padding: 16px; }
  #spinner { width: 40px; height: 40px; background: #3366cc; animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  #card { margin: 24px; padding: 24px; width: 480px; background: #eef2ff; border-radius: 12px; }
  input { font: 20px sans-serif; width: 300px; }
  .tall { height: 2400px; }
  .consent-bar { position: fixed; left: 0; right: 0; bottom: 0; height: 160px; background: #ff00ff; color: #ffffff; font-size: 24px; }
</style></head><body>
  <header><img id="logo" alt="Acme" src="${LOGO_SVG}" width="120" height="60"><h1>Dashboard</h1><div id="spinner"></div></header>
  <section id="card"><p>Revenue <span id="tick">0</span></p><input id="search" placeholder="Search"></section>
  <div class="tall"></div>
  <div class="consent-bar">We use cookies. <button>Accept</button></div>
  <script>
    let n = 0;
    setInterval(() => { document.getElementById("tick").textContent = String(++n) + " " + new Date().toISOString(); }, 100);
    setTimeout(() => {
      const late = document.createElement("div");
      late.id = "onetrust-banner-sdk";
      late.style.cssText = "position:fixed;top:0;right:0;width:300px;height:200px;background:#ff00ff";
      late.textContent = "Cookie settings";
      document.body.appendChild(late);
    }, 200);
  </script>
</body></html>`;

const SCRIPT: Action[] = [
  { do: "wait", ms: 120 },
  { do: "move", to: { selector: "#search" }, durationMs: 200 },
  { do: "click" },
  { do: "type", text: "ok", bpm: 120 },
  { do: "scroll", velocity: 600, friction: 6, minVelocity: 200 },
  { do: "wait", ms: 160 },
];

let dir = "";
let fixtureUrl = "";
let decoder: Browser | undefined;

beforeAll(async () => {
  if (!E2E) return;
  dir = await mkdtemp(join(tmpdir(), "spectracut-capture-e2e-"));
  const file = join(dir, "fixture.html");
  await writeFile(file, FIXTURE);
  fixtureUrl = pathToFileURL(file).href;
  decoder = await chromium.launch({ headless: true });
});

afterAll(async () => {
  await decoder?.close();
  if (dir) await rm(dir, { recursive: true, force: true });
});

function options(name: string, extra: Partial<LaunchOptions> = {}): LaunchOptions {
  return {
    url: fixtureUrl,
    script: SCRIPT,
    fps: 25,
    outDir: join(dir, name),
    viewport: { width: 1280, height: 720 },
    clean: { deniedLogos: [{ name: "Acme" }] },
    layers: [{ name: "card", selector: "#card" }],
    ...extra,
  };
}

async function frames(out: string, sub = "frames"): Promise<Buffer[]> {
  const names = (await readdir(join(out, sub))).sort();
  return Promise.all(names.map((n) => readFile(join(out, sub, n))));
}

/** Count pixels near each colour, and fully transparent pixels, by decoding the PNG in Chromium. */
async function count(png: Buffer, colours: number[][]): Promise<{ hits: number[]; transparent: number }> {
  if (!decoder) throw new Error("decoder browser not started");
  const page = await decoder.newPage();
  try {
    const expr = `(async () => {
      const img = new Image();
      img.src = "data:image/png;base64,${png.toString("base64")}";
      await img.decode();
      const c = document.createElement("canvas");
      c.width = img.width; c.height = img.height;
      const g = c.getContext("2d");
      g.drawImage(img, 0, 0);
      const d = g.getImageData(0, 0, c.width, c.height).data;
      const colours = ${JSON.stringify(colours)};
      const hits = colours.map(() => 0);
      let transparent = 0;
      for (let i = 0; i < d.length; i += 4) {
        if (d[i + 3] === 0) { transparent++; continue; }
        colours.forEach((k, j) => {
          if (Math.abs(d[i] - k[0]) <= 3 && Math.abs(d[i + 1] - k[1]) <= 3 && Math.abs(d[i + 2] - k[2]) <= 3) hits[j]++;
        });
      }
      return { hits, transparent };
    })()`;
    return (await page.evaluate(expr)) as { hits: number[]; transparent: number };
  } finally {
    await page.close();
  }
}

const MAGENTA = [255, 0, 255];
const LOGO_GREEN = [0, 255, 1];

describe.skipIf(!E2E)("capture in headless Chromium", () => {
  it("never shows cookie banners or deny-listed logos in frames", async () => {
    const result = await launchAndCapture(options("clean"));
    const shots = await frames(result.outDir);
    expect(shots.length).toBeGreaterThan(15);
    for (const shot of shots) {
      const { hits } = await count(shot, [MAGENTA, LOGO_GREEN]);
      expect(hits).toEqual([0, 0]);
    }
  }, 120_000);

  it("shows them when cleaning is off (control)", async () => {
    const result = await launchAndCapture(options("dirty", { clean: false, layers: [] }));
    const shots = await frames(result.outDir);
    const last = shots.at(-1);
    if (!last) throw new Error("no frames");
    const first = await count(shots[0] ?? last, [MAGENTA, LOGO_GREEN]);
    expect(first.hits[0]).toBeGreaterThan(1000);
    expect(first.hits[1]).toBeGreaterThan(1000);
  }, 120_000);

  it("yields byte-identical frames when the same script re-runs", async () => {
    const a = await launchAndCapture(options("run-a"));
    const b = await launchAndCapture(options("run-b"));
    const fa = await frames(a.outDir);
    const fb = await frames(b.outDir);
    expect(fa.length).toBe(fb.length);
    fa.forEach((f, i) => expect(f.equals(fb[i] ?? Buffer.alloc(0)), `frame ${i}`).toBe(true));
    expect(a.manifest).toEqual(b.manifest);
    expect(new Set(a.manifest.frames.map((f) => f.sha256)).size).toBeGreaterThan(5);
    const layersA = await frames(a.outDir, join("layers", "card"));
    const layersB = await frames(b.outDir, join("layers", "card"));
    expect(layersA).toEqual(layersB);
  }, 180_000);

  it("exports layers as transparent PNGs and interactions as timed events", async () => {
    const out = join(dir, "clean");
    const layer = await frames(out, join("layers", "card"));
    expect(layer.length).toBe((await frames(out)).length);
    const first = layer[0];
    if (!first) throw new Error("no layer frames");
    expect(pngColorType(first)).toBe(6);
    expect((await count(first, [])).transparent).toBeGreaterThan(100_000);
    const events = JSON.parse(await readFile(join(out, "events.json"), "utf8")) as Array<{ type: string }>;
    const types = new Set(events.map((e) => e.type));
    for (const t of ["navigation", "click", "keystroke", "scroll"]) expect(types).toContain(t);
  });
});
