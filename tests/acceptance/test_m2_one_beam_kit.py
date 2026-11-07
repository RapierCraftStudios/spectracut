# SPDX-License-Identifier: AGPL-3.0-or-later
"""M2 One Beam lookdev kit (#346): every kit asset loads, compiles and renders on the farm.

Unit cases run always: the kit at ``assets/kits/one-beam`` loads (provenance, pinned
HDRI and licence sidecar verified) and every asset, placed under the studio_softbox
lighting preset with a camera, compiles to a recipe of runtime ops only whose pinned
assets are exactly the kit files it stages. The farm case (marked ``e2e``) runs only
with ``SPECTRACUT_E2E=1`` and ``SPECTRACUT_FLAMENCO_URL`` (post-deploy check
``m2-one-beam-kit-farm-e2e``): it renders each asset through
:class:`FarmRecipeRenderer` with the kit staged and checks the frame is not flat.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from pydantic import JsonValue
from spectracut_host.guard import runtime_tool_names

from spectracut.harness.imagecmp import decode_png
from spectracut.harness.kit import KITS_DIR, Kit, load_kit
from spectracut.spec.recipe import Recipe
from spectracut.studio import device_shot as ds
from spectracut.studio.scene_builder import compile_calls

ROOT = Path(__file__).resolve().parents[2]
TOOLS_PY = ROOT / "host" / "bpy_runtime" / "tools.py"
E2E = os.environ.get("SPECTRACUT_E2E") == "1" and bool(os.environ.get("SPECTRACUT_FLAMENCO_URL"))
FLAT_VARIANCE = 5.0
"""Luma variance (8-bit code values squared) at or below which a frame counts as flat."""


def _kit() -> Kit:
    return load_kit(KITS_DIR / "one-beam")


def asset_recipe(kit: Kit, name: str) -> Recipe:
    """One kit asset lit by the studio_softbox preset, framed by a camera."""
    calls: list[JsonValue] = [
        {"tool": "scene.clear", "args": {}},
        {"kit": name, "args": {}},
        {"kit": "studio_softbox", "args": {}},
        {
            "tool": "camera.add",
            "args": {"name": "cam", "location": [0, -5, 1.4], "rotation": [1.4, 0, 0]},
        },
        {"tool": "camera.use", "args": {"name": "cam"}},
    ]
    return compile_calls(calls, recipe_id=f"one-beam-kit-{name}", shot_id="one-beam-kit", kit=kit)


def _assets(kit: Kit) -> list[str]:
    return [n for n, e in kit.entries.items() if e.kind == "asset"]


# ---- always-on unit tests ---------------------------------------------------------------


def test_every_kit_asset_compiles_to_runtime_ops_with_staged_pins() -> None:
    kit = _kit()
    allowed = runtime_tool_names(TOOLS_PY)
    staged = kit.files_to_stage()
    assert len(_assets(kit)) >= 5
    for name in _assets(kit):
        recipe = asset_recipe(kit, name)
        assert {s.tool for s in recipe.steps} <= allowed, name
        refs = ds.referenced_assets(recipe)
        assert refs and set(refs) <= set(staged), name
        for rel, sha in refs.items():
            digest = hashlib.sha256(staged[rel].read_bytes()).hexdigest()
            assert digest == sha.removeprefix("sha256:"), rel


# ---- farm e2e ---------------------------------------------------------------------------


def _luma_variance(png: bytes) -> float:
    image = decode_png(png)
    n, px = image.channels, image.pixels
    count = image.width * image.height
    if n < 3:
        values = [float(px[i * n]) for i in range(count)]
    else:
        values = [
            0.2126 * px[i * n] + 0.7152 * px[i * n + 1] + 0.0722 * px[i * n + 2]
            for i in range(count)
        ]
    mean = sum(values) / count
    return sum((v - mean) ** 2 for v in values) / count


@pytest.mark.e2e
@pytest.mark.skipif(not E2E, reason="set SPECTRACUT_E2E=1 and SPECTRACUT_FLAMENCO_URL")
def test_e2e_every_kit_asset_renders_on_the_farm(tmp_path: Path) -> None:
    from spectracut.cli import studio as studio_mod
    from spectracut.studio.departments.style_frame_farm import FarmRecipeRenderer
    from spectracut.studio.store import ProductionStore

    kit = _kit()
    home = Path(os.environ.get("SPECTRACUT_HOME") or Path.home() / ".local/share/spectracut")
    production = "alterlab-one-beam-kit-e2e"
    deps = studio_mod.default_device_shot(studio_mod.Production(production, ProductionStore(home)))
    render = FarmRecipeRenderer(lambda: deps, production, tmp_path / "work", kit=kit)
    for name in _assets(kit):
        out = tmp_path / "out" / f"{name}.png"
        record, usage = render(asset_recipe(kit, name), out)
        assert out.is_file() and record.path == str(out), name
        assert usage.gpu_minutes > 0, name
        assert _luma_variance(out.read_bytes()) > FLAT_VARIANCE, f"{name} rendered flat"
