// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it } from "vitest";

import { CAPTURE_PACKAGE, frameDurationMs } from "./index.js";

describe("@spectracut/capture", () => {
  it("exports its package name", () => {
    expect(CAPTURE_PACKAGE).toBe("@spectracut/capture");
  });

  it("computes frame duration from fps", () => {
    expect(frameDurationMs(25)).toBe(40);
  });

  it("rejects non-positive fps", () => {
    expect(() => frameDurationMs(0)).toThrow(RangeError);
  });
});
