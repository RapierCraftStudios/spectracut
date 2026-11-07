# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brand-kit values from a product's theme tokens, and kit drift (#356)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from spectracut.spec.brand import BrandKit, load_brand_kit
from spectracut.spec.product import ProductRun, ProductSource
from spectracut.spec.theme import (
    active_scheme,
    css_colour_to_hex,
    dark_scheme,
    delta_e,
    kit_drift,
    propose_kit_values,
    read_product_theme,
)

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "brands" / "alterlab" / "brand-kit.yaml"
APP = ROOT / "tests" / "studio" / "fixtures" / "themed-app"
CSS = "src/app/globals.css"
LAYOUT = "src/app/layout.tsx"


def _source(repo: Path) -> ProductSource:
    return ProductSource(id="themed", repo=repo, run=ProductRun(url="http://localhost:3000"))


def _kit(swatches: dict[str, str], **extra: object) -> BrandKit:
    base = load_brand_kit(KIT).model_dump(mode="json")
    base["palette"] = {"swatches": swatches}
    base["token_sources"] = {}
    base.update(extra)
    return BrandKit.model_validate(base)


def test_alterlab_kit_palette_comes_from_product_theme_tokens() -> None:
    kit = load_brand_kit(KIT)
    swatches = kit.palette.swatches
    for name, hex_ in swatches.items():
        source = kit.token_sources.get(f"palette.{name}")
        assert source is not None, f"swatch {name} records no source"
        assert source.path == CSS and source.token.startswith("--")
        assert css_colour_to_hex(source.value) == hex_.lower(), name
    for role in kit.typography.families:
        source = kit.token_sources.get(f"typography.{role}")
        assert source is not None, f"family {role} records no source"
        assert source.path == LAYOUT and source.value == kit.typography.families[role]
    # The product is a near-black monochrome UI with white type.
    assert swatches["ink"] == "#030303" and swatches["surface"] == "#f8f8f8"
    for old in ("#0b1220", "#f5f7fa", "#2f6bff", "#18b47b", "#f2a33a"):
        assert old not in {h.lower() for h in swatches.values()}
    assert kit.typography.families["mono"] == "JetBrains Mono"


def test_derives_kit_from_fixture_app_css_variables(tmp_path: Path) -> None:
    repo = tmp_path / "web"
    shutil.copytree(APP, repo)
    ignored = repo / "node_modules" / "x"
    ignored.mkdir(parents=True)
    (ignored / "ignored.css").write_text(".dark { --background: #00ff00; }\n", encoding="utf-8")

    theme = read_product_theme(_source(repo))
    dark = theme.scheme("dark")
    light = theme.scheme("light")
    assert {n: t.hex for n, t in dark.items()} == {
        "background": "#030303",  # oklch(10% 0 0)
        "foreground": "#f8f8f8",  # oklch(98% 0 0)
        "primary": "#ff0000",  # chromatic oklch
        "card": "#111111",  # #rgb
        "muted-foreground": "#999999",  # bare shadcn triplet
        "border": "#333333",  # hsl(a, b%, c%)
    }
    assert {n: t.hex for n, t in light.items()} == {
        "background": "#ffffff",
        "foreground": "#0f1729",
        "primary": "#2563eb",
    }
    assert (dark["background"].path, dark["background"].line) == (CSS, 14)
    assert dark["background"].value == "oklch(10% 0 0)"
    assert "#00ff00" not in {t.hex for t in theme.tokens}  # node_modules skipped
    assert "#ff00ff" not in {t.hex for t in theme.tokens}  # not a :root/.dark block
    assert [(f.family, f.path, f.line) for f in theme.fonts] == [
        ("Inter", LAYOUT, 2),
        ("JetBrains Mono", LAYOUT, 2),
    ]
    assert theme.dark_forced and dark_scheme(theme) and active_scheme(theme) == "dark"

    proposed = propose_kit_values(theme)
    assert proposed.swatches == {
        "ink": "#030303",
        "surface": "#f8f8f8",
        "accent": "#ff0000",
        "card": "#111111",
        "muted": "#999999",
        "border": "#333333",
    }
    assert proposed.families == {"display": "Inter", "body": "Inter", "mono": "JetBrains Mono"}
    ink = proposed.token_sources["palette.ink"]
    assert (ink.path, ink.line, ink.token) == (CSS, 14, "--background")


def test_light_scheme_is_proposed_when_dark_is_not_forced(tmp_path: Path) -> None:
    repo = tmp_path / "web"
    shutil.copytree(APP, repo)
    layout = repo / LAYOUT
    layout.write_text(layout.read_text().replace(' className="dark"', ""), encoding="utf-8")
    theme = read_product_theme(_source(repo))
    assert not dark_scheme(theme) and active_scheme(theme) == "light"
    assert propose_kit_values(theme).swatches["ink"] == "#ffffff"


def test_kit_drift_reports_colours_missing_from_theme() -> None:
    theme = read_product_theme(_source(APP))
    drifted = _kit({"ink": "#030303", "surface": "#f8f8f8", "accent": "#2f6bff"})
    lines = kit_drift(drifted, theme)
    assert len(lines) == 1
    assert lines[0].startswith("swatch accent #2f6bff is not in the product theme (nearest --")
    assert f"at {CSS}:" in lines[0] and "dE " in lines[0]

    proposed = propose_kit_values(theme)
    assert kit_drift(_kit(proposed.swatches), theme) == []
    # Within ΔE 2 of a token counts as present (hex rounding).
    assert kit_drift(_kit({"ink": "#040404"}), theme) == []


def test_empty_theme_is_refused(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    with pytest.raises(ValueError, match="no colour tokens"):
        read_product_theme(_source(tmp_path))
    with pytest.raises(ValueError, match="does not exist"):
        read_product_theme(_source(tmp_path / "missing"))


def test_token_sources_key_must_name_a_swatch() -> None:
    source = {"path": CSS, "line": 1, "token": "--x", "value": "#000000"}
    with pytest.raises(ValueError, match="palette.nope names no swatch or family"):
        _kit({"ink": "#000000"}, token_sources={"palette.nope": source})
    kit = _kit({"ink": "#000000"}, token_sources={"palette.ink": source})
    assert kit.token_sources["palette.ink"].line == 1
    assert load_brand_kit(KIT).model_copy().token_sources  # kit carries its sources


def test_oklch_conversion_matches_css_color_4() -> None:
    assert css_colour_to_hex("oklch(10% 0 0)") == "#030303"
    assert css_colour_to_hex("oklch(98% 0 0)") == "#f8f8f8"
    assert css_colour_to_hex("oklch(0.98 0 0)") == "#f8f8f8"
    red = css_colour_to_hex("oklch(62.8% 0.2577 29.23)")
    assert red is not None
    assert all(abs(int(red[i : i + 2], 16) - v) <= 1 for i, v in ((1, 255), (3, 0), (5, 0)))
    assert css_colour_to_hex("oklch(50% 0.2 20 / 0.5)") == css_colour_to_hex("oklch(50% 0.2 20)")
    assert css_colour_to_hex("#ABC") == "#aabbcc"
    assert css_colour_to_hex("0.5rem") is None
    assert css_colour_to_hex("var(--x)") is None
    assert delta_e("#000000", "#000000") == 0.0
    assert delta_e("#000000", "#ffffff") == pytest.approx(100.0, abs=0.01)
