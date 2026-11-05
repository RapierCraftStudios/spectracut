# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-shot compositing graphs: the comp library, render passes, and the ``comp`` stage.

A :class:`CompGraph` is a declarative, ordered list of compositing ops drawn
from a fixed allowlist with bounded parameters:

* screen ops apply only inside the device-screen cryptomatte matte
  (``screen_matte``, the ``device_screen`` object by default):
  ``screen_grade`` (ASC CDL), ``ui_sharpen`` and ``screen_glow``;
* scene ops always apply outside it: ``glints``, ``light_wrap`` and ``lens``.

They build on SpectraCut's finishing node groups (glare, lens distortion with
dispersion, the CDL grade) and run in Blender's compositor on a render host
through the ``comp.apply_graph`` runtime op. That op reads the render's
multilayer EXR (AOVs, depth, motion vectors and cryptomatte, enabled by
:func:`passes_steps`). The ``comp`` node keys only on :func:`comp_params`, so
changing a comp graph re-runs ``comp`` and ``finish``, both compositor-only jobs,
and never the render (spike § Production pipeline).

UI legibility always wins. Scene ops cannot touch the screen, and
:func:`legibility_check` runs the screen ops' pure model (:func:`screen_transfer`)
over synthetic UI text. :func:`load_comp` refuses a graph that would drop the
screen below WCAG AA or lose more than :data:`MAX_CONTRAST_LOSS` of its source
contrast.

