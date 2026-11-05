// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Clean frame: cookie and consent banners, deny-listed third-party logos and
 * secrets never reach a frame. Cleaning runs before every frame (banners often
 * appear late), and a verify pass fails the capture if anything matched is still
 * visible.
 */

/** A logo that must not appear, matched by name (alt, aria-label, title, src path) or source. */
export interface DeniedLogo {
  name: string;
  /** Image hosts, matched on the hostname suffix. */
  hosts?: string[];
  /** Substrings of the image URL. */
  srcIncludes?: string[];
}

export interface CleanRules {
  /** Hide cookie and consent banners (default true). */
  cookieBanners?: boolean;
  /** Extra banner selectors. */
  bannerSelectors?: string[];
  /** Selectors of logos to hide. */
  logoSelectors?: string[];
  deniedLogos?: DeniedLogo[];
  /** Elements whose text is always masked. */
  secretSelectors?: string[];
  /** Extra secret patterns (masked wherever they appear in text or input values). */
  secretPatterns?: RegExp[];
}

/** What the in-page describe pass reports about a candidate element. */
export interface ElementInfo {
  /** Capture-assigned id (data-spectracut-id). */
  id: string;
  tag: string;
  idAttr: string;
  className: string;
  role: string;
  ariaLabel: string;
  title: string;
  /** Text content of fixed or sticky elements and of svg (first 300 chars), else empty. */
  text: string;
  /** Image URL: src, currentSrc, href of svg image, or a CSS background-image URL. */
  src: string;
  alt: string;
  fixed: boolean;
  visible: boolean;
  matchesBannerSelector: boolean;
  matchesLogoSelector: boolean;
}

export interface PatternSource {
  source: string;
  flags: string;
}

export interface DescribeQuery {
  bannerSelectors: string[];
  logoSelectors: string[];
}

export interface HideRequest {
  id: string;
  /** Banners are removed from layout; logos keep their space. */
  mode: "remove" | "invisible";
}

export interface RedactQuery {
  patterns: PatternSource[];
  selectors: string[];
}

/** The page operations cleaning needs. */
export interface CleanPage {
  describeElements(query: DescribeQuery): Promise<ElementInfo[]>;
  hideElements(hits: HideRequest[]): Promise<void>;
  /** Mask secrets in text nodes and input values; returns the number of masked spans. */
  redactText(query: RedactQuery): Promise<number>;
}

