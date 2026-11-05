// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Capture orchestrator: virtual clock, clean frame, interaction script, framing,
 * legibility and DOM layers, written as a frame-locked PNG sequence with a
 * manifest, timed interaction events and layer exports.
 */
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

import type { Browser, Page } from "playwright-core";

import { cleanFrame, DESCRIBE_SRC, HIDE_SRC, REDACT_SRC, type CleanPage, type CleanRules } from "./clean.js";
import {
  DEFAULT_START_TIME_MS,
  FrameRecorder,
  SEEK_ANIMATIONS_SRC,
  VirtualClock,
  type ClockPage,
  type FrameManifest,
  type Point,
} from "./clock.js";
import { navigationEvent, runScript, type Action, type CaptureEvent, type InputPage } from "./dsl.js";
import {
  assertLegible,
  DEFAULT_MIN_TEXT_HEIGHT_PX,
  MEASURE_TEXT_SRC,
  subjectFill,
  type Rect,
  type Size,
  type TextBox,
} from "./framing.js";
import { ISOLATE_LAYER_SRC, LayerExporter, type LayerManifest, type LayerPage, type LayerSpec } from "./layers.js";

/** Everything the capture needs from a page. Playwright pages adapt via `playwrightPage`. */
export interface CapturePage extends ClockPage, InputPage, CleanPage, LayerPage {
  url(): string;
  viewport(): Size;
  deviceScaleFactor(): number;
  /** Glyph-box heights of visible text under `selector` (whole page when null). */
  measureText(selector: string | null): Promise<TextBox[]>;
}

export interface SubjectOptions {
  /** Element the frame is built around. */
  selector: string;
  /** CSS px of breathing room around the subject. */
  padding?: number;
  /** Output aspect, width / height (default 16 / 9). */
  aspect?: number;
}

export interface LegibilityConfig {
  /** Height in px the captured region occupies in the 1080p output (default 1080: full frame). */
  screenHeightPx?: number;
  minTextHeightPx?: number;
}

export interface CaptureOptions {
  /** First page to open (optional when the script starts with goto). */
  url?: string;
  script: readonly Action[];
  fps: number;
  outDir: string;
  /** Epoch ms the page clock reads at frame 0. */
  startTimeMs?: number;
  /** Clean rules; false disables cleaning. */
  clean?: CleanRules | false;
  subject?: SubjectOptions;
  /** Legibility check after device compositing; false disables it. */
  legibility?: LegibilityConfig | false;
  layers?: readonly LayerSpec[];
}

export interface CaptureResult {
  outDir: string;
  manifest: FrameManifest;
  events: CaptureEvent[];
  layers: LayerManifest[];
  clip: Rect | null;
}

function json(value: unknown): string {
  return `${JSON.stringify(value, null, 2)}\n`;
}

/** Run a capture against an already-open page. The clock is installed before the first navigation. */
export async function capture(page: CapturePage, options: CaptureOptions): Promise<CaptureResult> {
  const { fps, outDir } = options;
  const clock = new VirtualClock(page, fps, options.startTimeMs ?? DEFAULT_START_TIME_MS);
  const recorder = new FrameRecorder(outDir);
  const layers = new LayerExporter(outDir, options.layers ?? []);
  const events: CaptureEvent[] = [];
  const cursor: Point = { x: 0, y: 0 };
  const cleanRules = options.clean === false ? null : (options.clean ?? {});
  const legibility = options.legibility === false ? null : (options.legibility ?? {});
  let clip: Rect | null = null;
  let reframe = true;
  let lastUrl = "";

  await mkdir(outDir, { recursive: true });
  await clock.install();
  lastUrl = page.url();

  const emit = (event: Omit<CaptureEvent, "frame" | "timeMs">): void => {
    events.push({ ...event, frame: clock.frame, timeMs: clock.timeMs });
  };
  const onNavigation = async (url: string): Promise<void> => {
    lastUrl = page.url() || url;
    emit(navigationEvent(url));
    reframe = true;
  };

  const captureFrame = async (): Promise<void> => {
    const current = page.url();
    if (current !== lastUrl) await onNavigation(current);
    await clock.settle();
    if (cleanRules) await cleanFrame(page, cleanRules);
    if (reframe) {
      clip = null;
      if (options.subject) {
        const box = await page.boundingBox(options.subject.selector);
        if (!box) throw new Error(`subject ${options.subject.selector} not found`);
        clip = subjectFill(box, page.viewport(), options.subject.aspect, options.subject.padding);
      }
      reframe = false;
    }
    if (legibility) {
      const boxes = await page.measureText(options.subject?.selector ?? null);
      assertLegible(boxes, {
        captureCssHeight: clip ? clip.height : page.viewport().height,
        screenHeightPx: legibility.screenHeightPx ?? 1080,
        minTextHeightPx: legibility.minTextHeightPx ?? DEFAULT_MIN_TEXT_HEIGHT_PX,
        deviceScaleFactor: page.deviceScaleFactor(),
      });
    }
    const png = await page.screenshot({ clip: clip ?? undefined });
    await recorder.write(clock.frame, clock.timeMs, png, cursor);
    await layers.exportFrame(page, clock.frame, clip ?? undefined);
    await clock.advance();
  };

  if (options.url) {
    await page.goto(options.url);
    await onNavigation(options.url);
  }
  await runScript({ page, fps, clock, cursor, captureFrame, emit, onNavigation }, options.script);
  if (clock.frame === 0) await captureFrame();

  const manifest = recorder.manifest(fps);
  const layerManifest = layers.manifest();
  await writeFile(join(outDir, "manifest.json"), json(manifest));
  await writeFile(join(outDir, "events.json"), json(events));
  if ((options.layers ?? []).length > 0) await writeFile(join(outDir, "layers.json"), json(layerManifest));
  return { outDir, manifest, events, layers: layerManifest, clip };
}