The library is ``comps/*.yaml`` in the repo, loaded by path like ``looks/``.
This module is pure Python; Blender runs only on render hosts.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import yaml
from pydantic import Field, JsonValue, ValidationError

from spectracut.qa.legibility import AA_RATIO, LegibilityReport, linear_legibility
from spectracut.spec._base import Slug, StrictModel, Version
from spectracut.spec.recipe import Recipe, RecipeStep
from spectracut.studio.graph import RunContext, RunResult

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from spectracut.queue.executors.flamenco import FlamencoApi
    from spectracut.studio.device_shot import DeviceShotConfig, ExecutorOptions

COMP_SUFFIX = ".yaml"
FRAMES_SUBDIR = "frames"
EXR_SUFFIX = ".exr"
DEFAULT_SCREEN_MATTE = "device_screen"
"""The device screen object (``build_device_recipe``) and so its cryptomatte name."""
RENDER_PASSES: tuple[str, ...] = (
    "combined",
    "z",
    "vector",
    "normal",
    "diffuse",
    "glossy",
    "emission",
    "cryptomatte_object",
)
"""AOVs, depth, motion vectors and the object cryptomatte the comp stage reads."""
CRYPTOMATTE_PASS = "cryptomatte_object"
MAX_CONTRAST_LOSS = 0.10
"""A comp graph may lower the screen's text contrast by at most 10% of the source's."""

_LUMA = (0.2126, 0.7152, 0.0722)

# ---- the graph model -------------------------------------------------------------

Slope = Annotated[float, Field(ge=0.5, le=2.0)]
Offset = Annotated[float, Field(ge=-0.2, le=0.2)]
Power = Annotated[float, Field(ge=0.5, le=2.0)]


class ScreenGrade(StrictModel):
    """Per-shot ASC CDL on the screen only (through the cryptomatte matte)."""

    op: Literal["screen_grade"] = "screen_grade"
    slope: tuple[Slope, Slope, Slope] = (1.0, 1.0, 1.0)
    offset: tuple[Offset, Offset, Offset] = (0.0, 0.0, 0.0)
    power: tuple[Power, Power, Power] = (1.0, 1.0, 1.0)
    saturation: Annotated[float, Field(ge=0.0, le=2.0)] = 1.0


class UiSharpen(StrictModel):
    """Sharpen the screen's UI: a 3x3 sharpen at ``amount``, ``radius`` passes (0 = off)."""

    op: Literal["ui_sharpen"] = "ui_sharpen"
    amount: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    radius: Annotated[int, Field(ge=0, le=3)] = 1


class ScreenGlow(StrictModel):
    """Fog glow of the screen's highlights above ``threshold``, added back at ``strength``."""

    op: Literal["screen_glow"] = "screen_glow"
    strength: Annotated[float, Field(ge=0.0, le=0.5)] = 0.0
    size: Annotated[int, Field(ge=1, le=9)] = 6
    threshold: Annotated[float, Field(ge=0.0, le=2.0)] = 0.9


class Glints(StrictModel):
    """Streak glints on scene highlights (outside the screen)."""

    op: Literal["glints"] = "glints"
    threshold: Annotated[float, Field(ge=0.0, le=10.0)] = 1.0
    strength: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    streaks: Annotated[int, Field(ge=2, le=8)] = 4


class LightWrap(StrictModel):
    """Blurred plate screened over the screen matte's outer edge."""

    op: Literal["light_wrap"] = "light_wrap"
    amount: Annotated[float, Field(ge=0.0, le=0.5)] = 0.0
    blur: Annotated[float, Field(ge=0.0, le=64.0)] = 8.0


class Lens(StrictModel):
    """Lens distortion and dispersion (chromatic aberration) outside the screen."""

    op: Literal["lens"] = "lens"
    distortion: Annotated[float, Field(ge=-0.1, le=0.1)] = 0.0
    dispersion: Annotated[float, Field(ge=0.0, le=0.05)] = 0.0


CompOp = Annotated[
    ScreenGrade | UiSharpen | ScreenGlow | Glints | LightWrap | Lens, Field(discriminator="op")
]
SCREEN_OPS = frozenset({"screen_grade", "ui_sharpen", "screen_glow"})
SCENE_OPS = frozenset({"glints", "light_wrap", "lens"})


class CompGraph(StrictModel):
    """A shot's declarative compositing graph: ordered allowlisted ops, at most one of each."""

    id: Slug
    version: Version = 1
    screen_matte: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,63}$")] = DEFAULT_SCREEN_MATTE
    ops: list[CompOp] = Field(default_factory=list, max_length=16)

    def model_post_init(self, _context: Any) -> None:
        names = [op.op for op in self.ops]
        repeated = sorted({n for n in names if names.count(n) > 1})
        if repeated:
            raise ValueError(f"comp graph {self.id!r} repeats op(s) {repeated}")


# ---- the library -----------------------------------------------------------------


def _parse(path: Path) -> CompGraph:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read comp graph {path}: {exc.strerror or exc}") from None
    except yaml.YAMLError as exc:
        raise ValueError(f"comp graph {path} is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ValueError(f"comp graph {path} must be a YAML mapping")
    try:
        return CompGraph.model_validate(raw)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            first = exc.errors()[0]
            loc = ".".join(str(p) for p in first["loc"]) or "<root>"
            detail = f"{exc.error_count()} error(s), first: {loc}: {first['msg']}"
        else:
            detail = str(exc)
        raise ValueError(f"comp graph {path} is invalid: {detail}") from None


def load_comp(path: Path) -> CompGraph:
    """Read one ``comps/<id>.yaml``; errors (YAML, schema, legibility) name the file."""
    path = Path(path)
    graph = _parse(path)
    if path.stem != graph.id:
        raise ValueError(f"comp graph {path} declares id {graph.id!r}; the file must be {graph.id}.yaml")
    for case, (before, after) in legibility_check(graph).items():
        if not _legible(before, after):
            raise ValueError(
                f"comp graph {path} makes the screen illegible on {case} UI: contrast "
                f"{before.contrast:.2f} -> {after.contrast:.2f} (needs >= {AA_RATIO} and at "
                f"most {MAX_CONTRAST_LOSS:.0%} loss)"
            )
    return graph


def load_comps(directory: Path) -> dict[str, CompGraph]:
    """Every ``*.yaml`` comp graph in ``directory`` by id; a duplicate id is refused."""
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError(f"comps directory {directory} does not exist")
    comps: dict[str, CompGraph] = {}
    for path in sorted(directory.glob(f"*{COMP_SUFFIX}")):
        graph = load_comp(path)
        if graph.id in comps:
            raise ValueError(f"comp graph {path} repeats id {graph.id!r}")
        comps[graph.id] = graph
    return comps


def default_comps_dir() -> Path:
    """The repo's ``comps/`` library."""
    return Path(__file__).resolve().parents[3] / "comps"


# ---- recipe steps and the node key -------------------------------------------------


def comp_params(graph: CompGraph) -> dict[str, JsonValue]:
    """The comp graph as canonical JSON: what the ``comp.apply_graph`` step (and node) keys on."""
    ops: list[JsonValue] = [json.loads(op.model_dump_json()) for op in graph.ops]
    return {
        "comp_id": graph.id,
        "comp_version": graph.version,
        "screen_matte": graph.screen_matte,
        "ops": ops,
    }


def comp_hash(graph: CompGraph) -> str:
    """``sha256:`` of :func:`comp_params` in canonical (sorted, compact) JSON."""
    text = json.dumps(comp_params(graph), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def passes_steps(start: int = 0) -> list[RecipeStep]:
    """Enable :data:`RENDER_PASSES` and write 32-bit float multilayer EXR.

    Full float keeps the cryptomatte ID hashes exact (half float corrupts them).
    """
    return [
        RecipeStep(index=start, tool="render.set_passes", args={"passes": list(RENDER_PASSES)}),
        RecipeStep(
            index=start + 1,
            tool="render.set_output",
            args={"format": "OPEN_EXR_MULTILAYER", "color_depth": "32"},
        ),
    ]


def comp_steps(graph: CompGraph, frames: list[str], start: int = 0) -> list[RecipeStep]:
    """Single-layer linear EXR output (what ``finish`` reads), then ``comp.apply_graph``."""
    if not frames:
        raise ValueError("comp needs at least one input frame")
    args: dict[str, JsonValue] = {**comp_params(graph), "frames": list(frames)}
    return [
        RecipeStep(
            index=start, tool="render.set_output", args={"format": "OPEN_EXR", "color_depth": "16"}
        ),
        RecipeStep(index=start + 1, tool="comp.apply_graph", args=args),
    ]


# ---- the screen model (for the legibility check) -------------------------------------

Pixel = tuple[float, float, float]
Rows = list[list[Pixel]]


def _grade(px: Pixel, op: ScreenGrade) -> Pixel:
    c = [max(v * s + o, 0.0) ** p for v, s, o, p in zip(px, op.slope, op.offset, op.power, strict=True)]
    luma = sum(w * v for w, v in zip(_LUMA, c, strict=True))
    r, g, b = (max(luma + op.saturation * (v - luma), 0.0) for v in c)
    return (r, g, b)


def _sharpen_once(rows: Rows, amount: float) -> Rows:
    h, w = len(rows), len(rows[0])
    out: Rows = []
    for y in range(h):
        row: list[Pixel] = []
        for x in range(w):
            ring = [
                rows[min(max(y + dy, 0), h - 1)][min(max(x + dx, 0), w - 1)]
                for dy in (-1, 0, 1)
                for dx in (-1, 0, 1)
                if dy or dx
            ]
            px = rows[y][x]
            sharp = [9 * px[c] - sum(n[c] for n in ring) for c in range(3)]
            r, g, b = (max(px[c] + amount * (sharp[c] - px[c]), 0.0) for c in range(3))
            row.append((r, g, b))
        out.append(row)
    return out


def _glow(rows: Rows, op: ScreenGlow) -> Rows:
    n = sum(len(r) for r in rows)
    lift = [
        op.strength * sum(max(px[c] - op.threshold, 0.0) for row in rows for px in row) / n
        for c in range(3)
    ]
    return [[(px[0] + lift[0], px[1] + lift[1], px[2] + lift[2]) for px in row] for row in rows]


def screen_transfer(graph: CompGraph, rgb_rows: Sequence[Sequence[Sequence[float]]]) -> Rows:
    """What ``graph`` does to scene-linear RGB inside the screen matte (a pure model).

    Scene ops are masked off the screen by the cryptomatte, so only the screen
    ops act: the CDL grade per pixel, a 3x3 sharpen mixed at ``amount`` per
    ``radius`` pass, and fog glow as an even lift by the mean highlight above
    ``threshold`` (a glow wider than the screen's text).
    """
    rows: Rows = [[(float(p[0]), float(p[1]), float(p[2])) for p in row] for row in rgb_rows]
    if not rows or not rows[0] or any(len(r) != len(rows[0]) for r in rows):
        raise ValueError("screen_transfer needs a non-empty rectangle of RGB pixels")
    for op in graph.ops:
        if isinstance(op, ScreenGrade):
            rows = [[_grade(px, op) for px in row] for row in rows]
        elif isinstance(op, UiSharpen):
            for _ in range(op.radius if op.amount > 0 else 0):
                rows = _sharpen_once(rows, op.amount)
        elif isinstance(op, ScreenGlow):
            rows = _glow(rows, op)
    return rows


def ui_text_patch(text: float, background: float, size: int = 24, period: int = 6) -> Rows:
    """A synthetic UI patch: 2 px glyph strokes of grey ``text`` on grey ``background``."""
    return [
        [(text, text, text) if x % period < 2 and 2 <= y < size - 2 else (background,) * 3
         for x in range(size)]
        for y in range(size)
    ]


UI_CASES: dict[str, tuple[float, float]] = {
    "dark-on-light": (0.012, 0.85),
    "light-on-dark": (0.85, 0.012),
    "bright-screen": (0.02, 1.3),
}
"""Scene-linear text and background greys of the synthetic screens."""


def legibility_check(graph: CompGraph) -> dict[str, tuple[LegibilityReport, LegibilityReport]]:
    """Source vs comped legibility of each :data:`UI_CASES` screen under ``graph``."""
    out: dict[str, tuple[LegibilityReport, LegibilityReport]] = {}
    for case, (text, background) in UI_CASES.items():
        patch = ui_text_patch(text, background)
        out[case] = (linear_legibility(patch), linear_legibility(screen_transfer(graph, patch)))
    return out


def _legible(before: LegibilityReport, after: LegibilityReport) -> bool:
    return after.passed and after.contrast >= (1.0 - MAX_CONTRAST_LOSS) * before.contrast


# ---- the comp department -------------------------------------------------------------


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
    raise ValueError(f"comp node {ctx.spec.id!r} has no input render with frames")


def comp_runner(
    ctx: RunContext,
    *,
    graph: CompGraph,
    production_id: str,
    config: DeviceShotConfig,
    client: FlamencoApi,
    key: Ed25519PrivateKey,
    now: Callable[[], datetime],
    options: ExecutorOptions,
) -> RunResult:
    """Build a ``comp`` node: one Flamenco compositor-only job running ``graph`` on the render.

    The render's multilayer ``frame_*.exr`` files are staged content-addressed
    under ``<shared>/assets/<sha>/`` and reach the host only through its
    confined assets dir. The output has the render-output shape (``frames``,
    ``output_dir``, the render's ``recipe``/``recipe_hash``/``render_rules``), so
    ``finish`` consumes it as it would the render. Raises :class:`ValueError`
    before anything is submitted when the input has no EXR frames or was not
    rendered with passes (no cryptomatte to matte the screen with).
    """
    from spectracut.queue.queue import JobQueue
    from spectracut.studio import device_shot as ds

    render = _render_input(ctx)
    source_dir = Path(render["output_dir"])
    names = sorted(n for n in render["frames"] if str(n).endswith(EXR_SUFFIX))
    if not names:
        raise ValueError(f"comp input {source_dir} has no EXR frames")
    passes = render.get("render_passes")
    if not isinstance(passes, list) or CRYPTOMATTE_PASS not in passes:
        raise ValueError(
            f"comp input {source_dir} was not rendered with passes (no {CRYPTOMATTE_PASS}); "
            f"set param render_passes on its render node"
        )
    base = Recipe.model_validate(render["recipe"])
    tmp = Path(tempfile.mkdtemp(prefix="spectracut-comp-"))
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
        assets_sha = ds.stage_assets(tmp, config.shared, referenced)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assets_dir = ds.storage_assets_dir(assets_sha)
    resolution = [s for s in base.steps if s.tool == "render.set_resolution"][-1:]
    calls = [RecipeStep(index=0, tool="scene.clear", args={})]
    calls += [s.model_copy(update={"index": 1}) for s in resolution]
    calls += comp_steps(graph, frames, start=len(calls))
    recipe = Recipe(
        id=f"{base.shot_id}-comp-{graph.id}",
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
    comped = ds.frame_hashes(out)
    if not comped:
        raise RuntimeError(f"comp in {out} produced no frame_* files")
    flamenco = result.output if isinstance(result.output, dict) else {}
    output: dict[str, JsonValue] = {
        "comp_id": graph.id,
        "comp_version": graph.version,
        "comp_recipe_hash": recipe.content_hash,
        "frames": dict(comped),
        "frame_files": dict(ds.frame_file_hashes(out)),
        "output_dir": str(out),
        "recipe": render["recipe"],
        "recipe_hash": render.get("recipe_hash"),
        "assets_sha": assets_sha,
        "job_id": job_id,
        "flamenco_job_id": flamenco.get("flamenco_job_id"),
    }
    if "render_rules" in render:
        output["render_rules"] = render["render_rules"]
    return RunResult(output=output, usage=result.usage)
