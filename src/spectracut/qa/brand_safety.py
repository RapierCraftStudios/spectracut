# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brand safety: third-party URLs, wordmarks and logos, OEM marks, cookie banners, secrets.

Text findings come from OCR lines (:class:`~spectracut.qa.copy_ocr.TextBox`);
logos are found by matching reference mark images against the frame. No real
third-party brand may appear unless the brief owner supplied and licensed it,
which the caller expresses through ``allowed`` names and domains.

A leaked secret is never copied into the report: findings carry a redacted
preview only.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from spectracut.qa.common import BBox, Gray, Integral, QACheck, downscale, resample
from spectracut.qa.copy_ocr import TextBox

THIRD_PARTY_MARKS: frozenset[str] = frozenset(
    {
        # OEM and device marks
        "apple", "macbook", "imac", "iphone", "ipad", "samsung", "lenovo", "thinkpad",
        "dell", "asus", "acer", "huawei", "xiaomi", "nvidia", "geforce", "intel",
        "microsoft", "playstation", "xbox", "nintendo", "logitech",
        # platform and service wordmarks
        "google", "android", "facebook", "instagram", "tiktok", "youtube", "twitter",
        "linkedin", "netflix", "amazon", "spotify", "slack", "salesforce", "shopify",
        "stripe", "github", "openai", "chatgpt", "figma", "notion", "dropbox", "zoom",
    }
)  # fmt: skip
"""Wordmarks that must not appear in a frame unless the brief owner licenses them."""

COOKIE_PHRASES: tuple[str, ...] = (
    "accept all cookies",
    "accept cookies",
    "we use cookies",
    "this website uses cookies",
    "this site uses cookies",
    "cookie settings",
    "cookie preferences",
    "manage cookies",
    "reject all",
    "cookie policy",
)

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai-key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe-key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("bearer-token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

_TLDS = (
    "com|net|org|io|dev|app|ai|co|gov|edu|info|biz|me|tv|us|uk|de|fr|eu|cloud|xyz|site|so|gg"
)
URL_RE = re.compile(
    rf"(?:https?://)?(?:www\.)?((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:{_TLDS}))\b(?:/\S*)?",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-z0-9]+")


def redact(secret: str) -> str:
    """A preview that identifies a secret's kind without revealing it."""
    return secret[:4] + "…" if len(secret) > 4 else "…"


def _domain_allowed(domain: str, allowed: Iterable[str]) -> bool:
    domain = domain.lower()
    return any(domain == a or domain.endswith("." + a) for a in allowed)


def check_text(
    frames: Sequence[Sequence[TextBox]],
    *,
    allowed_domains: Iterable[str] = (),
    allowed_marks: Iterable[str] = (),
    forbidden_words: Iterable[str] = (),
) -> list[QACheck]:
    """Third-party URLs and wordmarks, cookie banners, leaked secrets and forbidden words."""
    domains = {d.lower().lstrip(".") for d in allowed_domains}
    marks_ok = {m.lower() for m in allowed_marks}
    forbidden = [w.lower() for w in forbidden_words]
    out: list[QACheck] = []

    def fail(name: str, detail: str, frame: int, box: TextBox) -> None:
        out.append(QACheck(name=name, status="fail", detail=detail, frame=frame, bbox=box.box))

    for i, boxes in enumerate(frames):
        for box in boxes:
            text = box.text
            lowered = text.lower()
            secret_hit = False
            for kind, pattern in SECRET_PATTERNS:
                match = pattern.search(text)
                if match:
                    secret_hit = True
                    fail("brand_safety.secret", f"{kind} leaked: {redact(match.group(0))}", i, box)
                    break
            if secret_hit:
                continue
            for match in URL_RE.finditer(text):
                domain = match.group(1).lower()
                if not _domain_allowed(domain, domains):
                    fail("brand_safety.third_party_url", f"third-party URL {domain}", i, box)
            words = set(_WORD_RE.findall(lowered))
            for mark in sorted((words & THIRD_PARTY_MARKS) - marks_ok):
                fail("brand_safety.wordmark", f"third-party mark {mark!r}", i, box)
            for phrase in COOKIE_PHRASES:
                if phrase in lowered:
                    fail("brand_safety.cookie_banner", f"cookie banner text {phrase!r}", i, box)
                    break
            for word in forbidden:
                if re.search(rf"\b{re.escape(word)}\b", lowered):
                    fail("brand_safety.forbidden_word", f"forbidden word {word!r}", i, box)
    if not out:
        out.append(QACheck(name="brand_safety.text", status="pass"))
    return out


SIGNATURE = 8
"""Marks and candidate windows are compared as ``SIGNATURE`` x ``SIGNATURE`` area means."""

MATCH_THRESHOLD = 0.9
"""Normalised cross-correlation at or above which a window is a mark."""

MARK_WIDTHS = (0.06, 0.08, 0.1, 0.12, 0.15, 0.2)
"""Mark widths, as fractions of the frame width, searched for."""


@dataclass(frozen=True)
class ReferenceMark:
    """A third-party logo to look for, as a greyscale image."""

    name: str
    image: Gray

    @property
    def aspect(self) -> float:
        return self.image.height / self.image.width


def _normalised(values: Sequence[float]) -> tuple[list[float], float]:
    m = sum(values) / len(values)
    centred = [v - m for v in values]
    norm = math.sqrt(sum(c * c for c in centred))
    return centred, norm


def _window_signature(table: Integral, x: int, y: int, w: int, h: int) -> list[float]:
    out: list[float] = []
    for j in range(SIGNATURE):
        y0, y1 = y + j * h // SIGNATURE, y + (j + 1) * h // SIGNATURE
        for i in range(SIGNATURE):
            x0, x1 = x + i * w // SIGNATURE, x + (i + 1) * w // SIGNATURE
            out.append(table.mean(x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)))
    return out


