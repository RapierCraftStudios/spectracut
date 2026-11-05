#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * spectracut-demo: capture a short clip of a PR's changed flow on staging and
 * attach it to the PR.
 *
 * Live-UI capture (@spectracut/capture) plus an ffmpeg mux; no Blender. The
 * flow comes from a ```demo fenced JSON block, or a `demo: <path>` line naming
 * a recorded interaction script, in the PR body or an issue the PR closes.
 *
 *   node demo.mjs --pr <N> --staging-url <URL> [--repo owner/repo] [--dry-run]
 *
 * SpectraCut never imports this file; it is a ForgeDock skill pack only.
 */
import { execFile } from "node:child_process";
import console from "node:console";
import { mkdtemp, readdir, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import process from "node:process";
import { URL, fileURLToPath } from "node:url";
import { promisify } from "node:util";

const run = promisify(execFile);

export const MARKER = "<!-- spectracut-demo -->";
export const VISIBLE_MARKER = "SpectraCut demo clip";
export const RELEASE_TAG = "spectracut-demos";
export const MAX_FPS = 60;
export const DEFAULT_FPS = 30;
export const MAX_SCRIPT_MS = 30_000;
export const ACTIONS = Object.freeze(["goto", "move", "click", "drag", "type", "scroll", "wait"]);

const FENCE = /```demo[ \t]*\r?\n([\s\S]*?)\r?\n```/;
const LINE = /^[ \t]*demo:[ \t]*`?([^\s`]+\.json)`?[ \t]*$/m;

/** Estimated scripted time in ms (scroll inertia is not counted). */
export function scriptDurationMs(script, fps) {
  let ms = 0;
  for (const a of script) {
    if (a.do === "move" || a.do === "drag") ms += a.durationMs;
    else if (a.do === "click") ms += a.holdMs ?? 0;
    else if (a.do === "type") ms += (a.text.length * 60_000) / a.bpm / (a.subdivision ?? 4);
    else if (a.do === "wait") ms += a.ms ?? ((a.frames ?? 0) * 1000) / fps;
  }
  return ms;
}

/** Validate a demo spec ({path?, script, fps?, viewport?}) and fill defaults. */
export function validateDemo(spec) {
  if (spec === null || typeof spec !== "object" || Array.isArray(spec)) {
    throw new Error("demo block must be a JSON object");
  }
  const { path = "/", script, fps = DEFAULT_FPS, viewport } = spec;
  if (typeof path !== "string" || !path.startsWith("/")) throw new Error("demo path must start with /");
  if (!Array.isArray(script) || script.length === 0) throw new Error("demo script must be a non-empty array");
  script.forEach((a, i) => {
    if (!a || !ACTIONS.includes(a.do)) throw new Error(`demo script[${i}]: unknown action ${JSON.stringify(a?.do)}`);
    if ((a.do === "move" || a.do === "drag") && !(a.durationMs >= 0)) {
      throw new Error(`demo script[${i}]: ${a.do} needs durationMs`);
    }
    if (a.do === "type" && (typeof a.text !== "string" || !(a.bpm > 0))) {
      throw new Error(`demo script[${i}]: type needs text and bpm`);
    }
    if (a.do === "goto" && (typeof a.url !== "string" || !a.url.startsWith("/"))) {
      throw new Error(`demo script[${i}]: goto url must be a staging path starting with /`);
    }
  });
  if (!Number.isFinite(fps) || fps <= 0 || fps > MAX_FPS) throw new Error(`demo fps must be in (0, ${MAX_FPS}]`);
  if (viewport !== undefined && !(viewport.width > 0 && viewport.height > 0)) {
    throw new Error("demo viewport needs positive width and height");
  }
  const ms = scriptDurationMs(script, fps);
  if (ms > MAX_SCRIPT_MS) throw new Error(`demo script runs ${Math.round(ms)} ms, over the ${MAX_SCRIPT_MS} ms cap`);
  return { path, script, fps, ...(viewport ? { viewport } : {}) };
}

/**
 * Find the demo in markdown: a ```demo fenced JSON block (preferred) or a
 * `demo: <path>.json` line. Returns a validated spec, {scriptFile}, or null.
 */
export function parseDemoBlock(markdown) {
  if (typeof markdown !== "string" || markdown === "") return null;
  const fence = FENCE.exec(markdown);
  if (fence) {
    let spec;
    try {
      spec = JSON.parse(fence[1]);
    } catch (err) {
      throw new Error(`demo block is not valid JSON: ${err.message}`);
    }
    return validateDemo(spec);
  }
  const line = LINE.exec(markdown);
  if (line) {
    const scriptFile = line[1];
    if (scriptFile.startsWith("/") || scriptFile.split("/").includes("..")) {
      throw new Error("demo script path must be repo-relative");
    }
    return { scriptFile };
  }
  return null;
}

/** A recorded script file is either an Action[] or a full demo spec. */
export function demoFromRecorded(json) {
  return validateDemo(Array.isArray(json) ? { script: json } : json);
}

/** Join a staging path onto the staging URL, refusing to leave its origin. */
export function resolveUrl(stagingUrl, path) {
  const base = new URL(stagingUrl);
  const url = new URL(path, base);
  if (url.origin !== base.origin) throw new Error(`demo path ${path} leaves the staging origin`);
  return url.href;
}

/** Rewrite goto paths in a script to absolute staging URLs. */
export function resolveScript(stagingUrl, script) {
  return script.map((a) => (a.do === "goto" ? { ...a, url: resolveUrl(stagingUrl, a.url) } : a));
}

/** ffmpeg argv muxing frames/NNNNNN.png into a web-playable H.264 MP4. */
export function ffmpegArgs(framesDir, fps, outFile) {
  return [
    "-y", "-loglevel", "error",
    "-framerate", String(fps),
    "-i", join(framesDir, "%06d.png"),
    "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    outFile,
  ];
}

export function clipName(pr, sha) {
  return `pr-${pr}-${String(sha).slice(0, 7)}.mp4`;
}

export function commentBody({ clipUrl, sha, pr }) {
  return [
    MARKER,
    `**${VISIBLE_MARKER}** for #${pr} at \`${String(sha).slice(0, 7)}\` (staging)`,
    "",
    `[Watch the clip](${clipUrl})`,
    "",
    "_Captured by the SpectraCut `spectracut-demo` skill: live-UI capture plus mux, no Blender._",
  ].join("\n");
}

export function parseArgs(argv) {
  const out = { dryRun: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--dry-run") out.dryRun = true;
    else if (a === "--pr") out.pr = Number(argv[++i]);
    else if (a === "--staging-url") out.stagingUrl = argv[++i];
    else if (a === "--repo") out.repo = argv[++i];
    else throw new Error(`unknown argument ${a}`);
  }
  if (!Number.isInteger(out.pr) || out.pr <= 0) throw new Error("--pr <number> is required");
  if (!out.stagingUrl) throw new Error("--staging-url <url> is required");
  const u = new URL(out.stagingUrl);
  if (u.protocol !== "https:" && u.protocol !== "http:") throw new Error("--staging-url must be http(s)");
  return out;
}

/** Drop credentials, query and fragment from a URL before logging it. */
export function redactUrl(url) {
  try {
    const u = new URL(url);
    return `${u.origin}${u.pathname}`;
  } catch {
    return "<url>";
  }
}

async function defaultGh(args) {
  const { stdout } = await run("gh", args, { maxBuffer: 16 * 1024 * 1024 });
  return stdout;
}

async function defaultFfmpeg(args) {
  await run("ffmpeg", args);
}

export const CAPTURE_MODULE = "@spectracut/capture";

async function defaultCapture() {
  // A variable specifier keeps bundlers and vitest from resolving the package
  // (and its unbuilt dist/) until a real run needs it.
  return import(/* @vite-ignore */ CAPTURE_MODULE);
}

async function ghJson(gh, args) {
  return JSON.parse(await gh(args));
}

/** Load the demo: PR body first, then each issue the PR closes. */
export async function findDemo(gh, repo, pr) {
  const view = await ghJson(gh, ["pr", "view", String(pr), "--repo", repo, "--json", "body,headRefOid,closingIssuesReferences"]);
  const sha = view.headRefOid;
  const bodies = [view.body ?? ""];
  for (const ref of view.closingIssuesReferences ?? []) {
    const issue = await ghJson(gh, ["issue", "view", String(ref.number), "--repo", repo, "--json", "body"]);
    bodies.push(issue.body ?? "");
  }
  for (const body of bodies) {
    const found = parseDemoBlock(body);
    if (!found) continue;
    if (found.scriptFile) {
      const raw = await gh(["api", "-H", "Accept: application/vnd.github.raw", `repos/${repo}/contents/${found.scriptFile}?ref=${sha}`]);
      return { sha, demo: demoFromRecorded(JSON.parse(raw)) };
    }
    return { sha, demo: found };
  }
  return { sha, demo: null };
}

async function ensureRelease(gh, repo) {
  try {
    await gh(["release", "view", RELEASE_TAG, "--repo", repo, "--json", "tagName"]);
  } catch {
    await gh(["release", "create", RELEASE_TAG, "--repo", repo, "--prerelease",
      "--title", "SpectraCut PR demo clips", "--notes", "PR demo clips captured by the spectracut-demo skill."]);
  }
}

async function upsertComment(gh, repo, pr, body) {
  const comments = await ghJson(gh, ["api", `repos/${repo}/issues/${pr}/comments`, "--paginate", "--slurp"]);
  const mine = comments.flat().find((c) => typeof c.body === "string" && c.body.includes(MARKER));
  if (mine) {
    await gh(["api", "-X", "PATCH", `repos/${repo}/issues/comments/${mine.id}`, "-f", `body=${body}`]);
    return "updated";
  }
  await gh(["pr", "comment", String(pr), "--repo", repo, "--body", body]);
  return "created";
}

async function repoName(gh) {
  return (await gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])).trim();
}

