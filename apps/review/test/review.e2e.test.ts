// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * End-to-end review page (#35) in real headless Chromium (playwright-core)
 * against a real `spectracut serve`. Runs only with SPECTRACUT_E2E=1 so the plain
 * `vitest run` needs no browser, uv or Python.
 *
 * Seeds a temp SPECTRACUT_HOME with production "e2e": a `board` node waiting at a
 * human gate (media: the committed 24 fps VP8/Opus clip) and a `render` node that
 * reads it. Then steps frames, checks timecode and audio sync, pins a note,
 * approves in the page, and asserts the render node is unblocked in the graph.
 */
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium, type Browser, type Page } from "playwright-core";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const E2E = process.env.SPECTRACUT_E2E === "1";
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "..", "..", "..");
const CLIP = join(HERE, "fixtures", "clip.webm");
const SEED = join(REPO, "tests", "api", "review_e2e_seed.py");
const FPS = 24;

let home = "";
let server: ChildProcess | undefined;
let browser: Browser | undefined;
let page: Page;
let base = "";

function freePort(): Promise<number> {
  return new Promise((done, fail) => {
    const srv = createServer();
    srv.unref();
    srv.on("error", fail);
    srv.listen(0, "127.0.0.1", () => {
      const address = srv.address();
      const port = typeof address === "object" && address ? address.port : 0;
      srv.close(() => done(port));
    });
  });
}

function seed(...args: string[]): { status: number | null; out: string } {
  const res = spawnSync("uv", ["run", "--quiet", "python", SEED, home, ...args], {
    cwd: REPO,
    encoding: "utf8",
  });
  return { status: res.status, out: `${res.stdout}${res.stderr}` };
}

async function waitForServer(url: string, ms: number): Promise<void> {
  const until = Date.now() + ms;
  let last = "";
  while (Date.now() < until) {
    try {
      const res = await fetch(url);
      if (res.ok) return;
      last = `HTTP ${res.status}`;
    } catch (err) {
      last = String(err);
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`spectracut serve did not come up at ${url}: ${last}`);
}

type Player = { frame: string; timecode: string; video: number; audio: number; paused: boolean };

async function player(): Promise<Player> {
  return page.evaluate(() => {
    const v = document.getElementById("video") as HTMLVideoElement;
    const a = document.getElementById("audio") as HTMLAudioElement;
    return {
      frame: document.getElementById("frame")?.textContent ?? "",
      timecode: document.getElementById("timecode")?.textContent ?? "",
      video: v.currentTime,
      audio: a.currentTime,
      paused: v.paused,
    };
  });
}

beforeAll(async () => {
  if (!E2E) return;
  home = await mkdtemp(join(tmpdir(), "spectracut-review-e2e-"));
  const seeded = seed(CLIP);
  if (seeded.status !== 0) throw new Error(`seed failed: ${seeded.out}`);
  const port = await freePort();
  base = `http://127.0.0.1:${port}`;
  server = spawn("uv", ["run", "--quiet", "spectracut", "serve", "--home", home, "--port", String(port)], {
    cwd: REPO,
    stdio: ["ignore", "inherit", "inherit"],
  });
  await waitForServer(`${base}/v1/review/queue`, 60_000);
  browser = await chromium.launch({ headless: true, args: ["--autoplay-policy=no-user-gesture-required"] });
  page = await browser.newPage();
}, 120_000);

afterAll(async () => {
  await browser?.close();
  if (server && server.exitCode === null) {
    server.kill("SIGTERM");
    await new Promise((r) => server?.once("exit", r));
  }
  if (home) await rm(home, { recursive: true, force: true });
});

describe.skipIf(!E2E)("local review page against spectracut serve", () => {
  it("lists the gate and steps frame by frame with synced audio and timecode", async () => {
    await page.goto(`${base}/`);
    const item = page.locator('[data-testid="queue-item"]');
    await expect.poll(() => item.count()).toBe(1);
    expect(await item.first().getAttribute("data-node")).toBe("board");
    await page.waitForSelector('[data-testid="video"][data-ready="1"]', { timeout: 30_000 });
    expect(await page.locator('[data-testid="fps"]').textContent()).toBe(`${FPS} fps`);

    let p = await player();
    expect(p.frame).toBe("0");
    expect(p.timecode).toBe("00:00:00:00");

    for (let i = 0; i < 3; i++) await page.click('[data-testid="next"]');
    await expect.poll(async () => (await player()).timecode).toBe("00:00:00:03");
    p = await player();
    expect(p.frame).toBe("3");
    expect(Math.abs(p.video - 3.5 / FPS)).toBeLessThan(0.5 / FPS);
    expect(Math.abs(p.audio - p.video)).toBeLessThan(1 / FPS);

    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("ArrowLeft");
    await expect.poll(async () => (await player()).frame).toBe("2");

    // 25 frames at 24 fps crosses one second of timecode.
    for (let i = 0; i < 23; i++) await page.click('[data-testid="next"]');
    await expect.poll(async () => (await player()).timecode).toBe("00:00:01:01");

    // Play with audio, then pause: both clocks move together and land on a frame.
    await page.click('[data-testid="play"]');
    await expect.poll(async () => (await player()).video, { timeout: 10_000 }).toBeGreaterThan(25.5 / FPS + 0.1);
    const playing = await player();
    expect(playing.paused).toBe(false);
    expect(Math.abs(playing.audio - playing.video)).toBeLessThan(0.15);
    await page.click('[data-testid="play"]');
    await expect.poll(async () => (await player()).paused).toBe(true);
    p = await player();
    expect(Math.abs(p.audio - p.video)).toBeLessThan(1 / FPS);
    const frame = Number(p.frame);
    expect(Math.abs(p.video - (frame + 0.5) / FPS)).toBeLessThan(0.5 / FPS);
    expect(p.timecode).toBe(`00:00:0${Math.floor(frame / FPS)}:${String(frame % FPS).padStart(2, "0")}`);
  });

  it("pins a note to the current frame", async () => {
    for (let i = 0; i < 60; i++) await page.click('[data-testid="prev"]');
    for (let i = 0; i < 3; i++) await page.click('[data-testid="next"]');
    await expect.poll(async () => (await player()).frame).toBe("3");
    await page.fill('[data-testid="note-text"]', "Logo reads small here");
    await page.click('[data-testid="add-note"]');
    const note = page.locator('[data-testid="note"]');
    await expect.poll(() => note.count()).toBe(1);
    expect(await note.first().textContent()).toContain("00:00:00:03 (f3) Logo reads small here");
  });

  it("approving in the page unblocks the gate's dependants in the production graph", async () => {
    await page.click('[data-testid="approve"]');
    await expect.poll(() => page.locator('[data-testid="queue-item"]').count()).toBe(0);
    await expect.poll(() => page.locator('[data-testid="status"]').textContent()).toBe("board: clean");
    const checked = seed("--check");
    expect(checked.out).toContain("render unblocked");
    expect(checked.status).toBe(0);
  });
});
