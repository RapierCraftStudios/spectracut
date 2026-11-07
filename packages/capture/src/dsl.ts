// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Interaction script DSL: eased cursor paths, per-key typing on beat subdivisions,
 * clicks, drags, inertial scroll and waits, all frame-locked to the virtual clock.
 * Every interaction is emitted as a timed event for sound cues.
 */
import { framesFor, type Point } from "./clock.js";
import { redactUrl } from "./clean.js";

export type EasingName =
  | "linear"
  | "easeInQuad"
  | "easeOutQuad"
  | "easeInOutQuad"
  | "easeOutCubic"
  | "easeInOutCubic";

export const EASINGS: Record<EasingName, (t: number) => number> = {
  linear: (t) => t,
  easeInQuad: (t) => t * t,
  easeOutQuad: (t) => t * (2 - t),
  easeInOutQuad: (t) => (t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2),
  easeOutCubic: (t) => 1 - (1 - t) ** 3,
  easeInOutCubic: (t) => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2),
};

/** A point in CSS px, or the centre of the first element matching a selector. */
export type Target = Point | { selector: string };

export type Action =
  | { do: "goto"; url: string }
  | { do: "move"; to: Target; durationMs: number; easing?: EasingName }
  | { do: "click"; at?: Target; holdMs?: number }
  | { do: "drag"; from: Target; to: Target; durationMs: number; easing?: EasingName }
  | {
      do: "type";
      /** Literal text to type (exactly one of `text` and `textEnv`). */
      text?: string;
      /**
       * Name of an environment variable holding the text. The CLI resolves it into
       * `text` before launch, so the value never lands in a spec file.
       */
      textEnv?: string;
      /** Tempo the keys lock to. */
      bpm: number;
      /** Keys per beat (4 = sixteenth notes). */
      subdivision?: number;
      /** Mask the keys in exported events. */
      secret?: boolean;
    }
  | {
      do: "scroll";
      at?: Target;
      /** Initial velocity in CSS px per second (positive scrolls down). */
      velocity: number;
      /** Exponential decay rate per second. */
      friction?: number;
      /** Stop once speed falls below this, px per second. */
      minVelocity?: number;
    }
  | {
      do: "reveal";
      /** Element whose top is placed just under the `below` element. */
      selector: string;
      /** Fixed or sticky header to sit under (default "header"); the viewport top when absent. */
      below?: string;
      /** CSS px between the header's bottom and the element's top (default 16). */
      margin?: number;
    }
  | { do: "wait"; ms?: number; frames?: number };

export type EventType = "click" | "keystroke" | "scroll" | "navigation" | "drag";

export interface CaptureEvent {
  type: EventType;
  frame: number;
  timeMs: number;
  x?: number;
  y?: number;
  key?: string;
  /** Beat-grid time the keystroke was scheduled for. */
  scheduledMs?: number;
  deltaY?: number;
  url?: string;
  phase?: "start" | "move" | "end";
}

/** The page operations the interpreter needs. */
export interface InputPage {
  goto(url: string): Promise<void>;
  mouseMove(x: number, y: number): Promise<void>;
  mouseDown(): Promise<void>;
  mouseUp(): Promise<void>;
  keyPress(key: string): Promise<void>;
  keyType(text: string): Promise<void>;
  /** Wheel at a point; scrolls the nearest scrollable ancestor synchronously. */
  wheel(x: number, y: number, deltaX: number, deltaY: number): Promise<void>;
  boundingBox(selector: string): Promise<{ x: number; y: number; width: number; height: number } | null>;
  /**
   * Instantly scroll the window so `selector`'s top sits `margin` CSS px under the
   * bottom of `below` (a fixed or sticky element; else the viewport top). False when
   * no element matches `selector`.
   */
  reveal?(selector: string, below: string, margin: number): Promise<boolean>;
}

export interface ScriptContext {
  page: InputPage;
  fps: number;
  clock: { readonly frame: number; readonly timeMs: number };
  cursor: Point;
  /** Capture the current frame and advance the clock by one frame. */
  captureFrame(): Promise<void>;
  /** Record an event at the current frame. */
  emit(event: Omit<CaptureEvent, "frame" | "timeMs">): void;
  /** Called after a scripted navigation. */
  onNavigation(url: string): Promise<void>;
}

/** Times (ms since capture start) of `count` keys on the global beat grid, from `startMs`. */
export function beatTimes(startMs: number, count: number, bpm: number, subdivision = 4): number[] {
  if (!(bpm > 0) || !(subdivision > 0)) {
    throw new RangeError("bpm and subdivision must be positive");
  }
  const interval = 60000 / (bpm * subdivision);
  const first = Math.ceil(startMs / interval - 1e-9) * interval;
  return Array.from({ length: count }, (_, i) => first + i * interval);
}

/** Per-frame wheel deltas (whole CSS px) of an inertial scroll that decays exponentially. */
export function inertialDeltas(velocity: number, fps: number, friction = 4, minVelocity = 30): number[] {
  const dt = 1 / fps;
  const deltas: number[] = [];
  let v = velocity;
  const maxFrames = Math.ceil(fps * 10);
  while (Math.abs(v) >= minVelocity && deltas.length < maxFrames) {
    const d = Math.round(v * dt);
    if (d !== 0) deltas.push(d);
    v *= Math.exp(-friction * dt);
  }
  return deltas;
}

