# SPDX-License-Identifier: AGPL-3.0-or-later
"""A product's theme: its CSS colour tokens and fonts, a proposed brand kit and kit drift.

:func:`read_product_theme` reads the front end of a :class:`ProductSource`: CSS
custom properties in ``:root`` (light) and ``.dark`` (dark) blocks of its ``*.css``
files (``oklch()``, ``#hex``, ``hsl()`` or a bare shadcn ``H S% L%`` triplet, each
turned into sRGB hex per CSS Color 4) and the ``next/font/google`` families its
root layout loads, each with the repo-relative path and line it came from.

:func:`propose_kit_values` maps the active scheme's shadcn tokens to brand-kit
swatch roles; :func:`kit_drift` lists the kit swatches no theme colour matches.

The colour maths is self-contained: :mod:`spectracut.finishing.color` imports
:mod:`spectracut.spec.brand`, so this spec module must not import finishing.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from spectracut.spec.brand import BrandKit, TokenSource
from spectracut.spec.product import ProductSource

Scheme = Literal["light", "dark"]
RGB = tuple[float, float, float]

DRIFT_DE = 2.0
"""CIELAB ΔE76 (D65) above which a kit swatch is not in the product theme."""

SKIP_DIRS = frozenset({"node_modules", ".next", "dist"})
"""Directories under the front end whose CSS is not the product's theme."""

LAYOUTS = ("app/layout.tsx", "src/app/layout.tsx", "pages/_app.tsx", "src/pages/_app.tsx")
"""Root layouts whose ``next/font`` imports name the product's fonts."""

ROLE_TOKENS: tuple[tuple[str, str], ...] = (
    ("ink", "background"),
    ("surface", "foreground"),
    ("accent", "primary"),
    ("card", "card"),
    ("muted", "muted-foreground"),
    ("border", "border"),
)
"""Brand-kit swatch role and the shadcn token it is read from."""

_BLOCK = re.compile(r"([^{}]+)\{([^{}]*)\}")
_DECL = re.compile(r"--([A-Za-z0-9_-]+)\s*:\s*([^;{}]+?)\s*(?:;|$)", re.MULTILINE)
_FUNC = re.compile(r"^(oklch|hsla?)\(\s*([^)]*)\)$", re.IGNORECASE)
_HEX = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_TRIPLET = re.compile(r"^(-?[\d.]+)(?:deg)?\s+([\d.]+)%\s+([\d.]+)%$")
_FONT_IMPORT = re.compile(r"""import\s*\{([^}]*)\}\s*from\s*['"]next/font/google['"]""")
_DARK_HTML = re.compile(r"""<html\b[^>]*className\s*=\s*[^>]*\bdark\b""")


@dataclass(frozen=True)
class ThemeToken:
    """One colour custom property of the theme."""

    name: str
    """Property name without ``--``, e.g. ``background``."""
    value: str
    """Raw CSS value, e.g. ``oklch(10% 0 0)``."""
    hex: str
    path: str
    """CSS file, relative to the product repo."""
    line: int
    scheme: Scheme


@dataclass(frozen=True)
class FontToken:
    """One ``next/font/google`` family the root layout loads."""

    family: str
    path: str
    line: int


@dataclass(frozen=True)
class ProductTheme:
    frontend: Path
    tokens: list[ThemeToken] = field(default_factory=list)
    fonts: list[FontToken] = field(default_factory=list)
    dark_forced: bool = False
    """The root layout puts ``dark`` on ``<html>``: the dark scheme is always on."""

    def scheme(self, scheme: Scheme) -> dict[str, ThemeToken]:
        """The tokens of ``scheme`` by name (a later definition wins)."""
        return {t.name: t for t in self.tokens if t.scheme == scheme}


@dataclass(frozen=True)
class ProposedKit:
    """Brand-kit values read from a theme, each with its source."""

    swatches: dict[str, str]
    families: dict[str, str]
    token_sources: dict[str, TokenSource]


# ---- colour maths ------------------------------------------------------------


