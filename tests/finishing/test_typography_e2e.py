# SPDX-License-Identifier: AGPL-3.0-or-later
"""E2E (#56): every type preset renders on the farm in 16:9, 9:16 and 1:1 inside the safe area.

Each preset's card is built into a recipe by :func:`type_steps` (copy gate, kit
font, whole-pixel layout) and rendered by Blender on the real Flamenco farm at
the aspect's full resolution, through the settled end of the reveal. The last
frame is decoded and every pixel that differs from the background must lie
inside the title-safe area; settled 2D type must not shimmer. Runs only with
``SPECTRACUT_E2E=1`` and ``SPECTRACUT_FLAMENCO_URL`` (post-deploy check
``typography-presets-farm-e2e``); the layout half of the assertion runs in
``test_typography.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import JsonValue

from spectracut.finishing.typography import (
    PRESET_ASPECTS,
    TITLE_SAFE,
    check_no_shimmer,
    load_type_presets,
    reveal_span,
    type_steps,
)
from spectracut.formats.safe_areas import RESOLUTIONS
from spectracut.harness.imagecmp import decode_png
from spectracut.queue.executors.base import JobStatus
from spectracut.queue.queue import JobQueue
from spectracut.queue.signing import load_private_key
from spectracut.spec.recipe import Recipe, RecipeStep
from spectracut.spec.spec import Aspect
from spectracut.studio import device_shot as ds
from spectracut.studio.node import Tier

E2E = os.environ.get("SPECTRACUT_E2E") == "1" and bool(os.environ.get("SPECTRACUT_FLAMENCO_URL"))
EDGE_PX = 2
"""Anti-aliasing allowance at the safe-area edge."""


def _scene(aspect: str) -> list[RecipeStep]:
    width, height = RESOLUTIONS[aspect]
    calls: list[tuple[str, dict[str, JsonValue]]] = [
        ("scene.clear", {}),
        ("camera.add", {"name": "Cam", "location": [0, -10, 0], "rotation": [1.5707963, 0, 0]}),
        ("render.set_engine", {"engine": "eevee"}),
        ("render.set_resolution", {"width": width, "height": height, "percentage": 100}),
        ("render.set_output", {"format": "PNG"}),
    ]
    return [RecipeStep(index=i, tool=t, args=a) for i, (t, a) in enumerate(calls)]


def _render(recipe: Recipe, frames: int, tag: str) -> list[Path]:
    from spectracut.queue.flamenco import FlamencoClient

    config = ds.DeviceShotConfig.from_env()
    home = Path(os.environ.get("SPECTRACUT_HOME") or Path.home() / ".local/share/spectracut")
    key = load_private_key(home=home)
    now = datetime.now(UTC)
    job_id = ds._fresh_job_id(config, f"typography-e2e-{tag}")
    envelope = ds._envelope(job_id, recipe, config, now, frame_end=frames)
    job_file = ds.sign_to_shared(envelope, key, config.shared)
    out = config.out_dir(job_id)
    job = ds._blender_job(envelope, job_file, out, config, Tier.PREVIEW)
    opts = ds.ExecutorOptions()
    queue = JobQueue([ds._executor(FlamencoClient(config.flamenco_url), out, config, opts)])
    queue.submit(job)
    queue.run_until_idle(sleep=opts.sleep)
    assert job.status is JobStatus.SUCCEEDED, job.error
    return sorted(out.glob("frame_*.png"))


@pytest.mark.e2e
@pytest.mark.skipif(not E2E, reason="set SPECTRACUT_E2E=1 and SPECTRACUT_FLAMENCO_URL")
@pytest.mark.parametrize("aspect", PRESET_ASPECTS)
def test_e2e_every_preset_renders_inside_the_safe_area(aspect: Aspect) -> None:
    from test_typography import _brief, _cards, _kit  # type: ignore[import-not-found]

    presets = load_type_presets()
    width, height = RESOLUTIONS[aspect]
    x0, x1 = TITLE_SAFE.left * width - EDGE_PX, (1 - TITLE_SAFE.right) * width + EDGE_PX
    y0, y1 = TITLE_SAFE.top * height - EDGE_PX, (1 - TITLE_SAFE.bottom) * height + EDGE_PX
    for card in _cards():
        preset = presets[card.preset]
        card = card.model_copy(update={"start_frame": 1})
        base = _scene(aspect)
        steps = base + type_steps([card], presets, _kit(), _brief(), aspect, start_index=len(base))
        recipe = Recipe(
            id=f"type-{preset.id}",
            shot_id=f"type-{preset.id}",
            blender_version=ds.BLENDER_VERSION,
            steps=steps,
        )
        last = 1 + reveal_span(card, preset) + 2
        files = _render(recipe, last, f"{preset.id}-{aspect.replace(':', 'x')}")
        assert files, f"{preset.id} {aspect}: no frames"
        image = decode_png(files[-1].read_bytes())
        assert (image.width, image.height) == (width, height)
        ch, px = image.channels, image.pixels
        background = px[0:ch]
        lit = 0
        for y in range(height):
            row = y * width * ch
            for x in range(width):
                at = row + x * ch
                if px[at : at + ch] != background:
                    lit += 1
                    assert x0 <= x <= x1 and y0 <= y <= y1, (
                        f"{preset.id} {aspect}: text pixel ({x}, {y}) is outside the safe area"
                    )
        assert lit > 0, f"{preset.id} {aspect}: no text rendered"
        if preset.dimension == "2d":
            check_no_shimmer([f.read_bytes() for f in files[-2:]])
