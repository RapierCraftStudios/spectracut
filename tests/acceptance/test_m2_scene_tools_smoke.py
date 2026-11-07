# SPDX-License-Identifier: AGPL-3.0-or-later
"""M2 scene-tools smoke (#287, #279 part 4): the "One Beam" recipe built only from harness tools.

Unit cases run always: the fixture ``fixtures/one_beam_smoke.json`` validates as a ``Recipe``,
uses only runtime ``TOOL_NAMES`` ops and holds every ingredient (five glass slabs, an emissive
beam, engraved text, a light rig, a world gradient, camera and object keyframes over a frame
range). The farm case (marked ``e2e``) runs only with ``SPECTRACUT_E2E=1`` and
``SPECTRACUT_FLAMENCO_URL`` (post-deploy check ``m2-scene-tools-smoke-farm-e2e``): it renders the
recipe on the farm and checks the image sequence, a non-flat frame, moving frames, and per-object
coverage through the cryptomatte pass (comp jobs that grade only one object's matte).

The engraving is a ``hide_render`` cutter in the plate, so it has no matte of its own; its
coverage is the plate pixels the cutout removes (render with vs without ``text.add``).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from pydantic import JsonValue
from spectracut_host.guard import runtime_tool_names

from spectracut.cli import studio as studio_mod
from spectracut.finishing import comp
from spectracut.harness.imagecmp import Image, decode_png
from spectracut.queue.executors.base import JobStatus
from spectracut.queue.queue import JobQueue
from spectracut.spec.recipe import Recipe, RecipeStep
from spectracut.studio import device_shot as ds
from spectracut.studio.node import Tier
from spectracut.studio.store import ProductionStore

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "one_beam_smoke.json"
TOOLS_PY = ROOT / "host" / "bpy_runtime" / "tools.py"
E2E = os.environ.get("SPECTRACUT_E2E") == "1" and bool(os.environ.get("SPECTRACUT_FLAMENCO_URL"))

FLAT_VARIANCE = 5.0
"""Luma variance (8-bit code values squared) at or below which a frame counts as flat."""
SLABS = [f"slab_{i}" for i in range(1, 6)]
MASKED = [*SLABS, "beam", "readout_plate"]
GLASS = {"clear_glass", "frosted_glass"}


def load_fixture() -> Recipe:
    return Recipe.model_validate_json(FIXTURE.read_text())


def luma_variance(image: Image) -> float:
    n = image.channels
    px = image.pixels
    count = image.width * image.height
    values: list[float]
    if n < 3:
        values = [float(px[i * n]) for i in range(count)]
    else:
        values = [
            0.2126 * px[i * n] + 0.7152 * px[i * n + 1] + 0.0722 * px[i * n + 2]
            for i in range(count)
        ]
    mean = sum(values) / count
    return sum((v - mean) ** 2 for v in values) / count


def changed_pixels(a: Image, b: Image, tolerance: int = 1) -> int:
    assert (a.width, a.height, a.channels) == (b.width, b.height, b.channels)
    n, total = a.channels, 0
    for i in range(a.width * a.height):
        at = i * n
        if any(abs(a.pixels[at + c] - b.pixels[at + c]) > tolerance for c in range(min(n, 3))):
            total += 1
    return total


# ---- always-on unit tests ---------------------------------------------------------------


def test_fixture_validates_and_uses_only_runtime_tools() -> None:
    recipe = load_fixture()
    names = runtime_tool_names(TOOLS_PY)
    assert {s.tool for s in recipe.steps} <= names


def test_fixture_has_every_ingredient() -> None:
    steps = load_fixture().steps

    def by_tool(tool: str) -> list[RecipeStep]:
        return [s for s in steps if s.tool == tool]

    slabs = {s.args["name"] for s in by_tool("object.add_slab")}
    assert set(SLABS) <= slabs
    presets = {s.args["object"]: s.args["preset"] for s in by_tool("material.apply_preset")}
    assert all(presets[name] in GLASS for name in SLABS)
    beam = [s for s in by_tool("curve.add") if s.args["name"] == "beam"]
    assert beam and presets["beam"] == "emissive"
    cutouts = [s for s in by_tool("text.add") if s.args.get("mode") == "cutout"]
    assert cutouts and cutouts[0].args["plate"] in slabs
    assert len(by_tool("light.add")) >= 2 and by_tool("world.set_gradient")
    keyed = {s.args["object"] for s in by_tool("object.keyframe")}
    assert "Camera" in keyed and keyed - {"Camera"}
    frame_range = by_tool("render.set_frame_range")[0].args
    start, end = frame_range["frame_start"], frame_range["frame_end"]
    assert isinstance(start, int) and isinstance(end, int) and end > start


def test_flat_frame_is_below_the_variance_threshold_and_a_gradient_above() -> None:
    flat = Image(4, 4, 3, bytes([30, 40, 90] * 16))
    ramp = Image(4, 4, 3, bytes(v for i in range(16) for v in (i * 16,) * 3))
    assert luma_variance(flat) <= FLAT_VARIANCE < luma_variance(ramp)
    assert changed_pixels(flat, flat) == 0 and changed_pixels(flat, ramp) > 0


# ---- farm e2e ---------------------------------------------------------------------------


def _steps(recipe: Recipe, steps: list[tuple[str, dict[str, JsonValue]]], rid: str) -> Recipe:
    built = [RecipeStep(index=i, tool=t, args=a) for i, (t, a) in enumerate(steps)]
    return Recipe(
        id=rid,
        shot_id=recipe.shot_id,
        blender_version=recipe.blender_version,
        steps=built,
    )


class _Farm:
    def __init__(self, production: str) -> None:
        home = Path(os.environ.get("SPECTRACUT_HOME") or Path.home() / ".local/share/spectracut")
        self.deps = studio_mod.default_device_shot(
            studio_mod.Production(production, ProductionStore(home))
        )
        self.production = production

    def render(self, recipe: Recipe, frame_end: int, assets_dir: str | None, label: str) -> Path:
        config, opts = self.deps.config, self.deps.options or ds.ExecutorOptions()
        job_id = ds.fresh_job_id(config, f"{self.production}-{label}-{recipe.content_hash[7:19]}")
        envelope = ds.envelope(job_id, recipe, config, opts.now(), frame_end=frame_end)
        job_file = ds.sign_to_shared(envelope, self.deps.key, config.shared)
        out = config.out_dir(job_id)
        job = ds.blender_job(envelope, job_file, out, config, Tier.PREVIEW, assets_dir)
        queue = JobQueue(
            [ds.executor(self.deps.client, out, config, opts, assets_dir)], now=opts.now
        )
        queue.submit(job)
        queue.run_until_idle(sleep=opts.sleep, jobs=[job.id])
        assert job.status is JobStatus.SUCCEEDED, f"{label}: {job.status.value}: {job.error}"
        return out

    def comp_png(self, base: Recipe, exr: Path, matte: str, grade: bool, label: str) -> Image:
        config = self.deps.config
        tmp = Path(tempfile.mkdtemp(prefix="spectracut-smoke-"))
        try:
            rel = f"{comp.FRAMES_SUBDIR}/{exr.name}"
            dest = tmp / rel
            dest.parent.mkdir(parents=True)
            shutil.copyfile(exr, dest)
            sha = hashlib.sha256(dest.read_bytes()).hexdigest()
            assets = ds.storage_assets_dir(ds.stage_assets(tmp, config.shared, {rel: sha}))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        ops: list[JsonValue] = [{"op": "screen_grade", "slope": [0.5, 0.5, 0.5]}] if grade else []
        graph: dict[str, JsonValue] = {
            "comp_id": "smoke",
            "comp_version": 1,
            "screen_matte": matte,
            "ops": ops,
            "frames": [rel],
        }
        recipe = _steps(
            base,
            [
                ("scene.clear", {}),
                ("render.set_resolution", {"width": 640, "height": 360}),
                ("comp.apply_graph", graph),
                ("render.set_output", {"format": "PNG"}),
            ],
            f"smoke-comp-{label}",
        )
        out = self.render(recipe, 1, assets, f"comp-{label}")
        return decode_png(sorted(out.glob("frame_*.png"))[0].read_bytes())


def _pass_recipe(base: Recipe, *, text: bool, rid: str) -> Recipe:
    """The fixture as one cryptomatte multilayer EXR frame (Cycles), with or without the text."""
    steps: list[tuple[str, dict[str, JsonValue]]] = []
    for s in base.steps:
        if s.tool == "text.add" and not text:
            continue
        if s.tool == "render.set_frame_range":
            steps.append((s.tool, {"frame_start": 1, "frame_end": 1}))
        elif s.tool == "render.set_engine":
            steps += [(s.tool, {"engine": "cycles"})]
            steps.append(("render.set_property", {"property": "cycles.samples", "value": 16}))
        elif s.tool == "render.set_output":
            steps += [(p.tool, p.args) for p in comp.passes_steps()]
        else:
            steps.append((s.tool, s.args))
    return _steps(base, steps, rid)


def _coverage(farm: _Farm, base: Recipe, out: Path, matte: str, label: str) -> int:
    exr = sorted(out.glob("frame_*.exr"))[0]
    plain = farm.comp_png(base, exr, matte, False, f"{label}-base")
    graded = farm.comp_png(base, exr, matte, True, f"{label}-{matte}")
    return changed_pixels(plain, graded)


@pytest.mark.e2e
@pytest.mark.skipif(not E2E, reason="set SPECTRACUT_E2E=1 and SPECTRACUT_FLAMENCO_URL")
def test_e2e_one_beam_smoke_renders_on_the_farm() -> None:
    base = load_fixture()
    frame_range = [s for s in base.steps if s.tool == "render.set_frame_range"][0].args
    start, end = frame_range["frame_start"], frame_range["frame_end"]
    assert isinstance(start, int) and isinstance(end, int)
    frames = end - start + 1
    farm = _Farm("alterlab-scene-tools-smoke-e2e")

    out = farm.render(base, frames, None, "sequence")
    pngs = sorted(out.glob("frame_*.png"))
    assert len(pngs) == frames
    first, last = (decode_png(p.read_bytes()) for p in (pngs[0], pngs[-1]))
    assert luma_variance(first) > FLAT_VARIANCE
    assert changed_pixels(first, last) > 0

    with_text = farm.render(_pass_recipe(base, text=True, rid="smoke-pass"), 1, None, "pass")
    for name in MASKED:
        assert _coverage(farm, base, with_text, name, "text") > 0, f"{name} covers no pixels"
    without = farm.render(_pass_recipe(base, text=False, rid="smoke-notext"), 1, None, "notext")
    plate_with = _coverage(farm, base, with_text, "readout_plate", "text")
    plate_without = _coverage(farm, base, without, "readout_plate", "notext")
    assert plate_without > plate_with, "the engraved text removed no plate pixels"
