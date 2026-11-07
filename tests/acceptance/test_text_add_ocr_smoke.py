# SPDX-License-Identifier: AGPL-3.0-or-later
"""text.add OCR smoke (#332): an API line in the host-shipped brand font reads back legibly.

The unit cases run always: the recipe validates as a ``Recipe``, every step with a harness
tool parses against its schema, every step is a runtime ``TOOL_NAMES`` op, and the text step
has no ``font`` (so the host-shipped Inter is used) and a body with punctuation, braces and
quotes. The farm case (marked ``e2e``) runs only with ``SPECTRACUT_E2E=1`` and
``SPECTRACUT_FLAMENCO_URL`` (post-deploy check ``m2-text-add-ocr-farm-e2e``): it renders one
Cycles frame on the farm, the way ``test_m2_scene_tools_smoke.py`` does, and reads it back with
tesseract.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import JsonValue
from spectracut_host.guard import runtime_tool_names

from spectracut.cli import studio as studio_mod
from spectracut.harness.tools import build_tools, get_tool
from spectracut.qa.copy_ocr import available_reader, normalise
from spectracut.queue.executors.base import JobStatus
from spectracut.queue.queue import JobQueue
from spectracut.spec.recipe import Recipe, RecipeStep
from spectracut.studio import device_shot as ds
from spectracut.studio.node import Tier
from spectracut.studio.store import ProductionStore

ROOT = Path(__file__).resolve().parents[2]
TOOLS_PY = ROOT / "host" / "bpy_runtime" / "tools.py"
E2E = os.environ.get("SPECTRACUT_E2E") == "1" and bool(os.environ.get("SPECTRACUT_FLAMENCO_URL"))

BODY = 'POST /api/v1/scrape {"max_tier": 3}'

STEPS: list[tuple[str, dict[str, JsonValue]]] = [
    ("scene.clear", {}),
    ("render.set_engine", {"engine": "cycles"}),
    ("render.set_property", {"property": "cycles.samples", "value": 32}),
    ("render.set_resolution", {"width": 1280, "height": 720}),
    (
        "world.set_gradient",
        {"bottom": [0.01, 0.01, 0.02], "top": [0.03, 0.04, 0.08], "strength": 1.0},
    ),
    (
        "object.add_slab",
        {
            "name": "plate",
            "dimensions": [11.0, 0.1, 2.4],
            "bevel_width": 0.02,
            "location": [0, 0.3, 0],
        },
    ),
    (
        "material.apply_preset",
        {"object": "plate", "preset": "matte_plastic", "color": [0.02, 0.02, 0.03]},
    ),
    (
        "text.add",
        {
            "body": BODY,
            "name": "request",
            "align": "left",
            "size": 0.45,
            "extrude": 0.02,
            "location": [-4.8, 0, 0],
            "rotation": [1.5708, 0, 0],
        },
    ),
    (
        "material.apply_preset",
        {"object": "request", "preset": "emissive", "color": [1, 1, 1], "emission_strength": 4},
    ),
    (
        "light.add",
        {
            "kind": "area",
            "name": "key",
            "energy": 300,
            "size": 6,
            "location": [0, -6, 3],
            "rotation": [1.1, 0, 0],
        },
    ),
    ("camera.add", {"name": "Camera", "location": [0, -14, 0], "rotation": [1.5708, 0, 0]}),
    ("camera.use", {"name": "Camera"}),
    ("render.set_output", {"format": "PNG"}),
]


def recipe() -> Recipe:
    steps = [RecipeStep(index=i, tool=t, args=a) for i, (t, a) in enumerate(STEPS)]
    return Recipe(
        id="text-add-ocr-smoke", shot_id="text-add-ocr-smoke", blender_version="4.5", steps=steps
    )


# ---- always-on unit tests ---------------------------------------------------------------


def test_recipe_validates_and_uses_only_runtime_tools() -> None:
    built = recipe()
    assert Recipe.model_validate_json(built.model_dump_json()) == built
    assert {s.tool for s in built.steps} <= runtime_tool_names(TOOLS_PY)
    harness = {tool.name for tool in build_tools()}
    for step in built.steps:
        if step.tool in harness:
            get_tool(step.tool).parse(step.args)


def test_text_step_uses_the_brand_font_and_keeps_the_body_verbatim() -> None:
    (text,) = [s for s in recipe().steps if s.tool == "text.add"]
    assert "font" not in text.args and text.args["align"] == "left"
    assert text.args["body"] == BODY


def test_ocr_match_is_punctuation_insensitive() -> None:
    read = normalise(BODY)
    assert normalise("POST /api/v1/scrape") in read and "max_tier" in read and "3" in read


# ---- farm e2e ---------------------------------------------------------------------------


def _render_on_farm(built: Recipe, production: str) -> Path:
    home = Path(os.environ.get("SPECTRACUT_HOME") or Path.home() / ".local/share/spectracut")
    deps = studio_mod.default_device_shot(studio_mod.Production(production, ProductionStore(home)))
    config, opts = deps.config, deps.options or ds.ExecutorOptions()
    job_id = ds.fresh_job_id(config, f"{production}-{built.content_hash[7:19]}")
    envelope = ds.envelope(job_id, built, config, opts.now(), frame_end=1)
    job_file = ds.sign_to_shared(envelope, deps.key, config.shared)
    out = config.out_dir(job_id)
    job = ds.blender_job(envelope, job_file, out, config, Tier.PREVIEW, None)
    queue = JobQueue([ds.executor(deps.client, out, config, opts, None)], now=opts.now)
    queue.submit(job)
    queue.run_until_idle(sleep=opts.sleep, jobs=[job.id])
    assert job.status is JobStatus.SUCCEEDED, f"{job.status.value}: {job.error}"
    return out


@pytest.mark.e2e
@pytest.mark.skipif(not E2E, reason="set SPECTRACUT_E2E=1 and SPECTRACUT_FLAMENCO_URL")
def test_e2e_text_add_renders_the_api_line_legibly_on_the_farm() -> None:
    reader = available_reader()
    if reader is None:
        pytest.skip("tesseract is not on PATH")
    out = _render_on_farm(recipe(), "alterlab-text-add-ocr-smoke-e2e")
    (png,) = sorted(out.glob("frame_*.png"))
    read = normalise(" ".join(box.text for box in reader.read(png.read_bytes())))
    assert normalise("POST /api/v1/scrape") in read, read
    assert "max_tier" in read and "3" in read, read
