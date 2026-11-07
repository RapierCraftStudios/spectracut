# SPDX-License-Identifier: AGPL-3.0-or-later
"""Style frames rendered by Blender on the farm at the final tier.

:func:`style_frame_recipe` builds a declarative still per :class:`StyleFrame`: a
backdrop filling a 16:9 camera, showing the frame's concept image (pinned by
sha256, ``material.apply_screen``) or, with no concept, the art direction's first
palette swatch. :class:`FarmStyleFrameRenderer` renders it as one signed Blender job
through ``JobQueue`` + :class:`~spectracut.queue.executors.flamenco.FlamencoExecutor`
pinned to the config's worker tag (the same path as
:class:`~spectracut.studio.departments.shots.ShotRunner`), copies the frame to the
requested path and returns its :class:`ImageRecord` with the GPU minutes used. It is
the ``renderer`` :class:`~spectracut.studio.departments.art.PreproductionRunner`
takes for the animatic's boards (at the preview tier).

:class:`SceneStyleFrameRenderer` is the style-frame renderer in production: per frame
the :class:`~spectracut.studio.scene_builder.SceneBuilder` builds a CGI scene from the
frame's description at the preview tier (:class:`FarmRecipeRenderer`), the vision
verifier scores each candidate against the description
(:func:`~spectracut.critique.scene_verifier.scene_critic`), and the best recipe is
re-rendered at the final tier to the requested path. The control plane never imports
``bpy``.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import JsonValue

from spectracut.critique.scene_verifier import scene_critic
from spectracut.providers.base import TextProvider, VisionJudge
from spectracut.queue.executors.base import JobStatus
from spectracut.queue.queue import JobFailed, JobQueue
from spectracut.spec.brand import BrandKit
from spectracut.spec.recipe import Recipe
from spectracut.storyboard.explore import record_image
from spectracut.storyboard.models import ArtDirection, ImageRecord, StyleFrame
from spectracut.studio import device_shot as ds
from spectracut.studio.departments.art import STYLE_FRAMES
from spectracut.studio.departments.shots import tier_recipe
from spectracut.studio.ledger import Usage
from spectracut.studio.node import Tier
from spectracut.studio.profiles import QualityProfile
from spectracut.studio.scene_builder import SceneBrief, SceneBuilder
from spectracut.templates.base import Call, CameraVariant, camera_calls, steps

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from spectracut.queue.executors.flamenco import FlamencoApi

PROVIDER = "blender-farm"
"""``ImageRecord.provider`` of a farm-rendered style frame."""
BACKDROP = "style_frame"
CONCEPT_FILE = "concept.png"
"""The concept's relpath inside the staged assets dir."""

#: A top-down camera filling its 16:9 frame with the 2 x 2 plane scaled to 16:9
#: (36 mm sensor at 50 mm sees 3.556 units across at 4.94 units).
CAMERA = CameraVariant(
    id="frame",
    location=(0.0, 0.0, 4.94),
    rotation=(0.0, 0.0, 0.0),
    lens=50,
    vertical_location=(0.0, 0.0, 4.94),
    vertical_rotation=(0.0, 0.0, 0.0),
    vertical_lens=50,
)
_PLANE_SCALE: list[JsonValue] = [1.7778, 1.0, 1.0]


class FarmDeps(Protocol):
    """What the renderer needs from the farm (``cli.studio.DeviceShotDeps`` fits)."""

    @property
    def config(self) -> ds.DeviceShotConfig: ...
    @property
    def client(self) -> FlamencoApi: ...
    @property
    def key(self) -> Ed25519PrivateKey: ...
    @property
    def options(self) -> ds.ExecutorOptions | None: ...


def _rgb(hex_colour: str) -> list[JsonValue]:
    h = hex_colour.lstrip("#")
    return [round(int(h[i : i + 2], 16) / 255.0, 4) for i in (0, 2, 4)]


def style_frame_recipe(
    frame: StyleFrame,
    art: ArtDirection,
    concept_relpath: str | None = None,
    *,
    tier: Tier = Tier.FINAL,
) -> Recipe:
    """The PNG recipe for ``frame`` at ``tier`` (final for style frames).

    With ``concept_relpath`` (the concept's path in the staged assets dir) the
    backdrop shows ``frame.concept`` pinned by its sha256; without, the art
    direction's first palette swatch.
    """
    calls: list[Call] = [
        ("scene.clear", {}),
        (
            "object.add_primitive",
            {"kind": "plane", "name": BACKDROP, "location": [0, 0, 0], "scale": _PLANE_SCALE},
        ),
    ]
    if concept_relpath is not None and frame.concept is not None:
        sha = frame.concept.sha256.removeprefix("sha256:")
        calls.append(
            (
                "material.apply_screen",
                {"object": BACKDROP, "image": concept_relpath, "image_sha256": sha},
            )
        )
    else:
        swatches = art.palette.swatches
        colour = swatches[sorted(swatches)[0]]
        calls.append(
            (
                "material.apply_preset",
                {"object": BACKDROP, "preset": "matte_plastic", "color": _rgb(colour)},
            )
        )
    calls += camera_calls(CAMERA, "16:9")
    base = Recipe(
        id=f"style-frame-{frame.id}"[:63],
        shot_id=f"style-frame-{frame.id}"[:63],
        blender_version=ds.BLENDER_VERSION,
        steps=steps(calls),
    )
    return tier_recipe(base, tier)


