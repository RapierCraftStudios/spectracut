// SPDX-License-Identifier: AGPL-3.0-or-later
/** Name of the capture package; the capture engine lands in a later issue. */
export const CAPTURE_PACKAGE = "@spectracut/capture" as const;

/** Frame duration in milliseconds for a capture at the given frame rate. */
export function frameDurationMs(fps: number): number {
  if (!Number.isFinite(fps) || fps <= 0) {
    throw new RangeError(`fps must be a positive number, got ${fps}`);
  }
  return 1000 / fps;
}
