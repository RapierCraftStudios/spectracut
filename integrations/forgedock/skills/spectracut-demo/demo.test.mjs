// SPDX-License-Identifier: AGPL-3.0-or-later
/** Unit tests for the spectracut-demo runner: no browser, ffmpeg or gh needed. */
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  MARKER,
  RELEASE_TAG,
  VISIBLE_MARKER,
  clipName,
  commentBody,
  demoFromRecorded,
  ffmpegArgs,
  main,
  parseArgs,
  parseDemoBlock,
  redact,
  resolveScript,
  resolveUrl,
} from "./demo.mjs";

const SCRIPT = [
  { do: "move", to: { selector: "#go" }, durationMs: 400 },
  { do: "click" },
  { do: "wait", ms: 500 },
];
const BLOCK = "## Demo\n\n```demo\n" + JSON.stringify({ path: "/pricing", script: SCRIPT, fps: 24 }) + "\n```\n";

describe("parseDemoBlock", () => {
  it("reads a fenced demo JSON block", () => {
    expect(parseDemoBlock(`Intro\n${BLOCK}`)).toEqual({ path: "/pricing", script: SCRIPT, fps: 24 });
  });

  it("defaults path and fps", () => {
    const md = "```demo\n" + JSON.stringify({ script: SCRIPT }) + "\n```";
    expect(parseDemoBlock(md)).toMatchObject({ path: "/", fps: 30 });
  });

  it("reads a demo: line naming a recorded script", () => {
    expect(parseDemoBlock("Some text\ndemo: `e2e/demos/signup.json`\n")).toEqual({ scriptFile: "e2e/demos/signup.json" });
  });

  it("returns null without a demo", () => {
    expect(parseDemoBlock("no demo here\n```js\nx\n```")).toBeNull();
    expect(parseDemoBlock("")).toBeNull();
    expect(parseDemoBlock(undefined)).toBeNull();
  });

  it.each([
    ["```demo\n{not json\n```", /not valid JSON/],
    ["```demo\n[]\n```", /JSON object/],
    ["```demo\n{\"script\": []}\n```", /non-empty array/],
    ["```demo\n{\"script\": [{\"do\": \"eval\"}]}\n```", /unknown action "eval"/],
    ["```demo\n{\"script\": [{\"do\": \"wait\", \"ms\": 1}], \"fps\": 120}\n```", /fps/],
    ["```demo\n{\"script\": [{\"do\": \"wait\", \"ms\": 60000}]}\n```", /cap/],
    ["```demo\n{\"path\": \"pricing\", \"script\": [{\"do\": \"wait\"}]}\n```", /start with \//],
    ["```demo\n{\"script\": [{\"do\": \"goto\", \"url\": \"https://evil.test/\"}]}\n```", /staging path/],
    ["demo: ../secrets.json", /repo-relative/],
  ])("rejects %s", (md, err) => {
    expect(() => parseDemoBlock(md)).toThrow(err);
  });

  it("accepts a recorded script as an array or a spec", () => {
    expect(demoFromRecorded(SCRIPT)).toMatchObject({ path: "/", script: SCRIPT });
    expect(demoFromRecorded({ path: "/a", script: SCRIPT, fps: 25 })).toMatchObject({ path: "/a", fps: 25 });
  });
});

describe("helpers", () => {
  it("resolves paths on the staging origin only", () => {
    expect(resolveUrl("https://staging.example.test/app/", "/pricing?x=1")).toBe("https://staging.example.test/pricing?x=1");
    expect(() => resolveUrl("https://staging.example.test", "//other.test/x")).toThrow(/staging origin/);
    expect(resolveScript("https://s.test", [{ do: "goto", url: "/b" }, { do: "click" }])).toEqual([
      { do: "goto", url: "https://s.test/b" },
      { do: "click" },
    ]);
  });

  it("builds an H.264 mux of the frame sequence", () => {
    const args = ffmpegArgs("/w/frames", 24, "/w/out.mp4");
    expect(args).toEqual(expect.arrayContaining(["-framerate", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p", "+faststart"]));
    expect(args[args.indexOf("-i") + 1]).toBe(join("/w/frames", "%06d.png"));
    expect(args.at(-1)).toBe("/w/out.mp4");
  });

  it("names clips and comments with the marker", () => {
    expect(clipName(7, "abcdef1234")).toBe("pr-7-abcdef1.mp4");
    const body = commentBody({ clipUrl: "https://x.test/c.mp4", sha: "abcdef1234", pr: 7 });
    expect(body.startsWith(MARKER)).toBe(true);
    expect(body).toContain(VISIBLE_MARKER);
    expect(body).toContain("(https://x.test/c.mp4)");
  });

  it("parses and checks arguments", () => {
    expect(parseArgs(["--pr", "3", "--staging-url", "https://s.test", "--repo", "o/r", "--dry-run"])).toEqual({
      pr: 3, stagingUrl: "https://s.test", repo: "o/r", dryRun: true,
    });
    expect(() => parseArgs(["--staging-url", "https://s.test"])).toThrow(/--pr/);
    expect(() => parseArgs(["--pr", "3"])).toThrow(/--staging-url/);
    expect(() => parseArgs(["--pr", "3", "--staging-url", "file:///etc"])).toThrow(/http/);
    expect(() => parseArgs(["--pr", "3", "--staging-url", "https://s.test", "--x"])).toThrow(/unknown/);
  });

  it("redacts credentials and query strings", () => {
    expect(redact("https://u:p@s.test/a?token=1#f")).toBe("https://s.test/a");
  });
});

/** A recording gh fake over an in-memory PR, issue and comment list. */
function fakeGh({ prBody = "", issueBody = "", comments = [], release = false, files = {} } = {}) {
  const calls = [];
  const gh = async (args) => {
    calls.push(args);
    const [a, b] = args;
    if (a === "pr" && b === "view") {
      return JSON.stringify({ body: prBody, headRefOid: "0123456789abcdef", closingIssuesReferences: issueBody ? [{ number: 62 }] : [] });
    }
    if (a === "issue" && b === "view") return JSON.stringify({ body: issueBody });
    if (a === "release" && b === "view") {
      if (!release) throw new Error("release not found");
      return "{}";
    }
    if (a === "api" && args[1] === "-H") {
      const path = args[3].replace(/^repos\/o\/r\/contents\//, "").replace(/\?ref=.*$/, "");
      return files[path];
    }
    if (a === "api" && String(b).endsWith("/comments")) return JSON.stringify([comments]);
    return "";
  };
  return { gh, calls };
}

function fakeCapture(seen) {
  return async () => ({
    launchAndCapture: async (opts) => {
      seen.push(opts);
      await mkdir(join(opts.outDir, "frames"), { recursive: true });
      await writeFile(join(opts.outDir, "frames", "000000.png"), "png");
      await writeFile(join(opts.outDir, "frames", "000001.png"), "png");
    },
  });
}

const ffmpeg = async (args) => writeFile(args.at(-1), "mp4-bytes");
const ARGV = ["--pr", "5", "--staging-url", "https://staging.example.test", "--repo", "o/r"];

describe("main", () => {
  it("does nothing without a demo block", async () => {
    const { gh, calls } = fakeGh({ prBody: "plain PR" });
    const logs = [];
    const res = await main(ARGV, { gh, ffmpeg, capture: fakeCapture([]), log: (m) => logs.push(m) });
    expect(res).toEqual({ status: "no-demo" });
    expect(calls.map((c) => c.slice(0, 2).join(" "))).toEqual(["pr view"]);
    expect(logs.join("\n")).toMatch(/no demo block/);
  });

  it("captures from the closing issue's block, uploads and creates the comment", async () => {
    const { gh, calls } = fakeGh({ prBody: "Closes #62", issueBody: BLOCK });
    const seen = [];
    const res = await main(ARGV, { gh, ffmpeg, capture: fakeCapture(seen), log: () => {} });
    expect(res).toMatchObject({ status: "attached", clip: "pr-5-0123456.mp4", frames: 2, comment: "created" });
    expect(res.clipUrl).toBe(`https://github.com/o/r/releases/download/${RELEASE_TAG}/pr-5-0123456.mp4`);
    expect(seen[0]).toMatchObject({ url: "https://staging.example.test/pricing", fps: 24, script: SCRIPT });
    const verbs = calls.map((c) => c.slice(0, 2).join(" "));
    expect(verbs).toContain("release create");
    const upload = calls.find((c) => c[0] === "release" && c[1] === "upload");
    expect(upload).toEqual(expect.arrayContaining([RELEASE_TAG, "--clobber"]));
    const comment = calls.find((c) => c[0] === "pr" && c[1] === "comment");
    expect(comment[comment.indexOf("--body") + 1]).toContain(MARKER);
  });

  it("edits the existing marked comment on a rerun", async () => {
    const { gh, calls } = fakeGh({ prBody: BLOCK, release: true, comments: [{ id: 9, body: `${MARKER}\nold` }] });
    const res = await main(ARGV, { gh, ffmpeg, capture: fakeCapture([]), log: () => {} });
    expect(res.comment).toBe("updated");
    expect(calls.some((c) => c.includes("repos/o/r/issues/comments/9"))).toBe(true);
    expect(calls.some((c) => c[0] === "pr" && c[1] === "comment")).toBe(false);
    expect(calls.some((c) => c[1] === "create")).toBe(false);
  });

  it("reads a recorded interaction script at the PR head", async () => {
    const { gh, calls } = fakeGh({ prBody: "demo: demos/flow.json", files: { "demos/flow.json": JSON.stringify(SCRIPT) } });
    const seen = [];
    await main([...ARGV, "--dry-run"], { gh, ffmpeg, capture: fakeCapture(seen), log: () => {} });
    expect(calls.find((c) => c[0] === "api")[3]).toBe("repos/o/r/contents/demos/flow.json?ref=0123456789abcdef");
    expect(seen[0]).toMatchObject({ url: "https://staging.example.test/", script: SCRIPT });
  });

  it("stops before any GitHub write on --dry-run", async () => {
    const { gh, calls } = fakeGh({ prBody: BLOCK });
    const res = await main([...ARGV, "--dry-run"], { gh, ffmpeg, capture: fakeCapture([]), log: () => {} });
    expect(res).toMatchObject({ status: "dry-run", clip: "pr-5-0123456.mp4", frames: 2 });
    expect(calls.every((c) => c[1] === "view")).toBe(true);
  });

  it("fails when the capture writes no frames", async () => {
    const { gh } = fakeGh({ prBody: BLOCK });
    const capture = async () => ({ launchAndCapture: async (o) => mkdir(join(o.outDir, "frames"), { recursive: true }) });
    await expect(main(ARGV, { gh, ffmpeg, capture, log: () => {} })).rejects.toThrow(/no frames/);
  });
});
