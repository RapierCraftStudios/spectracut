// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it } from "vitest";

import { parseSpec, runSpec, type CaptureResult } from "../src/index.js";

const SPEC = { outDir: "/tmp/out", fps: 30, script: [{ type: "wait", ms: 100 }] };

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
  ])("rejects an invalid spec %#", (bad) => {
    expect(() => parseSpec(bad)).toThrow();
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
    expect(summary).toEqual({ outDir: "/tmp/out", fps: 30, frames: 3, events: 1, layers: 0 });
  });
});