def _encode(c: float) -> float:
    c = min(1.0, max(0.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _decode(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _to_hex(rgb: RGB) -> str:
    return "#" + "".join(f"{round(min(1.0, max(0.0, c)) * 255):02x}" for c in rgb)


def _oklch_to_srgb(lightness: float, chroma: float, hue: float) -> RGB:
    a = chroma * math.cos(math.radians(hue))
    b = chroma * math.sin(math.radians(hue))
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    r = 4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_
    g = -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_
    bl = -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_
    return (_encode(r), _encode(g), _encode(bl))


def _hsl_to_srgb(hue: float, sat: float, light: float) -> RGB:
    def f(n: float) -> float:
        k = (n + hue / 30) % 12
        return light - sat * min(light, 1 - light) * max(-1.0, min(k - 3, 9 - k, 1.0))

    return (f(0), f(8), f(4))


def _number(part: str, percent_scale: float) -> float:
    part = part.strip().lower().removesuffix("deg")
    if part == "none":
        return 0.0
    if part.endswith("%"):
        return float(part[:-1]) / 100 * percent_scale
    return float(part)


def css_colour_to_hex(value: str) -> str | None:
    """``value`` (``oklch()``, ``#hex``, ``hsl()`` or ``H S% L%``) as ``#rrggbb``; else ``None``."""
    value = value.strip().removesuffix("!important").strip()
    hexm = _HEX.match(value)
    if hexm:
        digits = hexm.group(1)
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        return "#" + digits.lower()
    try:
        func = _FUNC.match(value)
        if func:
            name, body = func.group(1).lower(), func.group(2)
            parts = body.split("/")[0].replace(",", " ").split()
            if len(parts) != 3:
                return None
            if name == "oklch":
                return _to_hex(
                    _oklch_to_srgb(
                        _number(parts[0], 1.0), _number(parts[1], 0.4), _number(parts[2], 1.0)
                    )
                )
            return _to_hex(
                _hsl_to_srgb(_number(parts[0], 1.0), _number(parts[1], 1.0), _number(parts[2], 1.0))
            )
        triplet = _TRIPLET.match(value)
        if triplet:
            h, s, lum = (float(g) for g in triplet.groups())
            return _to_hex(_hsl_to_srgb(h, s / 100, lum / 100))
    except ValueError:
        return None
    return None


def _lab(hex_colour: str) -> RGB:
    r, g, b = (_decode(int(hex_colour[i : i + 2], 16) / 255) for i in (1, 3, 5))
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(a: str, b: str) -> float:
    """CIELAB ΔE76 (D65) between two ``#rrggbb`` colours."""
    return math.dist(_lab(a), _lab(b))


# ---- reading the theme -------------------------------------------------------


def _rel(path: Path, repo: Path) -> str:
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return path.as_posix()


def _css_files(frontend: Path) -> list[Path]:
    return sorted(
        p
        for p in frontend.rglob("*.css")
        if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(frontend).parts)
    )


def _scheme_of(selector: str) -> Scheme | None:
    selector = selector.strip()
    if ".dark" in selector:
        return "dark"
    if ":root" in selector:
        return "light"
    return None


def _css_tokens(css: Path, repo: Path) -> list[ThemeToken]:
    text = css.read_text(encoding="utf-8", errors="replace")
    tokens: list[ThemeToken] = []
    for block in _BLOCK.finditer(text):
        scheme = _scheme_of(block.group(1))
        if scheme is None:
            continue
        start = block.start(2)
        for decl in _DECL.finditer(block.group(2)):
            hexed = css_colour_to_hex(decl.group(2))
            if hexed is None:
                continue
            line = text.count("\n", 0, start + decl.start()) + 1
            tokens.append(
                ThemeToken(
                    decl.group(1), decl.group(2).strip(), hexed, _rel(css, repo), line, scheme
                )
            )
    return tokens


def _fonts(frontend: Path, repo: Path) -> tuple[list[FontToken], bool]:
    fonts: list[FontToken] = []
    dark = False
    for rel in LAYOUTS:
        layout = frontend / rel
        if not layout.is_file():
            continue
        text = layout.read_text(encoding="utf-8", errors="replace")
        dark = dark or bool(_DARK_HTML.search(text))
        for imp in _FONT_IMPORT.finditer(text):
            line = text.count("\n", 0, imp.start()) + 1
            for name in imp.group(1).split(","):
                family = name.split(" as ")[0].strip().replace("_", " ")
                if family and family not in {f.family for f in fonts}:
                    fonts.append(FontToken(family, _rel(layout, repo), line))
    return fonts, dark


def read_product_theme(source: ProductSource) -> ProductTheme:
    """The colour tokens and fonts of ``source``'s front end.

    Raises :class:`ValueError` naming the front end when it is missing or holds no
    colour token in a ``:root`` or ``.dark`` block.
    """
    frontend = source.frontend
    if not frontend.is_dir():
        raise ValueError(f"front-end directory {frontend} does not exist")
    tokens = [t for css in _css_files(frontend) for t in _css_tokens(css, source.repo)]
    if not tokens:
        raise ValueError(f"front end {frontend} has no colour tokens in a :root or .dark block")
    fonts, dark = _fonts(frontend, source.repo)
    return ProductTheme(frontend=frontend, tokens=tokens, fonts=fonts, dark_forced=dark)


def dark_scheme(theme: ProductTheme) -> bool:
    """Whether the dark tokens apply: the layout forces ``dark`` or there are no light tokens."""
    return theme.dark_forced or not theme.scheme("light")


def active_scheme(theme: ProductTheme) -> Scheme:
    """``dark`` when :func:`dark_scheme` holds, else ``light``."""
    return "dark" if dark_scheme(theme) else "light"


def propose_kit_values(theme: ProductTheme) -> ProposedKit:
    """Swatches (ink, surface, accent, card, muted, border) and families from ``theme``."""
    tokens = theme.scheme(active_scheme(theme))
    swatches: dict[str, str] = {}
    sources: dict[str, TokenSource] = {}
    for role, name in ROLE_TOKENS:
        token = tokens.get(name)
        if token is None:
            continue
        swatches[role] = token.hex
        sources[f"palette.{role}"] = TokenSource(
            path=token.path, line=token.line, token=f"--{name}", value=token.value
        )
    families: dict[str, str] = {}
    sans = next((f for f in theme.fonts if "mono" not in f.family.lower()), None)
    mono = next((f for f in theme.fonts if "mono" in f.family.lower()), None)
    picks = [("display", sans), ("body", sans), ("mono", mono)]
    for role, font in picks:
        if font is None:
            continue
        families[role] = font.family
        sources[f"typography.{role}"] = TokenSource(
            path=font.path, line=font.line, token="next/font/google", value=font.family
        )
    return ProposedKit(swatches=swatches, families=families, token_sources=sources)


def kit_drift(kit: BrandKit, theme: ProductTheme) -> list[str]:
    """One line per kit swatch farther than :data:`DRIFT_DE` from every theme colour."""
    lines: list[str] = []
    for name, hex_ in kit.palette.swatches.items():
        swatch = hex_.lower()
        nearest = min(theme.tokens, key=lambda t: delta_e(swatch, t.hex))
        de = delta_e(swatch, nearest.hex)
        if de > DRIFT_DE:
            lines.append(
                f"swatch {name} {swatch} is not in the product theme (nearest --{nearest.name} "
                f"{nearest.hex} at {nearest.path}:{nearest.line}, dE {de:.1f})"
            )
    return lines
