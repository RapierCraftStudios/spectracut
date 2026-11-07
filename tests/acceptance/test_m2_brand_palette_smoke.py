# SPDX-License-Identifier: AGPL-3.0-or-later
"""M2 brand palette smoke (#333): an emissive #2f6bff beam on a #0b1220 void keeps its swatches.

The always-on unit cases check the beam-on-void recipe compiles through the harness, that
``tier_recipe`` pins the brand view (Standard / sRGB / look None / exposure 0), and that a
synthetic beam-on-void frame passes palette QA. The farm case (marked ``e2e``) runs only with
``SPECTRACUT_E2E=1`` and ``SPECTRACUT_FLAMENCO_URL`` (post-deploy check
``m2-brand-palette-farm-e2e``): it renders the recipe at ``Tier.FINAL`` through
:class:`FarmRecipeRenderer` and checks the background and the accent measure within Delta E 10
of their swatches.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest
from pydantic import JsonValue

from spectracut.qa.palette import DELTA_E_MAX, RGBFrame, load_rgb, palette_qa
from spectracut.spec.recipe import Recipe
from spectracut.studio.departments.shots import tier_recipe
from spectracut.studio.node import Tier
from spectracut.studio.scene_builder import compile_calls

E2E = os.environ.get("SPECTRACUT_E2E") == "1" and bool(os.environ.get("SPECTRACUT_FLAMENCO_URL"))

INK = "#0b1220"
ACCENT = "#2f6bff"
SWATCHES = {"ink": INK, "accent": ACCENT}

CALLS: list[JsonValue] = [
    {"tool": "scene.clear", "args": {}},
    {"tool": "world.set_gradient", "args": {"brand_bottom": INK, "brand_top": INK}},
    {
        "tool": "object.add_primitive",
        "args": {"kind": "cylinder", "name": "Beam", "scale": [0.08, 0.08, 1.2]},
    },
    {
        "tool": "material.apply_preset",
        "args": {"object": "Beam", "preset": "emissive", "brand_color": ACCENT},
    },
    {
        "tool": "camera.add",
        "args": {"name": "Cam", "location": [0, -6, 0], "rotation": [1.5708, 0, 0], "lens": 50},
    },
    {"tool": "camera.use", "args": {"name": "Cam"}},
]


def beam_recipe() -> Recipe:
    return compile_calls(CALLS, recipe_id="brand-palette-smoke", shot_id="brand-palette-smoke")


def test_beam_on_void_recipe_compiles_with_linear_numeric_colours() -> None:
    recipe = beam_recipe()
    gradient = next(s for s in recipe.steps if s.tool == "world.set_gradient")
    material = next(s for s in recipe.steps if s.tool == "material.apply_preset")
    assert set(gradient.args) == {"bottom", "top"}
    assert gradient.args["bottom"] == pytest.approx([0.003347, 0.006049, 0.014444], abs=1e-4)
    assert "brand_color" not in material.args
    assert material.args["emission_strength"] == 1.0


def test_final_tier_ends_on_the_brand_view() -> None:
    steps = tier_recipe(beam_recipe(), Tier.FINAL).steps
    view = {
        str(s.args["property"]): s.args["value"]
        for s in steps[-5:]
        if s.tool == "render.set_property"
    }
    assert view == {
        "view_settings.display_device": "sRGB",
        "view_settings.view_transform": "Standard",
        "view_settings.look": "None",
        "view_settings.exposure": 0.0,
    }
    assert steps[-1].tool == "render.set_output"


def test_synthetic_beam_on_void_passes_palette_qa() -> None:
    w, h = 160, 90

    def px(x: int, y: int) -> tuple[int, int, int]:
        hexes = ACCENT if abs(x - w / 2) < 6 and math.hypot(0, y - h / 2) < 35 else INK
        return (int(hexes[1:3], 16), int(hexes[3:5], 16), int(hexes[5:7], 16))

    frame = RGBFrame(w, h, tuple(px(x, y) for y in range(h) for x in range(w)))
    report = palette_qa(frame, SWATCHES)
    assert report.passed, report.lines()


@pytest.mark.e2e
@pytest.mark.skipif(not E2E, reason="needs SPECTRACUT_E2E=1 and SPECTRACUT_FLAMENCO_URL")
def test_brand_beam_renders_on_palette_on_the_farm(tmp_path: Path) -> None:
    from spectracut.cli import studio as studio_mod
    from spectracut.studio.departments.style_frame_farm import FarmRecipeRenderer
    from spectracut.studio.store import ProductionStore

    production = "brand-palette-e2e"
    home = Path(os.environ.get("SPECTRACUT_HOME") or Path.home() / ".local/share/spectracut")

    def deps() -> studio_mod.DeviceShotDeps:
        return studio_mod.default_device_shot(
            studio_mod.Production(production, ProductionStore(home))
        )

    renderer = FarmRecipeRenderer(deps, production, tmp_path / "farm", tier=Tier.FINAL)
    record, usage = renderer(beam_recipe(), tmp_path / "beam.png")

    frame = load_rgb(Path(record.path).read_bytes())
    assert frame is not None
    checks = {c.name: c for c in palette_qa(frame, SWATCHES).checks}
    background, accent = checks["background"], checks["swatch:accent"]
    assert background.delta_e is not None and background.delta_e <= DELTA_E_MAX, background
    assert accent.delta_e is not None and accent.delta_e <= DELTA_E_MAX, accent
    assert usage.gpu_minutes > 0
