// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * End-to-end spectracut-demo run: real headless Chromium capture of a local
 * "staging" page, real ffmpeg mux, and a recording gh fake for GitHub.
 * Runs only with SPECTRACUT_E2E=1 so the plain `vitest run` needs no browser.
 */
import { execFile } from "node:child_process";
import { createServer } from "node:http";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import process from "node:process";
import { promisify } from "node:util";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { launchAndCapture } from "../../../../packages/capture/src/index.js";
import { MARKER, main } from "./demo.mjs";

const E2E = process.env.SPECTRACUT_E2E === "1";
const run = promisify(execFile);

const PAGE = `<!doctype html><html><head><style>
body { margin: 0; font: 32px/1.4 sans-serif; background: #fff; color: #111; }
#go { margin: 80px; padding: 24px 48px; font-size: 32px; }
#done { display: none; margin: 80px; font-size: 40px; }
</style></head><body>
<button id="go" onclick="document.getElementById('done').style.display='block'">Start trial</button>
<p id="done">Trial started</p>
</body></html>`;

const DEMO = { path: "/pricing", fps: 10, viewport: { width: 640, height: 360 }, script: [
  { do: "move", to: { selector: "#go" }, durationMs: 300 },
  { do: "click" },
  { do: "wait", ms: 300 },
] };

let server;
let base = "";
let work = "";

beforeAll(async () => {
  if (!E2E) return;
  work = await mkdtemp(join(tmpdir(), "spectracut-demo-e2e-"));
  server = createServer((req, res) => {
    res.writeHead(req.url === "/pricing" ? 200 : 404, { "content-type": "text/html" });
    res.end(req.url === "/pricing" ? PAGE : "not found");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});

afterAll(async () => {
  await new Promise((resolve) => (server ? server.close(resolve) : resolve()));
  if (work) await rm(work, { recursive: true, force: true });
});

describe.skipIf(!E2E)("spectracut-demo end to end", () => {
  it("captures the staging flow, muxes an MP4 and attaches it to the PR", async () => {
    const calls = [];
    let probe = null;
    const gh = async (args) => {
      calls.push(args);
      if (args[0] === "pr" && args[1] === "view") {
        return JSON.stringify({ body: "Closes #62", headRefOid: "feedface00", closingIssuesReferences: [{ number: 62 }] });
      }
      if (args[0] === "issue") return JSON.stringify({ body: "```demo\n" + JSON.stringify(DEMO) + "\n```" });
      if (args[0] === "release" && args[1] === "view") throw new Error("not found");
      if (args[0] === "release" && args[1] === "upload") {
        const { stdout } = await run("ffprobe", ["-v", "error", "-select_streams", "v:0", "-count_frames",
          "-show_entries", "stream=codec_name,pix_fmt,width,height,nb_read_frames", "-of", "json", args[3]]);
        probe = JSON.parse(stdout).streams[0];
      }
      if (args[0] === "api") return JSON.stringify([[]]);
      return "";
    };
    const res = await main(["--pr", "5", "--staging-url", base, "--repo", "o/r"], {
      gh, capture: async () => ({ launchAndCapture }), log: () => {}, workDir: work,
    });
    expect(res).toMatchObject({ status: "attached", clip: "pr-5-feedfac.mp4", comment: "created" });
    expect(res.frames).toBeGreaterThan(3);
    expect(res.bytes).toBeGreaterThan(1000);
    expect(probe).toMatchObject({ codec_name: "h264", pix_fmt: "yuv420p", width: 640, height: 360 });
    expect(Number(probe.nb_read_frames)).toBe(res.frames);
    const comment = calls.find((c) => c[0] === "pr" && c[1] === "comment");
    expect(comment[comment.indexOf("--body") + 1]).toContain(MARKER);
  }, 120_000);
});
