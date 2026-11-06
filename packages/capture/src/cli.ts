// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Process entry for queue jobs: `spectracut-capture <spec.json>` runs one capture
 * from a JSON spec (the options of `launchAndCapture`) and prints a JSON summary.
 */
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import { launchAndCapture, type CaptureResult, type LaunchOptions } from "./capture.js";

export interface CaptureSummary {
  outDir: string;
  fps: number;
  frames: number;
  events: number;
  layers: number;
}

/** Validate a decoded job spec and return it as launch options. */
export function parseSpec(raw: unknown): LaunchOptions {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new TypeError("capture spec must be a JSON object");
  }
  const spec = raw as Record<string, unknown>;
  if (typeof spec.outDir !== "string" || spec.outDir === "") throw new TypeError("capture spec needs an outDir");
  if (typeof spec.fps !== "number" || !Number.isFinite(spec.fps) || spec.fps <= 0) {
    throw new RangeError(`capture spec fps must be a positive number, got ${String(spec.fps)}`);
  }
  if (!Array.isArray(spec.script)) throw new TypeError("capture spec needs a script array");
  if (spec.url !== undefined && typeof spec.url !== "string") throw new TypeError("capture spec url must be a string");
  return spec as unknown as LaunchOptions;
}

/** Run a spec and summarise the result. `launch` is injectable for tests. */
export async function runSpec(
  raw: unknown,
  launch: (options: LaunchOptions) => Promise<CaptureResult> = launchAndCapture,
): Promise<CaptureSummary> {
  const options = parseSpec(raw);
  const result = await launch(options);
  return {
    outDir: result.outDir,
    fps: result.manifest.fps,
    frames: result.manifest.frames.length,
    events: result.events.length,
    layers: result.layers.length,
  };
}

async function main(argv: readonly string[]): Promise<number> {
  const path = argv[0];
  if (path === undefined) {
    process.stderr.write("usage: spectracut-capture <spec.json>\n");
    return 2;
  }
  try {
    const summary = await runSpec(JSON.parse(await readFile(path, "utf8")));
    process.stdout.write(`${JSON.stringify(summary)}\n`);
    return 0;
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    return 1;
  }
}

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === process.argv[1]) {
  process.exitCode = await main(process.argv.slice(2));
}
