// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it } from "vitest";

import { parseSpec, resolveTextEnv, runSpec, type CaptureResult, type LaunchOptions } from "../src/index.js";

const SPEC = { outDir: "/tmp/out", fps: 30, script: [{ do: "wait", ms: 100 }] };

describe("capture cli spec", () => {
  it("accepts a valid spec", () => {
    expect(parseSpec(SPEC).fps).toBe(30);
  });

  it.each([
    [null],
    [{ ...SPEC, outDir: "" }],
    [{ ...SPEC, fps: 0 }],
    [{ ...SPEC, script: "nope" }],
    [{ ...SPEC, url: 5 }],
    [{ ...SPEC, script: [5] }],
    [{ ...SPEC, script: [{ type: "wait" }] }],
    [{ ...SPEC, setup: [{}] }],
    [{ ...SPEC, script: [{ do: "type", text: "a", textEnv: "A", bpm: 120 }] }],
    [{ ...SPEC, script: [{ do: "type", bpm: 120 }] }],
    [{ ...SPEC, textSelectors: [1] }],
    [{ ...SPEC, colorScheme: "blue" }],
    [{ ...SPEC, colorScheme: 1 }],
  ])("rejects an invalid spec %#", (bad) => {
    expect(() => parseSpec(bad)).toThrow();
  });

  it("passes a dark colorScheme through to the launch and leaves it unset by default", async () => {
    expect(parseSpec({ ...SPEC, colorScheme: "dark" }).colorScheme).toBe("dark");
    expect(parseSpec(SPEC).colorScheme).toBeUndefined();
    const result = {
      outDir: "/tmp/out",
      manifest: { fps: 30, frames: [] },
      events: [],
      layers: [],
      clip: null,
    } as unknown as CaptureResult;
    const seen: LaunchOptions[] = [];
    await runSpec({ ...SPEC, colorScheme: "dark" }, async (options) => {
      seen.push(options);
      return result;
    });
    expect(seen[0]?.colorScheme).toBe("dark");
  });

  it("summarises the launch result", async () => {
    const result = {
      outDir: "/tmp/out",
      manifest: { fps: 30, frames: [{}, {}, {}] },
      events: [{}],
      layers: [],
      clip: null,
    } as unknown as CaptureResult;
    const summary = await runSpec(SPEC, async (options) => {
      expect(options.outDir).toBe("/tmp/out");
      return result;
    });
    expect(summary).toEqual({ outDir: "/tmp/out", fps: 30, frames: 3, events: 1, layers: 0, texts: 0 });
  });

  it("resolves textEnv from the environment at launch without touching the spec", async () => {
    const spec = {
      ...SPEC,
      setup: [{ do: "type", textEnv: "DEMO_PASSWORD", bpm: 120, secret: true }],
      script: [{ do: "type", textEnv: "DEMO_EMAIL", bpm: 120 }],
    };
    const before = JSON.stringify(spec);
    let seen: LaunchOptions | undefined;
    const result = {
      outDir: "/tmp/out",
      manifest: { fps: 30, frames: [] },
      events: [],
      layers: [],
      texts: [{}, {}],
      clip: null,
    } as unknown as CaptureResult;
    const summary = await runSpec(
      spec,
      async (options) => {
        seen = options;
        return result;
      },
      { DEMO_PASSWORD: "s3cret", DEMO_EMAIL: "demo@fixture.test" },
    );
    expect(seen?.setup).toEqual([{ do: "type", text: "s3cret", bpm: 120, secret: true }]);
    expect(seen?.script).toEqual([{ do: "type", text: "demo@fixture.test", bpm: 120 }]);
    expect(JSON.stringify(spec)).toBe(before);
    expect(JSON.stringify(summary)).not.toContain("s3cret");
    expect(summary.texts).toBe(2);
  });

  it("names a missing environment variable, never a value", () => {
    expect(() => resolveTextEnv([{ do: "type", textEnv: "MISSING_VAR", bpm: 120 }], {})).toThrow(/MISSING_VAR/);
  });
});
