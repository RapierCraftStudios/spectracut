// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * DOM layer export for exploded-UI shots: each layer renders alone on a
 * transparent background, one RGBA PNG per frame, plus its box per frame.
 */
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

import { frameFileName } from "./clock.js";
import type { Rect } from "./framing.js";

export interface LayerSpec {
  /** Directory name under layers/ ([A-Za-z0-9_-]+). */
  name: string;
  selector: string;
}

export interface ScreenshotOptions {
  clip?: Rect;
  omitBackground?: boolean;
}

/** The page operations layer export needs. */
export interface LayerPage {
  /** Show only the first element matching `selector` (null restores the page). */
  isolateLayer(selector: string | null): Promise<void>;
  boundingBox(selector: string): Promise<Rect | null>;
  screenshot(options: ScreenshotOptions): Promise<Uint8Array>;
}

export class LayerError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LayerError";
  }
}

const PNG_SIGNATURE = [137, 80, 78, 71, 13, 10, 26, 10];

/** PNG colour type from the IHDR chunk. */
export function pngColorType(png: Uint8Array): number {
  if (png.length < 26 || PNG_SIGNATURE.some((b, i) => png[i] !== b)) {
    throw new LayerError("not a PNG");
  }
  return png[25] ?? -1;
}

/** True when the PNG carries alpha (grey+alpha, RGBA, or a tRNS chunk). */
export function isTransparentPng(png: Uint8Array): boolean {
  const type = pngColorType(png);
  if (type === 4 || type === 6) return true;
  let off = 8;
  while (off + 8 <= png.length) {
    const len = ((png[off] ?? 0) << 24) | ((png[off + 1] ?? 0) << 16) | ((png[off + 2] ?? 0) << 8) | (png[off + 3] ?? 0);
    const kind = String.fromCharCode(png[off + 4] ?? 0, png[off + 5] ?? 0, png[off + 6] ?? 0, png[off + 7] ?? 0);
    if (kind === "tRNS") return true;
    if (kind === "IDAT" || kind === "IEND") return false;
    off += 12 + len;
  }
  return false;
}

export interface LayerFrame {
  index: number;
  box: Rect | null;
}

export interface LayerManifest {
  name: string;
  selector: string;
  /** Stacking order: position in the layer list, back to front. */
  order: number;
  frames: LayerFrame[];
}

const NAME_RE = /^[A-Za-z0-9_-]+$/;

export class LayerExporter {
  private readonly manifests: LayerManifest[];

  constructor(
    private readonly outDir: string,
    readonly layers: readonly LayerSpec[],
  ) {
    const seen = new Set<string>();
    for (const l of layers) {
      if (!NAME_RE.test(l.name)) throw new LayerError(`invalid layer name: ${JSON.stringify(l.name)}`);
      if (seen.has(l.name)) throw new LayerError(`duplicate layer name: ${l.name}`);
      seen.add(l.name);
    }
    this.manifests = layers.map((l, order) => ({ name: l.name, selector: l.selector, order, frames: [] }));
  }

  /** Export every layer for one frame; the page is restored afterwards. */
  async exportFrame(page: LayerPage, index: number, clip?: Rect): Promise<void> {
    for (const [i, layer] of this.layers.entries()) {
      const manifest = this.manifests[i];
      if (!manifest) continue;
      const box = await page.boundingBox(layer.selector);
      let png: Uint8Array;
      try {
        await page.isolateLayer(layer.selector);
        png = await page.screenshot({ clip, omitBackground: true });
      } finally {
        await page.isolateLayer(null);
      }
      if (!isTransparentPng(png)) {
        throw new LayerError(`layer ${layer.name} frame ${index} is not a transparent PNG`);
      }
      const dir = join(this.outDir, "layers", layer.name);
      await mkdir(dir, { recursive: true });
      await writeFile(join(dir, frameFileName(index)), png);
      manifest.frames.push({ index, box });
    }
  }

  manifest(): LayerManifest[] {
    return this.manifests.map((m) => ({ ...m, frames: m.frames.map((f) => ({ ...f })) }));
  }
}

/** In-page source: show only the layer element on a transparent page, or restore. */
export const ISOLATE_LAYER_SRC = `(selector) => {
  document.getElementById("spectracut-layer-style")?.remove();
  for (const el of document.querySelectorAll("[data-spectracut-layer]")) el.removeAttribute("data-spectracut-layer");
  if (selector === null) return true;
  const el = document.querySelector(selector);
  if (!el) return false;
  el.setAttribute("data-spectracut-layer", "");
  const style = document.createElement("style");
  style.id = "spectracut-layer-style";
  style.textContent = "html, body { background: transparent !important; }" +
    " body *:not([data-spectracut-layer]):not([data-spectracut-layer] *) { visibility: hidden !important; }" +
    " [data-spectracut-layer] { visibility: visible !important; }";
  document.head.appendChild(style);
  return true;
}`;
