# SPDX-License-Identifier: AGPL-3.0-or-later
"""Looks: the preset library, kit resolution, and the split between render and finishing.

A :class:`~spectracut.spec.brand.Look` touches two places:

* :func:`render_rules`: motion blur and the depth-of-field rule. They are
  render-time settings, so they belong to the render recipe and its key.
* :func:`finish_params`: grade (CDL or LUT), lens, bloom and grain. They are
  applied by a separate Blender compositor job (``finish.apply_look``) on the
  render's linear EXR frames, so they key only the downstream ``finish`` node.

Two Looks with equal render rules therefore share one render: switching the
finish node's Look re-runs only ``finish`` (spike § Finishing: a grade change
re-runs only compositing). ``qa_thresholds`` enter neither; the QA gate
(:mod:`spectracut.qa.look`) reads them.

The library is ``looks/*.yaml`` in the repo, loaded by path like brand kits.
This module is pure Python; Blender runs only on render hosts.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import JsonValue, ValidationError

from spectracut.finishing.color import ColorPipeline
from spectracut.spec.brand import BrandKit, Look
from spectracut.spec.recipe import Recipe, RecipeStep
from spectracut.studio.graph import RunContext, RunResult

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from spectracut.queue.executors.flamenco import FlamencoApi
    from spectracut.studio.device_shot import DeviceShotConfig, ExecutorOptions

LOOK_SUFFIX = ".yaml"
FRAMES_SUBDIR = "frames"
LUT_SUBDIR = "lut"
EXR_SUFFIX = ".exr"


# ---- the library ----------------------------------------------------------------


def load_look(path: Path) -> Look:
    """Read one ``looks/<id>.yaml`` as a :class:`Look`; errors name the file."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read look {path}: {exc.strerror or exc}") from None
    except yaml.YAMLError as exc:
        raise ValueError(f"look {path} is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ValueError(f"look {path} must be a YAML mapping")
    try:
        look = Look.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "<root>"
        raise ValueError(
            f"look {path} is invalid: {exc.error_count()} error(s), first: {loc}: {first['msg']}"
        ) from None
    if path.stem != look.id:
        raise ValueError(f"look {path} declares id {look.id!r}; the file must be {look.id}.yaml")
    return look


def load_looks(directory: Path) -> dict[str, Look]:
    """Every ``*.yaml`` Look in ``directory`` by id; a duplicate id is refused."""
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError(f"looks directory {directory} does not exist")
    looks: dict[str, Look] = {}
    for path in sorted(directory.glob(f"*{LOOK_SUFFIX}")):
        look = load_look(path)
        if look.id in looks:
            raise ValueError(f"look {path} repeats id {look.id!r}")
        looks[look.id] = look
    return looks


def resolve_look(kit: BrandKit, look_id: str | None = None) -> Look:
    """The kit's Look ``look_id``, or its ``default_look_id`` when ``None``."""
    wanted = kit.default_look_id if look_id is None else look_id
    for look in kit.looks:
        if look.id == wanted:
            return look
    known = ", ".join(sorted(look.id for look in kit.looks))
    raise ValueError(f"brand kit {kit.id!r} has no look {wanted!r}; its looks are: {known}")


# ---- render rules vs finishing ----------------------------------------------------


def render_rules(look: Look, start: int = 0) -> list[RecipeStep]:
    """The Look's render-time rules: motion blur on/off, and DoF off when the Look forbids it.

    A Look that allows depth of field leaves the shot's camera DoF as the
    recipe set it; one that forbids it turns it off.
    """
    steps = [
        RecipeStep(
            index=start,
            tool="render.set_property",
            args={"property": "render.use_motion_blur", "value": look.motion_blur},
        )
    ]
    if not look.depth_of_field:
        steps.append(RecipeStep(index=start + 1, tool="camera.set_dof", args={"enabled": False}))
    return steps


def _rules_signature(look: Look) -> list[JsonValue]:
    return [{"tool": s.tool, "args": s.args} for s in render_rules(look)]


def finish_params(look: Look) -> dict[str, JsonValue]:
    """Grade, lens, bloom and grain plus the Look's id and version; nothing render-time."""
    grade: dict[str, JsonValue] = {}
    if look.grade.cdl is not None:
        cdl = look.grade.cdl
        grade["cdl"] = {
            "slope": list(cdl.slope),
            "offset": list(cdl.offset),
            "power": list(cdl.power),
            "saturation": cdl.saturation,
        }
    if look.grade.lut is not None:
        grade["lut"] = look.grade.lut
    lens = look.lens
    return {
        "look_id": look.id,
        "look_version": look.version,
        "grade": grade,
        "lens": {
            "chromatic_aberration": lens.chromatic_aberration,
            "vignette": lens.vignette,
            "distortion": lens.distortion,
            "halation": lens.halation,
        },
        "bloom": look.bloom,
        "grain": look.grain,
    }


def finish_steps(
    look: Look,
    pipeline: ColorPipeline,
    frames: list[str],
    *,
    lut: Mapping[str, str] | None = None,
    start: int = 0,
) -> list[RecipeStep]:
    """8-bit PNG output, the pipeline's display/view/look, then ``finish.apply_look``."""
    if look.grade.lut is not None and lut is None:
        raise ValueError(
            f"look {look.id!r} grades with LUT {look.grade.lut!r}; pass its staged file"
        )
    if not frames:
        raise ValueError("finish needs at least one input frame")
    steps = [
        RecipeStep(
            index=start, tool="render.set_output", args={"format": "PNG", "color_depth": "8"}
        )
    ]
    steps += pipeline.render_steps(start=start + 1)
    args: dict[str, JsonValue] = {**finish_params(look), "frames": list(frames)}
    if lut is not None:
        args["lut"] = {"path": lut["path"], "sha256": lut["sha256"]}
    steps.append(RecipeStep(index=start + len(steps), tool="finish.apply_look", args=args))
    return steps


# ---- the finish department ----------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _render_input(ctx: RunContext) -> dict[str, Any]:
    for nid in ctx.spec.inputs:
        value = ctx.inputs.get(nid)
        if isinstance(value, dict) and isinstance(value.get("output_dir"), str):
            if isinstance(value.get("frames"), dict):
                return value
    raise ValueError(f"finish node {ctx.spec.id!r} has no input render with frames")


def _lut_file(look: Look, looks_dir: Path | None) -> Path:
    assert look.grade.lut is not None
    path = Path(look.grade.lut)
    if not path.is_absolute():
        if looks_dir is None:
            raise ValueError(f"look {look.id!r} names LUT {look.grade.lut!r} but no looks dir")
        path = looks_dir / path
    if not path.is_file() or path.suffix.lower() != ".cube":
        raise ValueError(f"look {look.id!r} LUT {path} is not a .cube file")
    return path


def finish_runner(
    ctx: RunContext,
    *,
    look: Look,
    pipeline: ColorPipeline,
    production_id: str,
    config: DeviceShotConfig,
    client: FlamencoApi,
    key: Ed25519PrivateKey,
    now: Callable[[], datetime],
    options: ExecutorOptions,
    looks_dir: Path | None = None,
) -> RunResult:
    """Build a ``finish`` node: one Flamenco compositor job grading the input render's EXRs.

    The render's ``frame_*.exr`` files (and the LUT, if the Look has one) are
    staged content-addressed under ``<shared>/assets/<sha>/`` and reach the host
    only through its confined assets dir. Raises :class:`ValueError` before
    anything is submitted when the input has no EXR frames or its render rules
    differ from ``look``'s (that Look needs its own render).
    """
    from spectracut.queue.queue import JobQueue
    from spectracut.studio import device_shot as ds

    render = _render_input(ctx)
    rules = render.get("render_rules")
    if rules is not None and rules != _rules_signature(look):
        raise ValueError(
            f"look {look.id!r} changes render rules (motion blur / depth of field); "
            f"its render node needs look_id {look.id!r} too"
        )
    source_dir = Path(render["output_dir"])
    names = sorted(n for n in render["frames"] if str(n).endswith(EXR_SUFFIX))
    if not names:
        raise ValueError(f"finish input {source_dir} has no linear EXR frames")
    base = Recipe.model_validate(render["recipe"])
    pipeline.require_blender(base.blender_version)
    tmp = Path(tempfile.mkdtemp(prefix="spectracut-finish-"))
    try:
        referenced: dict[str, str] = {}
        frames: list[str] = []
        for name in names:
            rel = f"{FRAMES_SUBDIR}/{name}"
            dest = tmp / FRAMES_SUBDIR / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_dir / name, dest)
            referenced[rel] = _sha256(dest)
            frames.append(rel)
        lut: dict[str, str] | None = None
        if look.grade.lut is not None:
            src = _lut_file(look, looks_dir)
            rel = f"{LUT_SUBDIR}/{src.name}"
            dest = tmp / LUT_SUBDIR / src.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            referenced[rel] = _sha256(dest)
            lut = {"path": rel, "sha256": referenced[rel]}
        assets_sha = ds.stage_assets(tmp, config.shared, referenced)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assets_dir = ds.storage_assets_dir(assets_sha)
    resolution = [s for s in base.steps if s.tool == "render.set_resolution"][-1:]
    calls = [RecipeStep(index=0, tool="scene.clear", args={})]
    calls += [s.model_copy(update={"index": 1}) for s in resolution]
    calls += finish_steps(look, pipeline, frames, lut=lut, start=len(calls))
    recipe = Recipe(
        id=f"{base.shot_id}-finish-{look.id}",
        shot_id=base.shot_id,
        blender_version=base.blender_version,
        steps=calls,
    )
    job_id = ds._fresh_job_id(config, f"{production_id}-{ctx.spec.id}-{recipe.content_hash[7:19]}")
    envelope = ds._envelope(job_id, recipe, config, now(), frame_end=len(frames))
    job_file = ds.sign_to_shared(envelope, key, config.shared)
    out = config.out_dir(job_id)
    job = ds._blender_job(envelope, job_file, out, config, ctx.tier, assets_dir)
    queue = JobQueue([ds._executor(client, out, config, options, assets_dir)], now=now)
    result = queue.as_runner(lambda _ctx: job, sleep=options.sleep)(ctx)
    masters = ds.frame_hashes(out)
    if not masters:
        raise RuntimeError(f"finish in {out} produced no frame_* files")
    flamenco = result.output if isinstance(result.output, dict) else {}
    output: dict[str, JsonValue] = {
        "look_id": look.id,
        "look_version": look.version,
        "render_recipe_hash": render.get("recipe_hash"),
        "frames": dict(masters),
        "frame_files": dict(ds.frame_file_hashes(out)),
        "output_dir": str(out),
        "job_id": job_id,
        "flamenco_job_id": flamenco.get("flamenco_job_id"),
        "assets_sha": assets_sha,
        "playback": dict(pipeline.playback()),
    }
    return RunResult(output=output, usage=result.usage)