class FarmStyleFrameRenderer:
    """Renders style frames on the farm; ``deps`` is called once, on the first frame.

    ``tier`` and ``node_id`` default to final-tier style frames; the animatic's boards
    use the same renderer at :attr:`Tier.PREVIEW` under the ``animatic`` node.
    """

    def __init__(
        self,
        deps: Callable[[], FarmDeps],
        production_id: str,
        workdir: Path,
        *,
        tier: Tier = Tier.FINAL,
        node_id: str = STYLE_FRAMES,
    ) -> None:
        self._factory = deps
        self._deps: FarmDeps | None = None
        self._lock = threading.Lock()
        self.production_id = production_id
        self.workdir = Path(workdir)
        self.tier = tier
        self.node_id = node_id

    def deps(self) -> FarmDeps:
        with self._lock:
            if self._deps is None:
                self._deps = self._factory()
            return self._deps

    def _stage_source(self, frame: StyleFrame) -> tuple[Path, str | None]:
        """A dir holding only this frame's concept (as :data:`CONCEPT_FILE`), if any."""
        source = self.workdir / "stage" / frame.id
        if source.exists():
            shutil.rmtree(source)
        source.mkdir(parents=True)
        if frame.concept is None:
            return source, None
        shutil.copyfile(frame.concept.path, source / CONCEPT_FILE)
        return source, CONCEPT_FILE

    def __call__(
        self, frame: StyleFrame, art: ArtDirection, path: Path
    ) -> tuple[ImageRecord, Usage]:
        source, concept = self._stage_source(frame)
        recipe = style_frame_recipe(frame, art, concept, tier=self.tier)
        base_id = f"{self.production_id}-{self.node_id}-{frame.id}"[:96]
        return render_recipe(
            self.deps(),
            recipe,
            source,
            base_id,
            path,
            tier=self.tier,
            node_id=self.node_id,
            label=f"style frame {frame.id!r}",
        )


def render_recipe(
    deps: FarmDeps,
    recipe: Recipe,
    source: Path,
    base_id: str,
    path: Path,
    *,
    tier: Tier,
    node_id: str,
    label: str = "recipe",
) -> tuple[ImageRecord, Usage]:
    """Render ``recipe`` (tier steps already applied) as one signed farm job.

    ``source`` is the assets dir to stage (may be empty); the first rendered frame is
    copied to ``path``. Returns its :class:`ImageRecord` and the GPU minutes used.
    """
    config, opts = deps.config, deps.options or ds.ExecutorOptions()
    assets_dir, _sha = ds.stage_for(config, str(source), recipe)
    job_id = ds.fresh_job_id(config, f"{base_id}-{recipe.content_hash[7:19]}")
    envelope = ds.envelope(job_id, recipe, config, opts.now())
    job_file = ds.sign_to_shared(envelope, deps.key, config.shared)
    out = config.out_dir(job_id)
    job = ds.blender_job(envelope, job_file, out, config, tier, assets_dir)
    job.node_id = node_id
    queue = JobQueue([ds.executor(deps.client, out, config, opts, assets_dir)], now=opts.now)
    queue.submit(job)
    queue.run_until_idle(sleep=opts.sleep, jobs=[job.id])
    if job.status is not JobStatus.SUCCEEDED or job.result is None:
        raise JobFailed(
            f"{label}: job {job.id} {job.status.value}: {job.error}",
            usage=job.failed_usage or Usage(),
        )
    _scene, frames = ds.render_record(out)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(out / sorted(frames)[0], path)
    return record_image(path, PROVIDER), Usage(gpu_minutes=job.result.usage.gpu_minutes)


SCENE_BUILD = "scene_build"
"""Default node id of scene-builder preview renders."""