/**
 * Run the skill. deps: {gh(args) -> stdout, ffmpeg(args), capture() -> module
 * with launchAndCapture, log(msg), workDir?}. Returns a summary object.
 */
export async function main(argv, deps = {}) {
  const gh = deps.gh ?? defaultGh;
  const ffmpeg = deps.ffmpeg ?? defaultFfmpeg;
  const loadCapture = deps.capture ?? defaultCapture;
  const log = deps.log ?? ((m) => console.log(m));
  const args = parseArgs(argv);
  const repo = args.repo ?? (await repoName(gh));

  const { sha, demo } = await findDemo(gh, repo, args.pr);
  if (!demo) {
    log(`spectracut-demo: no demo block for ${repo}#${args.pr}; nothing attached`);
    return { status: "no-demo" };
  }

  const work = await mkdtemp(join(deps.workDir ?? tmpdir(), "spectracut-demo-"));
  try {
    const outDir = join(work, "capture");
    const url = resolveUrl(args.stagingUrl, demo.path);
    log(`spectracut-demo: capturing ${redactUrl(url)} at ${demo.fps} fps`);
    const { launchAndCapture } = await loadCapture();
    await launchAndCapture({
      url,
      script: resolveScript(args.stagingUrl, demo.script),
      fps: demo.fps,
      outDir,
      ...(demo.viewport ? { viewport: demo.viewport } : {}),
    });
    const framesDir = join(outDir, "frames");
    const frames = (await readdir(framesDir)).filter((f) => f.endsWith(".png"));
    if (frames.length === 0) throw new Error("capture produced no frames");

    const name = clipName(args.pr, sha);
    const clip = join(work, name);
    await ffmpeg(ffmpegArgs(framesDir, demo.fps, clip));
    const bytes = (await stat(clip)).size;
    log(`spectracut-demo: muxed ${frames.length} frames into ${name} (${bytes} bytes)`);

    if (args.dryRun) return { status: "dry-run", clip: name, frames: frames.length, bytes };

    await ensureRelease(gh, repo);
    await gh(["release", "upload", RELEASE_TAG, clip, "--repo", repo, "--clobber"]);
    const clipUrl = `https://github.com/${repo}/releases/download/${RELEASE_TAG}/${name}`;
    const comment = await upsertComment(gh, repo, args.pr, commentBody({ clipUrl, sha, pr: args.pr }));
    log(`spectracut-demo: ${comment} the demo comment on ${repo}#${args.pr}`);
    return { status: "attached", clip: name, clipUrl, frames: frames.length, bytes, comment };
  } finally {
    await rm(work, { recursive: true, force: true });
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main(process.argv.slice(2)).catch((err) => {
    console.error(`spectracut-demo: ${err.message}`);
    process.exitCode = 1;
  });
}
