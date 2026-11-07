# SPDX-License-Identifier: AGPL-3.0-or-later
"""M2 scene builder on the farm (#280): the One Beam frame built from its description.

The always-on unit test checks the control-plane modules import without bpy. The farm case
(marked ``e2e``) runs only with ``SPECTRACUT_E2E=1`` and ``SPECTRACUT_FLAMENCO_URL``
(post-deploy check ``m2-scene-builder-farm-e2e``): it builds the One Beam frame with the real
text provider and vision judge, renders every candidate through :class:`FarmRecipeRenderer`,
and checks the best frame is not flat and the build charged GPU minutes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
E2E = os.environ.get("SPECTRACUT_E2E") == "1" and bool(os.environ.get("SPECTRACUT_FLAMENCO_URL"))

FLAT_VARIANCE = 5.0
"""Luma variance (8-bit code values squared) at or below which a frame counts as flat."""

DESCRIPTION = (
    "A stack of five clear glass slabs with small gaps, centred and filling the frame, lit so a "
    "thin rim highlight draws every edge against a dark gradient. A single emissive teal beam "
    'passes through the stack. Below it, an engraved readout "ONE BEAM" is cut into a brushed '
    "metal plate and lit by a raking strip light. A 85 mm camera looks slightly down at the stack."
)


def test_scene_builder_modules_import_without_bpy() -> None:
    code = (
        "import sys\n"
        "import spectracut.studio.scene_builder, spectracut.critique.scene_verifier\n"
        "import spectracut.studio.departments.style_frame_farm\n"
        "bad = [m for m in ('bpy', 'bpy_runtime', 'spectracut_host') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=ROOT)


def _luma_variance(png: bytes) -> float:
    from spectracut.harness.imagecmp import decode_png

    image = decode_png(png)
    n, px, count = image.channels, image.pixels, image.width * image.height
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
@pytest.mark.skipif(not E2E, reason="needs SPECTRACUT_E2E=1 and SPECTRACUT_FLAMENCO_URL")
def test_one_beam_builds_on_the_farm(tmp_path: Path) -> None:
    from spectracut.cli import studio as studio_mod
    from spectracut.critique.scene_verifier import scene_critic
    from spectracut.providers.base import select_text_provider, select_vision_judge
    from spectracut.storyboard.models import ArtDirection
    from spectracut.studio.departments.style_frame_farm import FarmRecipeRenderer
    from spectracut.studio.profiles import load_profile
    from spectracut.studio.scene_builder import SceneBrief, SceneBuilder
    from spectracut.studio.store import ProductionStore

    production = "scene-builder-e2e"
    home = Path(os.environ.get("SPECTRACUT_HOME") or Path.home() / ".local/share/spectracut")

    def deps() -> studio_mod.DeviceShotDeps:
        return studio_mod.default_device_shot(
            studio_mod.Production(production, ProductionStore(home))
        )

    art = ArtDirection.model_validate(
        {
            "mood_board": [{"ref": "glass product macro", "take": "rim-lit edges"}],
            "palette": {"swatches": {"ink": "#101820", "beam": "#00c2a8", "metal": "#8a9099"}},
            "materials": ["clear glass", "brushed metal"],
            "look": {"id": "clean"},
        }
    )
    profile = load_profile("draft")
    builder = SceneBuilder(
        select_text_provider(),
        FarmRecipeRenderer(deps, production, tmp_path / "farm"),
        scene_critic(select_vision_judge(), profile=profile, description=DESCRIPTION, art=art),
        profile,
        tmp_path / "builds",
    )
    build = builder.build(SceneBrief(id="one-beam", description=DESCRIPTION, art=art))

    assert build.render is not None and build.recipe is not None
    assert _luma_variance(Path(build.render.path).read_bytes()) > FLAT_VARIANCE
    assert build.usage.gpu_minutes > 0
    assert all(it.notes for it in build.iterations)
