// SPDX-License-Identifier: AGPL-3.0-or-later
/** Name of the capture package. */
export const CAPTURE_PACKAGE = "@spectracut/capture" as const;

/** Frame duration in milliseconds for a capture at the given frame rate. */
export function frameDurationMs(fps: number): number {
  if (!Number.isFinite(fps) || fps <= 0) {
    throw new RangeError(`fps must be a positive number, got ${fps}`);
  }
  return 1000 / fps;
}

export * from "./capture.js";
export * from "./clean.js";
export * from "./clock.js";
export * from "./dsl.js";
export * from "./framing.js";
export * from "./layers.js";
