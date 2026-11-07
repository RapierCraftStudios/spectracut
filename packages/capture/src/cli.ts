// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Process entry for queue jobs: `spectracut-capture <spec.json>` runs one capture
 * from a JSON spec (the options of `launchAndCapture`) and prints a JSON summary.
 */
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import { launchAndCapture, type CaptureResult, type LaunchOptions } from "./capture.js";
import type { Action } from "./dsl.js";

export interface CaptureSummary {
  outDir: string;
  fps: number;
  frames: number;
  events: number;
  layers: number;
  /** Number of visible-text records (text.json). */
  texts: number;
}

function checkActions(value: unknown, field: string): void {
  if (!Array.isArray(value)) throw new TypeError(`capture spec needs a ${field} array`);
  for (const [i, action] of value.entries()) {
    if (typeof action !== "object" || action === null || Array.isArray(action)) {
      throw new TypeError(`capture spec ${field}[${i}] must be an object`);
    }
    if (typeof (action as Record<string, unknown>).do !== "string") {
      throw new TypeError(`capture spec ${field}[${i}] needs a do field`);
    }
    const a = action as Record<string, unknown>;
    if (a.do === "type" && (a.text === undefined) === (a.textEnv === undefined)) {
      throw new TypeError(`capture spec ${field}[${i}] type needs exactly one of text and textEnv`);
    }
    if (a.do === "click" && a.optional !== undefined && typeof a.optional !== "boolean") {
      throw new TypeError(`capture spec ${field}[${i}] click optional must be a boolean`);
    }
    if (a.do === "reveal") {
      if (typeof a.selector !== "string" || a.selector === "") {
        throw new TypeError(`capture spec ${field}[${i}] reveal needs a selector`);
      }
      if (a.below !== undefined && typeof a.below !== "string") {
        throw new TypeError(`capture spec ${field}[${i}] reveal below must be a string`);
      }
      if (a.margin !== undefined && (typeof a.margin !== "number" || !Number.isFinite(a.margin) || a.margin < 0)) {
        throw new RangeError(`capture spec ${field}[${i}] reveal margin must be a finite number >= 0`);
      }
    }
  }
}

function checkLegibilityChecks(legibility: unknown): void {
  if (typeof legibility !== "object" || legibility === null) return;
  const checks = (legibility as Record<string, unknown>).checks;
  if (checks === undefined) return;
  if (!Array.isArray(checks)) throw new TypeError("capture spec legibility.checks must be an array");
  for (const [i, check] of checks.entries()) {
    if (typeof check !== "object" || check === null || Array.isArray(check)) {
      throw new TypeError(`capture spec legibility.checks[${i}] must be an object`);
    }
    const c = check as Record<string, unknown>;
    if (typeof c.selector !== "string" || c.selector === "") {
      throw new TypeError(`capture spec legibility.checks[${i}] needs a selector`);
    }
    if (typeof c.minTextHeightPx !== "number" || !Number.isFinite(c.minTextHeightPx) || c.minTextHeightPx <= 0) {
      throw new RangeError(`capture spec legibility.checks[${i}] minTextHeightPx must be a positive number`);
    }
  }
}

/**
 * Replace every `type.textEnv` with the variable's value. Returns new arrays and
 * leaves the input untouched, so resolved values never go back to disk. A missing
 * variable is an error naming the variable, never a value.
 */
export function resolveTextEnv(
  actions: readonly Action[],
  env: Readonly<Record<string, string | undefined>> = process.env,
): Action[] {
  return actions.map((action) => {
    if (action.do !== "type" || action.textEnv === undefined) return action;
    const value = env[action.textEnv];
    if (value === undefined || value === "") {
      throw new Error(`capture spec types environment variable ${action.textEnv}, which is not set`);
    }
    const resolved = { ...action, text: value };
    delete resolved.textEnv;
    return resolved;
  });
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
  checkActions(spec.script, "script");
  if (spec.setup !== undefined) checkActions(spec.setup, "setup");
  if (spec.textSelectors !== undefined) {
    if (!Array.isArray(spec.textSelectors) || !spec.textSelectors.every((s) => typeof s === "string")) {
      throw new TypeError("capture spec textSelectors must be an array of strings");
    }
  }
  checkLegibilityChecks(spec.legibility);
  if (spec.url !== undefined && typeof spec.url !== "string") throw new TypeError("capture spec url must be a string");
  if (spec.colorScheme !== undefined && spec.colorScheme !== "light" && spec.colorScheme !== "dark") {
    throw new TypeError(`capture spec colorScheme must be "light" or "dark", got ${String(spec.colorScheme)}`);
  }
  return spec as unknown as LaunchOptions;
}

/** Run a spec and summarise the result. `launch` is injectable for tests. */
export async function runSpec(
  raw: unknown,
  launch: (options: LaunchOptions) => Promise<CaptureResult> = launchAndCapture,
  env: Readonly<Record<string, string | undefined>> = process.env,
): Promise<CaptureSummary> {
  const parsed = parseSpec(raw);
  const options: LaunchOptions = {
    ...parsed,
    script: resolveTextEnv(parsed.script, env),
    ...(parsed.setup ? { setup: resolveTextEnv(parsed.setup, env) } : {}),
  };
  const result = await launch(options);
  return {
    outDir: result.outDir,
    fps: result.manifest.fps,
    frames: result.manifest.frames.length,
    events: result.events.length,
    layers: result.layers.length,
    texts: result.texts?.length ?? 0,
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
