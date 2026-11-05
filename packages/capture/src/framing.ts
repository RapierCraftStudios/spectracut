// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Framing: the subject fills the frame, and captures fail when subject text would
 * be under the minimum height in the 1080p output after device compositing.
 */
import { redactSecrets } from "./clean.js";

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Size {
  width: number;
  height: number;
}

/**
 * Clip rect (CSS px) that frames `subject` plus `padding` at `aspect` (width / height),
 * as large as needed and never outside the viewport. Values are whole pixels.
 */
export function subjectFill(subject: Rect, viewport: Size, aspect = 16 / 9, padding = 0): Rect {
  if (!(aspect > 0)) throw new RangeError(`aspect must be positive, got ${aspect}`);
  let w = subject.width + 2 * padding;
  let h = subject.height + 2 * padding;
  if (w / h > aspect) h = w / aspect;
  else w = h * aspect;
  const fit = Math.min(1, viewport.width / w, viewport.height / h);
  w *= fit;
  h *= fit;
  const cx = subject.x + subject.width / 2;
  const cy = subject.y + subject.height / 2;
  const x = Math.min(Math.max(cx - w / 2, 0), viewport.width - w);
  const y = Math.min(Math.max(cy - h / 2, 0), viewport.height - h);
  const rx = Math.round(x);
  const ry = Math.round(y);
  return {
    x: rx,
    y: ry,
    width: Math.min(Math.round(w), viewport.width - rx),
    height: Math.min(Math.round(h), viewport.height - ry),
  };
}

/** A run of rendered text: its glyph-box height in CSS px. */
export interface TextBox {
  text: string;
  heightCss: number;
}

export interface LegibilityOptions {
  /** Height in CSS px of the captured region (clip or viewport). */
  captureCssHeight: number;
  /** Height in px that region occupies in the 1080p output once composited into the device screen. */
  screenHeightPx: number;
  /** Minimum text height in output px at 1080p. */
  minTextHeightPx: number;
  /** Device pixel ratio of the capture, reported for the source sampling density. */
  deviceScaleFactor: number;
}

export const DEFAULT_MIN_TEXT_HEIGHT_PX = 20;

export interface LegibilityFinding extends TextBox {
  /** Height in output px at 1080p. */
  effectivePx: number;
  /** Height in captured device pixels. */
  sourcePx: number;
}

export interface LegibilityReport {
  scale: number;
  smallestPx: number | null;
  failures: LegibilityFinding[];
}

export class LegibilityError extends Error {
  constructor(readonly report: LegibilityReport, readonly minTextHeightPx: number) {
    const list = report.failures
      .slice(0, 5)
      .map((f) => `"${redactSecrets(f.text).slice(0, 40)}" ${f.effectivePx.toFixed(1)}px`)
      .join(", ");
    super(
      `subject text under ${minTextHeightPx}px at 1080p after device compositing ` +
        `(${report.failures.length} run(s)): ${list}`,
    );
    this.name = "LegibilityError";
  }
}

/** Output scale: capture CSS px to 1080p output px. */
export function compositeScale(opts: Pick<LegibilityOptions, "captureCssHeight" | "screenHeightPx">): number {
  if (!(opts.captureCssHeight > 0) || !(opts.screenHeightPx > 0)) {
    throw new RangeError("captureCssHeight and screenHeightPx must be positive");
  }
  return opts.screenHeightPx / opts.captureCssHeight;
}

export function checkLegibility(boxes: readonly TextBox[], opts: LegibilityOptions): LegibilityReport {
  const scale = compositeScale(opts);
  let smallest: number | null = null;
  const failures: LegibilityFinding[] = [];
  for (const b of boxes) {
    if (!(b.heightCss > 0) || b.text.trim() === "") continue;
    const effectivePx = b.heightCss * scale;
    smallest = smallest === null ? effectivePx : Math.min(smallest, effectivePx);
    if (effectivePx + 1e-9 < opts.minTextHeightPx) {
      failures.push({ ...b, effectivePx, sourcePx: b.heightCss * opts.deviceScaleFactor });
    }
  }
  return { scale, smallestPx: smallest, failures };
}

/** Throws LegibilityError when any text run is under the minimum. */
export function assertLegible(boxes: readonly TextBox[], opts: LegibilityOptions): LegibilityReport {
  const report = checkLegibility(boxes, opts);
  if (report.failures.length > 0) throw new LegibilityError(report, opts.minTextHeightPx);
  return report;
}

/** In-page source: glyph-box heights of visible text under a root (selector or body). */
export const MEASURE_TEXT_SRC = `(selector) => {
  const root = selector ? document.querySelector(selector) : document.body;
  if (!root) return [];
  const bounds = root.getBoundingClientRect();
  const out = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = (node.nodeValue || "").trim();
    const parent = node.parentElement;
    if (!text || !parent) continue;
    if (parent.closest("script, style, noscript, [data-spectracut-hidden]")) continue;
    if (!parent.checkVisibility({ opacityProperty: true, visibilityProperty: true })) continue;
    range.selectNodeContents(node);
    let h = Infinity;
    for (const r of range.getClientRects()) {
      if (r.width <= 0 || r.height <= 0) continue;
      if (r.right < bounds.left || r.left > bounds.right || r.bottom < bounds.top || r.top > bounds.bottom) continue;
      if (r.right <= 0 || r.bottom <= 0 || r.left >= innerWidth || r.top >= innerHeight) continue;
      h = Math.min(h, r.height);
    }
    if (h !== Infinity) out.push({ text: text.slice(0, 80), heightCss: h });
  }
  return out;
}`;