export class CleanError extends Error {
  constructor(readonly remaining: Array<Pick<ElementInfo, "tag" | "idAttr" | "className">>) {
    super(
      `clean frame failed: ${remaining.length} banner or logo element(s) still visible: ` +
        remaining.map((e) => `${e.tag}${e.idAttr ? `#${e.idAttr}` : ""}`).join(", "),
    );
    this.name = "CleanError";
  }
}

/** Selectors of common consent-management platforms. */
export const KNOWN_BANNER_SELECTORS: readonly string[] = [
  "#onetrust-banner-sdk",
  "#onetrust-consent-sdk",
  "#CybotCookiebotDialog",
  "#usercentrics-root",
  "#didomi-host",
  "#truste-consent-track",
  ".qc-cmp2-container",
  ".cc-window",
  ".cc-banner",
  ".osano-cm-window",
  "[aria-label*='cookie' i]",
  "[aria-label*='consent' i]",
];

const BANNER_ATTR_RE =
  /cookie|consent|gdpr|ccpa|onetrust|cookiebot|didomi|truste|usercentrics|osano|quantcast|\bcmp\b|\bcc-(?:window|banner)\b/i;
const BANNER_TEXT_RE = /\b(?:cookies?|consent)\b/i;
const BANNER_ACTION_RE = /\b(?:accept|agree|allow|reject|decline|got it|okay|ok|preferences|settings|manage)\b/i;
const IMAGE_TAGS = new Set(["img", "svg", "picture", "image", "object", "embed"]);

/** Secret shapes masked in every frame. */
export const SECRET_PATTERNS: readonly RegExp[] = [
  /\bsk_(?:live|test)_[0-9A-Za-z]{10,}/g,
  /\b(?:rk|pk)_live_[0-9A-Za-z]{10,}/g,
  /\bgh[pousr]_[0-9A-Za-z]{20,}/g,
  /\bgithub_pat_[0-9A-Za-z_]{20,}/g,
  /\bAKIA[0-9A-Z]{16}\b/g,
  /\bxox[abprs]-[0-9A-Za-z-]{10,}/g,
  /\bsk-(?:proj-|ant-)?[0-9A-Za-z_-]{20,}/g,
  /\beyJ[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}/g,
  /\bBearer\s+[0-9A-Za-z._~+/-]{16,}=*/g,
];

const SECRET_QUERY_KEY_RE = /token|secret|password|passwd|api[_-]?key|auth|session|signature|code/i;

export function isCookieBanner(e: ElementInfo, rules: CleanRules = {}): boolean {
  if (e.matchesBannerSelector) return true;
  if (rules.cookieBanners === false) return false;
  // Heuristics only apply to overlays, so an in-page "consent" checkbox survives.
  const overlay = e.fixed || e.role === "dialog" || e.role === "alertdialog";
  if (!overlay) return false;
  if (BANNER_ATTR_RE.test(`${e.idAttr} ${e.className} ${e.ariaLabel}`)) return true;
  return BANNER_TEXT_RE.test(e.text) && BANNER_ACTION_RE.test(e.text);
}

function hostOf(src: string): string {
  try {
    return new URL(src).hostname.toLowerCase();
  } catch {
    return "";
  }
}

function nameRe(name: string): RegExp {
  const escaped = name.trim().replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(?:^|[^0-9a-z])${escaped}(?:$|[^0-9a-z])`, "i");
}

export function isDeniedLogo(e: ElementInfo, rules: CleanRules = {}): boolean {
  if (e.matchesLogoSelector) return true;
  const isImage = IMAGE_TAGS.has(e.tag) || e.role === "img" || e.src !== "";
  if (!isImage) return false;
  const labels = [e.alt, e.ariaLabel, e.title, e.tag === "svg" ? e.text : ""].join(" ");
  const src = e.src.startsWith("data:") ? "" : e.src;
  const host = hostOf(src);
  let path = "";
  try {
    path = src ? decodeURIComponent(new URL(src).pathname) : "";
  } catch {
    path = src;
  }
  return (rules.deniedLogos ?? []).some((d) => {
    const re = nameRe(d.name);
    if (re.test(labels) || (path && re.test(path))) return true;
    if (host && (d.hosts ?? []).some((h) => host === h.toLowerCase() || host.endsWith(`.${h.toLowerCase()}`))) {
      return true;
    }
    return (d.srcIncludes ?? []).some((s) => s !== "" && src.includes(s));
  });
}

function allPatterns(extra: readonly RegExp[] = []): RegExp[] {
  return [...SECRET_PATTERNS, ...extra].map((re) => new RegExp(re.source, re.flags.includes("g") ? re.flags : `${re.flags}g`));
}

/** Replace every secret-shaped span with bullets of the same length. */
export function redactSecrets(text: string, extra: readonly RegExp[] = []): string {
  let out = text;
  for (const re of allPatterns(extra)) out = out.replace(re, (m) => "•".repeat(m.length));
  return out;
}

/** A URL with secret-shaped spans and credential-like query values masked. */
export function redactUrl(url: string): string {
  try {
    const u = new URL(url);
    if (u.protocol === "data:") return "data:";
    if (u.username || u.password) {
      u.username = "";
      u.password = "";
    }
    for (const key of [...u.searchParams.keys()]) {
      if (SECRET_QUERY_KEY_RE.test(key)) u.searchParams.set(key, "redacted");
    }
    return redactSecrets(u.toString());
  } catch {
    return redactSecrets(url);
  }
}

export interface CleanReport {
  banners: number;
  logos: number;
  redacted: number;
}

function query(rules: CleanRules): DescribeQuery {
  return {
    bannerSelectors: [...(rules.cookieBanners === false ? [] : KNOWN_BANNER_SELECTORS), ...(rules.bannerSelectors ?? [])],
    logoSelectors: [...(rules.logoSelectors ?? [])],
  };
}

/** Clean the current frame, then verify nothing matched is still visible. */
export async function cleanFrame(page: CleanPage, rules: CleanRules = {}): Promise<CleanReport> {
  const q = query(rules);
  const found = await page.describeElements(q);
  const hits: HideRequest[] = [];
  let banners = 0;
  let logos = 0;
  for (const e of found) {
    if (!e.visible) continue;
    if (isCookieBanner(e, rules)) {
      hits.push({ id: e.id, mode: "remove" });
      banners += 1;
    } else if (isDeniedLogo(e, rules)) {
      hits.push({ id: e.id, mode: "invisible" });
      logos += 1;
    }
  }
  if (hits.length > 0) await page.hideElements(hits);
  const redacted = await page.redactText({
    patterns: allPatterns(rules.secretPatterns).map((re) => ({ source: re.source, flags: re.flags })),
    selectors: [...(rules.secretSelectors ?? [])],
  });
  if (hits.length > 0) {
    const remaining = (await page.describeElements(q)).filter(
      (e) => e.visible && (isCookieBanner(e, rules) || isDeniedLogo(e, rules)),
    );
    if (remaining.length > 0) throw new CleanError(remaining);
  }
  return { banners, logos, redacted };
}

/** In-page source: describe candidate elements (images, fixed or sticky, banner-like). */
export const DESCRIBE_SRC = `(q) => {
  const ATTR = ${BANNER_ATTR_RE.toString()};
  const IMG = new Set(${JSON.stringify([...IMAGE_TAGS])});
  const safe = (el, sels) => sels.some((s) => { try { return el.matches(s); } catch { return false; } });
  const out = [];
  let next = Number(document.documentElement.dataset.spectracutNext || "0");
  for (const el of document.querySelectorAll("body *")) {
    const tag = el.tagName.toLowerCase();
    if (tag === "script" || tag === "style" || tag === "noscript") continue;
    const cs = getComputedStyle(el);
    const bg = cs.backgroundImage && cs.backgroundImage.startsWith("url(") ? cs.backgroundImage.slice(4, -1).replace(/^["']|["']$/g, "") : "";
    const cls = typeof el.className === "string" ? el.className : (el.getAttribute("class") || "");
    const aria = el.getAttribute("aria-label") || "";
    const fixed = cs.position === "fixed" || cs.position === "sticky";
    const bannerSel = safe(el, q.bannerSelectors);
    const logoSel = safe(el, q.logoSelectors);
    const image = IMG.has(tag) || el.getAttribute("role") === "img" || bg !== "";
    if (!(image || fixed || bannerSel || logoSel || ATTR.test(el.id + " " + cls + " " + aria))) continue;
    if (!el.dataset.spectracutId) el.dataset.spectracutId = String(next++);
    const rect = el.getBoundingClientRect();
    const visible = rect.width > 0 && rect.height > 0 &&
      el.checkVisibility({ opacityProperty: true, visibilityProperty: true });
    const src = el.currentSrc || el.getAttribute("src") || el.getAttribute("href") || el.getAttribute("data") || bg;
    out.push({
      id: el.dataset.spectracutId, tag, idAttr: el.id || "", className: cls, role: el.getAttribute("role") || "",
      ariaLabel: aria, title: el.getAttribute("title") || (tag === "svg" ? (el.querySelector("title")?.textContent || "") : ""),
      text: fixed || tag === "svg" ? (el.textContent || "").replace(/\\s+/g, " ").trim().slice(0, 300) : "",
      src: src ? new URL(src, document.baseURI).href : "", alt: el.getAttribute("alt") || "", fixed, visible,
      matchesBannerSelector: bannerSel, matchesLogoSelector: logoSel,
    });
  }
  document.documentElement.dataset.spectracutNext = String(next);
  return out;
}`;

/** In-page source: hide elements by capture id with inline !important styles. */
export const HIDE_SRC = `(hits) => {
  for (const h of hits) {
    const el = document.querySelector('[data-spectracut-id="' + h.id + '"]');
    if (!el) continue;
    if (h.mode === "remove") el.style.setProperty("display", "none", "important");
    el.style.setProperty("visibility", "hidden", "important");
    el.dataset.spectracutHidden = h.mode;
  }
}`;

/** In-page source: mask secrets in text nodes, input values and secret selectors. */
export const REDACT_SRC = `(q) => {
  const res = q.patterns.map((p) => new RegExp(p.source, p.flags));
  const bullets = (m) => "•".repeat(m.length);
  let n = 0;
  const mask = (s) => { let o = s; for (const re of res) { re.lastIndex = 0; o = o.replace(re, (m) => { n++; return bullets(m); }); } return o; };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const p = node.parentElement && node.parentElement.tagName;
    if (p === "SCRIPT" || p === "STYLE") continue;
    const v = node.nodeValue || "";
    const m = mask(v);
    if (m !== v) node.nodeValue = m;
  }
  for (const el of document.querySelectorAll("input, textarea")) {
    if (el.type === "password") continue;
    const m = mask(el.value);
    if (m !== el.value) el.value = m;
  }
  for (const sel of q.selectors) {
    let els = [];
    try { els = document.querySelectorAll(sel); } catch { continue; }
    for (const el of els) {
      if ("value" in el && typeof el.value === "string" && el.value && !/^•+$/.test(el.value)) { el.value = bullets(el.value); n++; }
      else if (el.textContent && !/^[•\\s]+$/.test(el.textContent)) { el.textContent = bullets(el.textContent); n++; }
    }
  }
  return n;
}`;
