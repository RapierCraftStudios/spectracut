// SPDX-License-Identifier: AGPL-3.0-or-later
import { mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { crc32, deflateSync } from "node:zlib";

import { afterEach, describe, expect, it } from "vitest";

import {
  assertLegible,
  beatTimes,
  capture,
  checkLegibility,
  CleanError,
  cleanFrame,
  frameTimeMs,
  inertialDeltas,
  isCookieBanner,
  isDeniedLogo,
  isTransparentPng,
  LayerError,
  LegibilityError,
  pngColorType,
  redactSecrets,
  redactUrl,
  subjectFill,
  type Action,
  type CaptureEvent,
  type CapturePage,
  type CaptureOptions,
  type ElementInfo,
  type HideRequest,
  type Rect,
  type ScreenshotOptions,
  type TextBox,
} from "../src/index.js";

function chunk(type: string, data: Buffer): Buffer {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}

/** Tiny PNG whose pixels encode `seed`; colour type 6 (RGBA) or 2 (RGB). */
function png(seed: string, rgba: boolean): Buffer {
  const w = 4;
  const h = 4;
  const bpp = rgba ? 4 : 3;
  const bytes = Buffer.from(seed.padEnd(w * h * bpp, "."), "utf8").subarray(0, w * h * bpp);
  const raw = Buffer.alloc(h * (1 + w * bpp));
  for (let y = 0; y < h; y++) bytes.copy(raw, y * (1 + w * bpp) + 1, y * w * bpp, (y + 1) * w * bpp);
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0);
  ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8;
  ihdr[9] = rgba ? 6 : 2;
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

function element(over: Partial<ElementInfo>): ElementInfo {
  return {
    id: "0",
    tag: "div",
    idAttr: "",
    className: "",
    role: "",
    ariaLabel: "",
    title: "",
    text: "",
    src: "",
    alt: "",
    fixed: false,
    visible: true,
    matchesBannerSelector: false,
    matchesLogoSelector: false,
    ...over,
  };
}

/** Deterministic page: each screenshot is a pure function of the page state. */
class FakePage implements CapturePage {
  calls: string[] = [];
  clockMs = 0;
  clockInstalledAt: number | null = null;
  steps: number[] = [];
  currentUrl = "about:blank";
  pointer = { x: 0, y: 0, down: false };
  typed = "";
  scrollY = 0;
  hidden = new Set<string>();
  ignoreHide = false;
  isolated: string | null = null;
  opaqueLayers = false;
  elements: ElementInfo[] = [];
  text: TextBox[] = [{ text: "Dashboard", heightCss: 40 }];
  shots: ScreenshotOptions[] = [];
  boxes: Record<string, Rect> = { "#card": { x: 100, y: 100, width: 400, height: 225 } };

  async installClock(epochMs: number) {
    this.clockInstalledAt = epochMs;
    this.clockMs = epochMs;
  }
  async runClock(ms: number) {
    this.steps.push(ms);
    this.clockMs += ms;
  }
  async seekAnimations(elapsedMs: number) {
    this.calls.push(`seek:${elapsedMs}`);
  }
  async goto(url: string) {
    this.currentUrl = url;
  }
  url() {
    return this.currentUrl;
  }
  viewport() {
    return { width: 1920, height: 1080 };
  }
  deviceScaleFactor() {
    return 1;
  }
  async mouseMove(x: number, y: number) {
    this.pointer.x = x;
    this.pointer.y = y;
  }
  async mouseDown() {
    this.pointer.down = true;
  }
  async mouseUp() {
    this.pointer.down = false;
  }
  async keyPress(key: string) {
    this.typed += `<${key}>`;
  }
  async keyType(text: string) {
    this.typed += text;
  }
  async wheel(_x: number, _y: number, _dx: number, dy: number) {
    this.scrollY += dy;
  }
  async boundingBox(selector: string) {
    return this.boxes[selector] ?? null;
  }
  async describeElements() {
    return this.elements.map((e) => ({ ...e, visible: e.visible && !this.hidden.has(e.id) }));
  }
  async hideElements(hits: HideRequest[]) {
    if (!this.ignoreHide) for (const h of hits) this.hidden.add(h.id);
  }
  async redactText() {
    return 0;
  }
  async measureText() {
    return this.text;
  }
  async isolateLayer(selector: string | null) {
    this.calls.push(`isolate:${selector}`);
    this.isolated = selector;
  }
  async screenshot(options: ScreenshotOptions) {
    this.shots.push(options);
    const state = JSON.stringify([this.clockMs, this.pointer, this.typed, this.scrollY, this.isolated]);
    return png(state, Boolean(options.omitBackground) && !this.opaqueLayers);
  }
}

const dirs: string[] = [];
async function outDir(): Promise<string> {
  const d = await mkdtemp(join(tmpdir(), "spectracut-capture-"));
  dirs.push(d);
  return d;
}
afterEach(async () => {
  await Promise.all(dirs.splice(0).map((d) => rm(d, { recursive: true, force: true })));
});

const SCRIPT: Action[] = [
  { do: "move", to: { x: 300, y: 200 }, durationMs: 200, easing: "easeInOutCubic" },
  { do: "click" },
  { do: "type", text: "ab\n", bpm: 120, subdivision: 4 },
  { do: "scroll", velocity: 1200 },
  { do: "drag", from: { selector: "#card" }, to: { x: 900, y: 500 }, durationMs: 120 },
  { do: "goto", url: "https://app.example.test/next?token=abc123" },
  { do: "wait", frames: 2 },
];

async function run(page: FakePage, extra: Partial<CaptureOptions> = {}) {
  const dir = await outDir();
  const result = await capture(page, { url: "https://app.example.test/", script: SCRIPT, fps: 25, outDir: dir, ...extra });
  return { dir, result };
}

async function readFrames(dir: string): Promise<Buffer[]> {
  const names = (await readdir(join(dir, "frames"))).sort();
  return Promise.all(names.map((n) => readFile(join(dir, "frames", n))));
}

describe("virtual clock and determinism", () => {
  it("yields byte-identical frames and manifests when the same script re-runs", async () => {
    const a = await run(new FakePage());
    const b = await run(new FakePage());
    const fa = await readFrames(a.dir);
    const fb = await readFrames(b.dir);
    expect(fa.length).toBeGreaterThan(10);
    expect(fa).toEqual(fb);
    expect(await readFile(join(a.dir, "manifest.json"))).toEqual(await readFile(join(b.dir, "manifest.json")));
    expect(await readFile(join(a.dir, "events.json"))).toEqual(await readFile(join(b.dir, "events.json")));
    expect(new Set(a.result.manifest.frames.map((f) => f.sha256)).size).toBeGreaterThan(1);
  });

  it("installs a paused clock at a fixed time and advances exactly one frame per capture", async () => {
    const page = new FakePage();
    const { result } = await run(page, { startTimeMs: 1_700_000_000_000 });
    expect(page.clockInstalledAt).toBe(1_700_000_000_000);
    expect(page.steps.length).toBe(result.manifest.frames.length);
    expect(new Set(page.steps)).toEqual(new Set([40]));
    expect(page.calls.filter((c) => c.startsWith("seek:")).slice(0, 3)).toEqual(["seek:0", "seek:40", "seek:80"]);
    result.manifest.frames.forEach((f, i) => expect(f).toMatchObject({ index: i, timeMs: i * 40 }));
  });

  it("keeps frame times on whole milliseconds without drift", () => {
    expect([0, 1, 2, 3, 30].map((i) => frameTimeMs(i, 30))).toEqual([0, 33, 67, 100, 1000]);
    expect(() => frameTimeMs(1, 0)).toThrow(RangeError);
  });
});

describe("legibility after device compositing", () => {
  const small: TextBox[] = [{ text: "Fine print", heightCss: 14 }];

  it("fails the capture when subject text is under the minimum at 1080p", async () => {
    const page = new FakePage();
    page.text = small;
    // 14 css px in a 1080 css px capture composited into a 600 px device screen is 7.8 px.
    await expect(run(page, { legibility: { screenHeightPx: 600 } })).rejects.toBeInstanceOf(LegibilityError);
  });

  it("passes when the composited text clears the minimum", async () => {
    const page = new FakePage();
    page.text = [{ text: "Headline", heightCss: 40 }];
    await expect(run(page, { legibility: { screenHeightPx: 600 } })).resolves.toBeTruthy();
  });

  it("uses the subject crop, not the viewport, as the captured height", () => {
    const clip = subjectFill({ x: 100, y: 100, width: 400, height: 225 }, { width: 1920, height: 1080 });
    expect(clip).toEqual({ x: 100, y: 100, width: 400, height: 225 });
    const report = checkLegibility(small, {
      captureCssHeight: clip.height,
      screenHeightPx: 600,
      minTextHeightPx: 20,
      deviceScaleFactor: 2,
    });
    expect(report.failures).toEqual([]);
    expect(report.smallestPx).toBeCloseTo((14 * 600) / 225);
    expect(() =>
      assertLegible(small, { captureCssHeight: 1080, screenHeightPx: 1080, minTextHeightPx: 20, deviceScaleFactor: 1 }),
    ).toThrow(/under 20px/);
  });

  it("frames the subject at the output aspect inside the viewport", () => {
    const clip = subjectFill({ x: 1700, y: 50, width: 100, height: 100 }, { width: 1920, height: 1080 }, 16 / 9, 20);
    expect(clip.width / clip.height).toBeCloseTo(16 / 9, 1);
    expect(clip.x + clip.width).toBeLessThanOrEqual(1920);
    expect(clip.y).toBeGreaterThanOrEqual(0);
  });
});

describe("DOM layers", () => {
  it("exports each layer as a transparent PNG sequence", async () => {
    const page = new FakePage();
    const { dir, result } = await run(page, { layers: [{ name: "card", selector: "#card" }] });
    const names = (await readdir(join(dir, "layers", "card"))).sort();
    expect(names.length).toBe(result.manifest.frames.length);
    expect(names[0]).toBe("000000.png");
    for (const n of names) {
      const bytes = await readFile(join(dir, "layers", "card", n));
      expect(pngColorType(bytes)).toBe(6);
      expect(isTransparentPng(bytes)).toBe(true);
    }
    expect(page.shots.filter((s) => s.omitBackground).length).toBe(names.length);
    expect(page.calls.filter((c) => c.startsWith("isolate:")).slice(0, 2)).toEqual(["isolate:#card", "isolate:null"]);
    const manifest = JSON.parse(await readFile(join(dir, "layers.json"), "utf8"));
    expect(manifest[0]).toMatchObject({ name: "card", selector: "#card", order: 0 });
    expect(manifest[0].frames[0]).toEqual({ index: 0, box: { x: 100, y: 100, width: 400, height: 225 } });
  });

  it("rejects a layer frame without alpha", async () => {
    const page = new FakePage();
    page.opaqueLayers = true;
    await expect(run(page, { layers: [{ name: "card", selector: "#card" }] })).rejects.toBeInstanceOf(LayerError);
    expect(isTransparentPng(png("x", false))).toBe(false);
  });
});

describe("timed interaction events", () => {
  it("exports click, keystroke, scroll and navigation events with frame times", async () => {
    const { dir, result } = await run(new FakePage());
    const events: CaptureEvent[] = JSON.parse(await readFile(join(dir, "events.json"), "utf8"));
    expect(events).toEqual(result.events);
    const types = new Set(events.map((e) => e.type));
    for (const t of ["click", "keystroke", "scroll", "navigation", "drag"]) expect(types).toContain(t);
    for (const e of events) {
      expect(Number.isInteger(e.frame)).toBe(true);
      expect(e.timeMs).toBe(frameTimeMs(e.frame, 25));
    }
    const nav = events.filter((e) => e.type === "navigation");
    expect(nav.map((e) => e.url)).toEqual(["https://app.example.test/", "https://app.example.test/next?token=redacted"]);
    const click = events.find((e) => e.type === "click");
    expect(click).toMatchObject({ x: 300, y: 200, frame: 5 });
  });

  it("lands keystrokes on beat subdivisions", async () => {
    const { result } = await run(new FakePage());
    const keys = result.events.filter((e) => e.type === "keystroke");
    expect(keys.map((k) => k.key)).toEqual(["a", "b", "Enter"]);
    for (const k of keys) {
      expect((k.scheduledMs ?? -1) % 125).toBe(0);
      expect(k.timeMs).toBeGreaterThanOrEqual(k.scheduledMs ?? Infinity);
      expect(k.timeMs - (k.scheduledMs ?? 0)).toBeLessThan(40);
    }
    expect(beatTimes(130, 3, 120, 4)).toEqual([250, 375, 500]);
  });

  it("masks secret keystrokes", async () => {
    const page = new FakePage();
    const dir = await outDir();
    const r = await capture(page, { script: [{ do: "type", text: "sk", bpm: 240, secret: true }], fps: 25, outDir: dir });
    expect(r.events.map((e) => e.key)).toEqual(["•", "•"]);
    expect(page.typed).toBe("sk");
  });

  it("scrolls with inertia that decays to rest", async () => {
    const deltas = inertialDeltas(1200, 25);
    expect(deltas.length).toBeGreaterThan(3);
    for (let i = 1; i < deltas.length; i++) expect(deltas[i]).toBeLessThanOrEqual(deltas[i - 1] ?? 0);
    const { result } = await run(new FakePage());
    const scroll = result.events.filter((e) => e.type === "scroll");
    expect(scroll.map((e) => e.deltaY)).toEqual(deltas);
    expect(scroll[0]?.phase).toBe("start");
    expect(scroll.at(-1)?.phase).toBe("end");
  });
});

describe("clean frame", () => {
  it("recognises cookie and consent banners", () => {
    expect(isCookieBanner(element({ className: "consent-bar", fixed: true }))).toBe(true);
    expect(isCookieBanner(element({ fixed: true, text: "We use cookies. Accept all" }))).toBe(true);
    expect(isCookieBanner(element({ matchesBannerSelector: true }))).toBe(true);
    expect(isCookieBanner(element({ className: "consent-checkbox" }))).toBe(false);
    expect(isCookieBanner(element({ fixed: true, text: "Dashboard" }))).toBe(false);
    expect(isCookieBanner(element({ className: "cookie-bar", fixed: true }), { cookieBanners: false })).toBe(false);
  });

  it("recognises deny-listed logos by name, host and source", () => {
    const rules = { deniedLogos: [{ name: "Acme", hosts: ["cdn.acme.test"], srcIncludes: ["/brands/globex"] }] };
    expect(isDeniedLogo(element({ tag: "img", alt: "Acme logo" }), rules)).toBe(true);
    expect(isDeniedLogo(element({ tag: "img", src: "https://img.cdn.acme.test/x.png" }), rules)).toBe(true);
    expect(isDeniedLogo(element({ tag: "img", src: "https://example.test/brands/globex.svg" }), rules)).toBe(true);
    expect(isDeniedLogo(element({ tag: "img", src: "https://example.test/img/acme.svg" }), rules)).toBe(true);
    expect(isDeniedLogo(element({ tag: "svg", text: "Acme" }), rules)).toBe(true);
    expect(isDeniedLogo(element({ tag: "img", alt: "Acmeville skyline" }), rules)).toBe(false);
    expect(isDeniedLogo(element({ tag: "p", ariaLabel: "Acme" }), rules)).toBe(false);
    expect(isDeniedLogo(element({ matchesLogoSelector: true }), rules)).toBe(true);
  });

  it("redacts secrets in text and URLs", () => {
    const key = "sk_live_0123456789abcdef";
    expect(redactSecrets(`key ${key} end`)).toBe(`key ${"•".repeat(key.length)} end`);
    expect(redactSecrets("AKIAABCDEFGHIJKLMNOP")).toBe("•".repeat(20));
    expect(redactSecrets("plain text")).toBe("plain text");
    expect(redactSecrets("id-42", [/id-\d+/])).toBe("•••••");
    expect(redactUrl("https://u:p@x.test/a?api_key=1&q=ok")).toBe("https://x.test/a?api_key=redacted&q=ok");
  });

  it("hides matches before each frame and fails when one stays visible", async () => {
    const page = new FakePage();
    page.elements = [
      element({ id: "1", className: "consent-bar", fixed: true }),
      element({ id: "2", tag: "img", alt: "Acme" }),
      element({ id: "3", tag: "img", alt: "Our product" }),
    ];
    const rules = { deniedLogos: [{ name: "Acme" }] };
    expect(await cleanFrame(page, rules)).toMatchObject({ banners: 1, logos: 1 });
    expect([...page.hidden].sort()).toEqual(["1", "2"]);

    const stubborn = new FakePage();
    stubborn.elements = page.elements;
    stubborn.ignoreHide = true;
    await expect(run(stubborn, { clean: rules })).rejects.toBeInstanceOf(CleanError);
  });
});
