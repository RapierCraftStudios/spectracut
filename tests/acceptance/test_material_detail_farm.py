# SPDX-License-Identifier: AGPL-3.0-or-later
"""Material surface detail on the farm (#335): detail vs no detail at 1080p.

Unit cases run always: each comparison pair is a valid ``Recipe`` built only from runtime
``TOOL_NAMES`` ops, and the two recipes of a pair differ only in the detail arguments of
``material.apply_preset``. The farm case (marked ``e2e``) runs only with ``SPECTRACUT_E2E=1``
and ``SPECTRACUT_FLAMENCO_URL`` (post-deploy check ``m2-material-detail-farm-e2e``): it renders
a grazing-lit bevelled slab at 1920x1080 in Cycles with and without detail per preset (rubber
micro-texture, brushed anodised metal with worn edges, scratched brushed metal) and checks the
detailed frame carries visibly more fine, high-frequency structure over the object.

The farm submission copies the ``_Farm.render`` path of
``tests/acceptance/test_m2_scene_tools_smoke.py`` (JobQueue + Flamenco executor) as a local
helper; there is no production entry point that renders an arbitrary recipe at 1080p.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import JsonValue
from spectracut_host.guard import runtime_tool_names

from spectracut.cli import studio as studio_mod
from spectracut.harness.imagecmp import Image, decode_png
from spectracut.queue.executors.base import JobStatus
from spectracut.queue.queue import JobQueue
from spectracut.spec.recipe import Recipe, RecipeStep
from spectracut.studio import device_shot as ds
from spectracut.studio.node import Tier
from spectracut.studio.store import ProductionStore

ROOT = Path(__file__).resolve().parents[2]
TOOLS_PY = ROOT / "host" / "bpy_runtime" / "tools.py"
E2E = os.environ.get("SPECTRACUT_E2E") == "1" and bool(os.environ.get("SPECTRACUT_FLAMENCO_URL"))

WIDTH, HEIGHT = 1920, 1080
DETAIL_KEYS = {
    "bump_kind",
    "bump_strength",
    "bump_scale",
    "brushing",
    "brushing_angle",
    "scratches",
    "edge_wear",
    "seed",
}
CASES: dict[str, tuple[str, list[float], dict[str, JsonValue]]] = {
    "rubber": ("rubber", [0.05, 0.05, 0.05], {"bump_strength": 0.6, "bump_scale": 120, "seed": 1}),
    "anodised": (
        "anodised_metal",
        [0.2, 0.3, 0.6],
        {"brushing": 0.8, "brushing_angle": 0, "edge_wear": 0.6, "seed": 2},
    ),
    "brushed": (
        "brushed_metal",
        [0.75, 0.75, 0.76],
        {"brushing": 0.6, "brushing_angle": 30, "scratches": 0.7, "seed": 3},
    ),
}
HF_GAIN = 1.15
"""Detailed / plain mean neighbour-difference over the object that counts as visible detail."""
MIN_CHANGED = 0.02
"""Share of object pixels the detail must change (more than 2 code values)."""


def recipe(preset: str, color: list[float], detail: dict[str, JsonValue], rid: str) -> Recipe:
    material: dict[str, JsonValue] = {
        "object": "sample",
        "preset": preset,
        "color": list(color),
        **detail,
    }
    steps: list[tuple[str, dict[str, JsonValue]]] = [
        ("scene.clear", {}),
        ("world.set_gradient", {"bottom": [0.02, 0.02, 0.03], "top": [0.08, 0.08, 0.1]}),
        (
            "object.add_slab",
            {"name": "sample", "dimensions": [2.0, 2.0, 0.3], "bevel_width": 0.05},
        ),
        ("material.apply_preset", material),
        (
            "light.add",
            {
                "kind": "area",
                "name": "graze",
                "energy": 600,
                "size": 1.5,
                "location": [-3, -0.5, 1.2],
                "rotation": [0, -1.2, 0],
            },
        ),
        (
            "light.add",
            {"kind": "area", "name": "top", "energy": 200, "size": 3, "location": [0, 0, 4]},
        ),
        ("camera.add", {"name": "Camera", "location": [0, -2.4, 2.2], "rotation": [0.8, 0, 0]}),
        ("camera.use", {"name": "Camera"}),
        ("render.set_frame_range", {"frame_start": 1, "frame_end": 1}),
        ("render.set_engine", {"engine": "cycles"}),
        ("render.set_property", {"property": "cycles.samples", "value": 32}),
        ("render.set_resolution", {"width": WIDTH, "height": HEIGHT, "percentage": 100}),
        ("render.set_output", {"format": "PNG"}),
    ]
    built = [RecipeStep(index=i, tool=t, args=a) for i, (t, a) in enumerate(steps)]
    return Recipe(id=rid, shot_id=rid, blender_version="4.5", steps=built)


def pair(case: str) -> tuple[Recipe, Recipe]:
    preset, color, detail = CASES[case]
    plain = recipe(preset, color, {}, f"detail-{case}-plain")
    detailed = recipe(preset, color, detail, f"detail-{case}-detail")
    return plain, detailed


def luma(image: Image, x: int, y: int) -> float:
    n, px = image.channels, image.pixels
    at = (y * image.width + x) * n
    if n < 3:
        return float(px[at])
    return 0.2126 * px[at] + 0.7152 * px[at + 1] + 0.0722 * px[at + 2]


def centre_box(image: Image) -> tuple[range, range]:
    """The middle quarter of the frame, where the slab's top face sits."""
    w, h = image.width, image.height
    return range(w * 3 // 8, w * 5 // 8), range(h * 3 // 8, h * 5 // 8)


def high_frequency(image: Image) -> float:
    """Mean absolute luma difference to the right and lower neighbour over the centre box."""
    xs, ys = centre_box(image)
    total, count = 0.0, 0
    for y in ys:
        for x in xs:
            here = luma(image, x, y)
            total += abs(here - luma(image, x + 1, y)) + abs(here - luma(image, x, y + 1))
            count += 2
    return total / count


def changed_share(a: Image, b: Image, tolerance: float = 2.0) -> float:
    xs, ys = centre_box(a)
    changed = sum(1 for y in ys for x in xs if abs(luma(a, x, y) - luma(b, x, y)) > tolerance)
    return changed / (len(xs) * len(ys))


# ---- always-on unit tests ---------------------------------------------------------------


@pytest.mark.parametrize("case", sorted(CASES))
def test_pairs_use_only_runtime_tools_and_differ_only_in_detail(case: str) -> None:
    names = runtime_tool_names(TOOLS_PY)
    plain, detailed = pair(case)
    for r in (plain, detailed):
        assert {s.tool for s in r.steps} <= names
        assert Recipe.model_validate_json(r.model_dump_json()) == r
    assert len(plain.steps) == len(detailed.steps)
    for a, b in zip(plain.steps, detailed.steps, strict=True):
        if a.tool == "material.apply_preset":
            extra = set(b.args) - set(a.args)
            assert extra and extra <= DETAIL_KEYS
            assert {k: b.args[k] for k in a.args} == a.args
        else:
            assert a == b
    resolution = [s.args for s in detailed.steps if s.tool == "render.set_resolution"]
    assert resolution == [{"width": WIDTH, "height": HEIGHT, "percentage": 100}]


def _gray(width: int, height: int, value: int, stripe: bool) -> Image:
    pixels = bytearray()
    for _ in range(height):
        for x in range(width):
            v = value + (40 if stripe and x % 2 else 0)
            pixels += bytes((v, v, v))
    return Image(width=width, height=height, channels=3, pixels=bytes(pixels))


def test_high_frequency_tells_texture_from_a_flat_surface() -> None:
    flat, striped = _gray(64, 36, 100, False), _gray(64, 36, 100, True)
    assert high_frequency(flat) == 0.0
    assert high_frequency(striped) > 10
    assert changed_share(flat, striped) > 0.4


# ---- farm e2e ---------------------------------------------------------------------------


class _Farm:
    def __init__(self, production: str) -> None:
        home = Path(os.environ.get("SPECTRACUT_HOME") or Path.home() / ".local/share/spectracut")
        self.deps = studio_mod.default_device_shot(
            studio_mod.Production(production, ProductionStore(home))
        )
        self.production = production

    def render(self, recipe: Recipe, label: str) -> Image:
        config, opts = self.deps.config, self.deps.options or ds.ExecutorOptions()
        job_id = ds.fresh_job_id(config, f"{self.production}-{label}-{recipe.content_hash[7:19]}")
        envelope = ds.envelope(job_id, recipe, config, opts.now(), frame_end=1)
        job_file = ds.sign_to_shared(envelope, self.deps.key, config.shared)
        out = config.out_dir(job_id)
        job = ds.blender_job(envelope, job_file, out, config, Tier.FINAL, None)
        queue = JobQueue([ds.executor(self.deps.client, out, config, opts, None)], now=opts.now)
        queue.submit(job)
        queue.run_until_idle(sleep=opts.sleep, jobs=[job.id])
        assert job.status is JobStatus.SUCCEEDED, f"{label}: {job.status.value}: {job.error}"
        image = decode_png(sorted(out.glob("frame_*.png"))[0].read_bytes())
        assert (image.width, image.height) == (WIDTH, HEIGHT), label
        return image


@pytest.mark.e2e
@pytest.mark.skipif(not E2E, reason="set SPECTRACUT_E2E=1 and SPECTRACUT_FLAMENCO_URL")
def test_e2e_detail_renders_visible_micro_detail_at_1080p() -> None:
    farm = _Farm("alterlab-material-detail-e2e")
    for case in sorted(CASES):
        plain_recipe, detailed_recipe = pair(case)
        plain = farm.render(plain_recipe, f"{case}-plain")
        detailed = farm.render(detailed_recipe, f"{case}-detail")
        hf_plain, hf_detail = high_frequency(plain), high_frequency(detailed)
        assert hf_detail > hf_plain * HF_GAIN, f"{case}: {hf_detail:.3f} vs {hf_plain:.3f}"
        assert changed_share(plain, detailed) > MIN_CHANGED, case