def detect_marks(
    frame: Gray,
    marks: Sequence[ReferenceMark],
    *,
    threshold: float = MATCH_THRESHOLD,
    widths: Sequence[float] = MARK_WIDTHS,
) -> list[tuple[str, float, BBox]]:
    """``(mark name, score, box)`` of the best match of each mark found in ``frame``."""
    work = downscale(frame)
    table = Integral(work)
    found: list[tuple[str, float, BBox]] = []
    for mark in marks:
        ref, ref_norm = _normalised(resample(mark.image, SIGNATURE, SIGNATURE).pixels)
        if ref_norm < 1e-6:
            continue
        best: tuple[float, BBox] | None = None
        for fraction in widths:
            w = max(SIGNATURE, round(fraction * work.width))
            h = max(SIGNATURE, round(w * mark.aspect))
            if w > work.width or h > work.height:
                continue
            for y in range(0, work.height - h + 1):
                for x in range(0, work.width - w + 1):
                    sig, norm = _normalised(_window_signature(table, x, y, w, h))
                    if norm < 0.05:
                        continue
                    score = sum(a * b for a, b in zip(ref, sig, strict=True)) / (ref_norm * norm)
                    if score >= threshold and (best is None or score > best[0]):
                        best = (
                            score,
                            BBox(
                                x=x / work.width,
                                y=y / work.height,
                                w=w / work.width,
                                h=h / work.height,
                            ),
                        )
        if best is not None:
            found.append((mark.name, round(best[0], 4), best[1]))
    return found


def check_logos(frame: Gray, marks: Sequence[ReferenceMark], *, frame_index: int) -> list[QACheck]:
    """A failure per third-party mark seen in the frame; two or more is a logo strip."""
    hits = detect_marks(frame, marks)
    strip = len(hits) >= 2
    return [
        QACheck(
            name="brand_safety.logo",
            status="fail",
            value=score,
            limit=MATCH_THRESHOLD,
            detail=f"third-party logo {name!r}" + (" in a logo strip" if strip else ""),
            frame=frame_index,
            bbox=box,
        )
        for name, score, box in hits
    ]