/** In-page source: synthetic wheel at a point that scrolls the nearest scrollable ancestor at once. */
export const WHEEL_SRC = `(w) => {
  const target = document.elementFromPoint(w.x, w.y) || document.documentElement;
  const ev = new WheelEvent("wheel", { deltaX: w.dx, deltaY: w.dy, clientX: w.x, clientY: w.y, bubbles: true, cancelable: true, view: window });
  if (!target.dispatchEvent(ev)) return;
  for (let el = target; el && el !== document.body && el !== document.documentElement; el = el.parentElement) {
    const s = getComputedStyle(el);
    if (/(auto|scroll)/.test(s.overflowY + s.overflowX) && (el.scrollHeight > el.clientHeight || el.scrollWidth > el.clientWidth)) {
      el.scrollBy({ left: w.dx, top: w.dy, behavior: "instant" });
      return;
    }
  }
  (document.scrollingElement || document.documentElement).scrollBy({ left: w.dx, top: w.dy, behavior: "instant" });
}`;

function inPage<R>(page: Page, src: string, arg: unknown): Promise<R> {
  return page.evaluate(`(${src})(${JSON.stringify(arg)})`) as Promise<R>;
}

/** Adapt a Playwright page to the capture interface. */
export function playwrightPage(page: Page, deviceScaleFactor = 1): CapturePage {
  return {
    async installClock(epochMs) {
      await page.clock.install({ time: epochMs - 1000 });
      await page.clock.pauseAt(epochMs);
    },
    async runClock(ms) {
      await page.clock.runFor(ms);
    },
    async seekAnimations(elapsedMs) {
      await inPage(page, SEEK_ANIMATIONS_SRC, elapsedMs);
    },
    async goto(url) {
      await page.goto(url, { waitUntil: "load" });
    },
    url: () => page.url(),
    viewport: () => page.viewportSize() ?? { width: 1280, height: 720 },
    deviceScaleFactor: () => deviceScaleFactor,
    async mouseMove(x, y) {
      await page.mouse.move(x, y);
    },
    async mouseDown() {
      await page.mouse.down();
    },
    async mouseUp() {
      await page.mouse.up();
    },
    async keyPress(key) {
      await page.keyboard.press(key);
    },
    async keyType(text) {
      await page.keyboard.type(text);
    },
    async wheel(x, y, dx, dy) {
      await inPage(page, WHEEL_SRC, { x, y, dx, dy });
    },
    async boundingBox(selector) {
      const handle = await page.$(selector);
      if (!handle) return null;
      try {
        return await handle.boundingBox();
      } finally {
        await handle.dispose();
      }
    },
    describeElements: (query) => inPage(page, DESCRIBE_SRC, query),
    async hideElements(hits) {
      await inPage(page, HIDE_SRC, hits);
    },
    redactText: (query) => inPage(page, REDACT_SRC, query),
    measureText: (selector) => inPage(page, MEASURE_TEXT_SRC, selector),
    async isolateLayer(selector) {
      await inPage(page, ISOLATE_LAYER_SRC, selector);
    },
    async screenshot({ clip, omitBackground }) {
      return page.screenshot({ type: "png", caret: "hide", animations: "allow", clip, omitBackground });
    },
  };
}

export interface LaunchOptions extends CaptureOptions {
  viewport?: Size;
  deviceScaleFactor?: number;
  /** Browser binary; defaults to the Playwright-managed Chromium. */
  executablePath?: string;
  /** Browser channel such as "chrome" or "msedge". */
  channel?: string;
}

/** Launch headless Chromium, capture, and close the browser. */
export async function launchAndCapture(options: LaunchOptions): Promise<CaptureResult> {
  const { chromium } = await import("playwright-core");
  const browser: Browser = await chromium.launch({
    headless: true,
    executablePath: options.executablePath,
    channel: options.channel,
    args: ["--font-render-hinting=none", "--disable-lcd-text", "--force-color-profile=srgb", "--hide-scrollbars"],
  });
  try {
    const deviceScaleFactor = options.deviceScaleFactor ?? 1;
    const context = await browser.newContext({
      viewport: options.viewport ?? { width: 1280, height: 720 },
      deviceScaleFactor,
      locale: "en-US",
      timezoneId: "UTC",
      colorScheme: "light",
    });
    const page = await context.newPage();
    return await capture(playwrightPage(page, deviceScaleFactor), options);
  } finally {
    await browser.close();
  }
}