class FarmRecipeRenderer:
    """Renders any declarative recipe (a scene builder candidate) on the farm.

    The recipe gets :func:`~spectracut.studio.departments.shots.tier_recipe` steps at
    ``tier`` (preview by default) and renders as one signed job with an empty assets
    dir; ``deps`` is called once, on the first render. It is the scene builder's
    ``RecipeRenderer``: ``(recipe, path) -> (ImageRecord, Usage)``.
    """

    def __init__(
        self,
        deps: Callable[[], FarmDeps],
        production_id: str,
        workdir: Path,
        *,
        tier: Tier = Tier.PREVIEW,
        node_id: str = SCENE_BUILD,
    ) -> None:
        self._factory = deps
        self._deps: FarmDeps | None = None
        self._lock = threading.Lock()
        self.production_id = production_id
        self.workdir = Path(workdir)
        self.tier = tier
        self.node_id = node_id

    def deps(self) -> FarmDeps:
        with self._lock:
            if self._deps is None:
                self._deps = self._factory()
            return self._deps

    def __call__(self, recipe: Recipe, path: Path) -> tuple[ImageRecord, Usage]:
        source = self.workdir / "stage" / recipe.id
        if source.exists():
            shutil.rmtree(source)
        source.mkdir(parents=True)
        tiered = tier_recipe(recipe, self.tier)
        base_id = f"{self.production_id}-{self.node_id}-{recipe.id}"[:96]
        return render_recipe(
            self.deps(),
            tiered,
            source,
            base_id,
            path,
            tier=self.tier,
            node_id=self.node_id,
            label=f"scene recipe {recipe.id!r}",
        )


def _add_usage(a: Usage, b: Usage) -> Usage:
    ai = dict(a.ai_usage)
    for key, value in b.ai_usage.items():
        ai[key] = ai.get(key, 0) + value
    return Usage(
        gpu_minutes=a.gpu_minutes + b.gpu_minutes,
        ai_usage=ai,
        elevenlabs_characters=a.elevenlabs_characters + b.elevenlabs_characters,
    )


class SceneStyleFrameRenderer:
    """Style frames built by the scene builder, then rendered at the final tier.

    Per frame: a :class:`SceneBrief` from the frame's description and prompt, the art
    direction and the brand kit; one :class:`SceneBuilder` (its critic is bound to the
    description) rendering candidates at the preview tier on the farm; the build is
    written to ``workdir/scene/<frame>/build.json``; the best recipe re-renders at the
    final tier under the ``style_frames`` node to the requested path. Returns the
    final :class:`ImageRecord` and the build's usage plus the final GPU minutes. A
    build with no recipe raises :class:`ValueError` naming the frame.

    ``text``, ``vision`` and ``deps`` are factories called on the first frame, so
    text-only runs never touch the farm or a model.
    """

    def __init__(
        self,
        text: Callable[[], TextProvider],
        vision: Callable[[], VisionJudge],
        profile: QualityProfile,
        deps: Callable[[], FarmDeps],
        production_id: str,
        workdir: Path,
        *,
        brand_kit: Callable[[], BrandKit | None] = lambda: None,
        node_id: str = STYLE_FRAMES,
    ) -> None:
        self._text_factory = text
        self._vision_factory = vision
        self._text: TextProvider | None = None
        self._vision: VisionJudge | None = None
        self._lock = threading.Lock()
        self.profile = profile
        self.brand_kit = brand_kit
        self.production_id = production_id
        self.workdir = Path(workdir)
        self.node_id = node_id
        self.preview = FarmRecipeRenderer(deps, production_id, self.workdir / "scene")
        self.final = FarmRecipeRenderer(
            self.preview.deps,
            production_id,
            self.workdir / "final",
            tier=Tier.FINAL,
            node_id=node_id,
        )

    def _models(self) -> tuple[TextProvider, VisionJudge]:
        with self._lock:
            if self._text is None:
                self._text = self._text_factory()
            if self._vision is None:
                self._vision = self._vision_factory()
            return self._text, self._vision

    def __call__(
        self, frame: StyleFrame, art: ArtDirection, path: Path
    ) -> tuple[ImageRecord, Usage]:
        text, vision = self._models()
        kit = self.brand_kit()
        description = frame.description.strip()
        if frame.prompt.strip() and frame.prompt.strip() != description:
            description = f"{description}\n\n{frame.prompt.strip()}"
        brief = SceneBrief(id=frame.id, description=description, art=art, brand_kit=kit)
        critic = scene_critic(
            vision, profile=self.profile, description=description, art=art, brand_kit=kit
        )
        scene_dir = self.workdir / "scene"
        builder = SceneBuilder(text, self.preview, critic, self.profile, scene_dir)
        build = builder.build(brief)
        out = scene_dir / frame.id / "build.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(build.model_dump_json(indent=1), encoding="utf-8")
        if build.recipe is None:
            raise ValueError(
                f"style frame {frame.id!r}: the scene builder produced no recipe "
                f"({build.status}: {build.notes or 'no candidate rendered'})"
            )
        record, final = self.final(build.recipe, Path(path))
        return record, _add_usage(build.usage, final)
