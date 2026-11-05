// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Virtual clock and frame recording.
 *
 * Time on the page only moves when the capture advances it: `page.clock` controls
 * Date, timers and requestAnimationFrame, and every Web Animation (CSS animations
 * and transitions) is paused and seeked to the same virtual time each frame, so a
 * re-run of the same script yields the same pixels.
 */
import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

/** Wall-clock epoch the page sees at frame 0 (2026-01-01T00:00:00Z). */
export const DEFAULT_START_TIME_MS = Date.UTC(2026, 0, 1);

/** Virtual time of a frame in whole milliseconds since capture start. */
export function frameTimeMs(index: number, fps: number): number {
  if (!Number.isFinite(fps) || fps <= 0) {
    throw new RangeError(`fps must be a positive number, got ${fps}`);
  }
  return Math.round((index * 1000) / fps);
}

/** Number of frames that cover a duration, at least one. */
export function framesFor(durationMs: number, fps: number): number {
  return Math.max(1, Math.round((durationMs * fps) / 1000));
}

/** The page operations the clock needs. */
export interface ClockPage {
  /** Install a paused fake clock reading `epochMs`; must run before the first navigation. */
  installClock(epochMs: number): Promise<void>;
  /** Advance the paused clock, firing due timers and animation frames. */
  runClock(ms: number): Promise<void>;
  /** Pause every Web Animation and seek it to the capture's virtual elapsed time. */
  seekAnimations(elapsedMs: number): Promise<void>;
}

/**
 * In-page source: pause every animation and seek it relative to the virtual time
 * it was first seen, so CSS animations follow the virtual clock, not wall time.
 */
export const SEEK_ANIMATIONS_SRC = `(elapsed) => {
  const born = window.__spectracutBorn || (window.__spectracutBorn = new WeakMap());
  for (const a of document.getAnimations()) {
    if (!born.has(a)) born.set(a, elapsed);
    a.pause();
    a.currentTime = elapsed - born.get(a);
  }
}`;

export class VirtualClock {
  /** Index of the next frame to capture. */
  frame = 0;

  constructor(
    private readonly page: ClockPage,
    readonly fps: number,
    readonly startTimeMs: number = DEFAULT_START_TIME_MS,
  ) {
    frameTimeMs(0, fps);
  }

  /** Virtual milliseconds since capture start at the current frame. */
  get timeMs(): number {
    return frameTimeMs(this.frame, this.fps);
  }

  async install(): Promise<void> {
    await this.page.installClock(this.startTimeMs);
  }

  /** Bring animations to the current virtual time before a screenshot. */
  async settle(): Promise<void> {
    await this.page.seekAnimations(this.timeMs);
  }

  /** Move to the next frame. */
  async advance(): Promise<void> {
    const step = frameTimeMs(this.frame + 1, this.fps) - this.timeMs;
    await this.page.runClock(step);
    this.frame += 1;
  }
}

export interface Point {
  x: number;
  y: number;
}

export interface FrameEntry {
  index: number;
  timeMs: number;
  sha256: string;
  /** Cursor position (CSS px) for compositing a cursor later. */
  cursor: Point;
}

export interface FrameManifest {
  fps: number;
  frames: FrameEntry[];
}

export function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

export function frameFileName(index: number): string {
  return `${String(index).padStart(6, "0")}.png`;
}

/** Writes frames/NNNNNN.png and keeps the per-frame manifest. */
export class FrameRecorder {
  readonly frames: FrameEntry[] = [];
  private readonly dir: string;

  constructor(outDir: string) {
    this.dir = join(outDir, "frames");
  }

  async write(index: number, timeMs: number, png: Uint8Array, cursor: Point): Promise<FrameEntry> {
    await mkdir(this.dir, { recursive: true });
    await writeFile(join(this.dir, frameFileName(index)), png);
    const entry: FrameEntry = { index, timeMs, sha256: sha256(png), cursor: { ...cursor } };
    this.frames.push(entry);
    return entry;
  }

  manifest(fps: number): FrameManifest {
    return { fps, frames: this.frames.map((f) => ({ ...f, cursor: { ...f.cursor } })) };
  }
}