async function resolve(page: InputPage, target: Target): Promise<Point> {
  if ("selector" in target) {
    const box = await page.boundingBox(target.selector);
    if (!box) throw new Error(`no element matches ${target.selector}`);
    return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  }
  return { x: target.x, y: target.y };
}

async function path(ctx: ScriptContext, to: Point, durationMs: number, easing: EasingName): Promise<void> {
  const from = { ...ctx.cursor };
  const ease = EASINGS[easing];
  const n = framesFor(durationMs, ctx.fps);
  for (let k = 1; k <= n; k++) {
    const t = ease(k / n);
    const p = { x: from.x + (to.x - from.x) * t, y: from.y + (to.y - from.y) * t };
    await moveTo(ctx, p);
    await ctx.captureFrame();
  }
}

async function moveTo(ctx: ScriptContext, p: Point): Promise<void> {
  await ctx.page.mouseMove(p.x, p.y);
  ctx.cursor.x = p.x;
  ctx.cursor.y = p.y;
}

async function runAction(ctx: ScriptContext, action: Action): Promise<void> {
  const { page } = ctx;
  switch (action.do) {
    case "goto": {
      await page.goto(action.url);
      await ctx.onNavigation(action.url);
      return;
    }
    case "move":
      await path(ctx, await resolve(page, action.to), action.durationMs, action.easing ?? "easeInOutCubic");
      return;
    case "click": {
      if (action.at) await moveTo(ctx, await resolve(page, action.at));
      await page.mouseDown();
      ctx.emit({ type: "click", x: ctx.cursor.x, y: ctx.cursor.y });
      const hold = framesFor(action.holdMs ?? 1000 / ctx.fps, ctx.fps);
      for (let i = 0; i < hold; i++) await ctx.captureFrame();
      await page.mouseUp();
      return;
    }
    case "drag": {
      await moveTo(ctx, await resolve(page, action.from));
      await page.mouseDown();
      ctx.emit({ type: "drag", phase: "start", x: ctx.cursor.x, y: ctx.cursor.y });
      await path(ctx, await resolve(page, action.to), action.durationMs, action.easing ?? "easeInOutCubic");
      await page.mouseUp();
      ctx.emit({ type: "drag", phase: "end", x: ctx.cursor.x, y: ctx.cursor.y });
      return;
    }
    case "type": {
      if (action.text === undefined) {
        throw new Error(
          action.textEnv !== undefined
            ? `type action textEnv ${action.textEnv} was not resolved before the capture`
            : "type action needs text or textEnv",
        );
      }
      const keys = Array.from(action.text);
      const times = beatTimes(ctx.clock.timeMs, keys.length, action.bpm, action.subdivision ?? 4);
      for (const [i, ch] of keys.entries()) {
        const at = times[i] ?? ctx.clock.timeMs;
        while (ctx.clock.timeMs < at - 1e-9) await ctx.captureFrame();
        if (ch === "\n") await page.keyPress("Enter");
        else await page.keyType(ch);
        ctx.emit({
          type: "keystroke",
          key: action.secret ? "•" : ch === "\n" ? "Enter" : ch,
          scheduledMs: Math.round(at * 1000) / 1000,
        });
      }
      if (keys.length > 0) await ctx.captureFrame();
      return;
    }
    case "scroll": {
      if (action.at) await moveTo(ctx, await resolve(page, action.at));
      const deltas = inertialDeltas(action.velocity, ctx.fps, action.friction, action.minVelocity);
      for (const [i, d] of deltas.entries()) {
        await page.wheel(ctx.cursor.x, ctx.cursor.y, 0, d);
        const phase = i === 0 ? "start" : i === deltas.length - 1 ? "end" : "move";
        ctx.emit({ type: "scroll", deltaY: d, phase, x: ctx.cursor.x, y: ctx.cursor.y });
        await ctx.captureFrame();
      }
      return;
    }
    case "reveal": {
      if (!page.reveal) throw new Error("this page cannot run reveal actions");
      const found = await page.reveal(action.selector, action.below ?? "header", action.margin ?? 16);
      if (!found) throw new Error(`reveal: no element matches ${action.selector}`);
      await ctx.captureFrame();
      return;
    }
    case "wait": {
      const n = action.frames ?? framesFor(action.ms ?? 0, ctx.fps);
      for (let i = 0; i < n; i++) await ctx.captureFrame();
      return;
    }
  }
}

/** Run a script against the page, capturing frames as virtual time moves. */
export async function runScript(ctx: ScriptContext, script: readonly Action[]): Promise<void> {
  for (const action of script) await runAction(ctx, action);
}

/** Navigation event payload with secrets stripped from the URL. */
export function navigationEvent(url: string): Omit<CaptureEvent, "frame" | "timeMs"> {
  return { type: "navigation", url: redactUrl(url) };
}
