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
takes; the control plane never imports ``bpy``.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import JsonValue

from spectracut.queue.executors.base import JobStatus
from spectracut.queue.queue import JobFailed, JobQueue
from spectracut.spec.recipe import Recipe
from spectracut.storyboard.explore import record_image
from spectracut.storyboard.models import ArtDirection, ImageRecord, StyleFrame
from spectracut.studio import device_shot as ds
from spectracut.studio.departments.art import STYLE_FRAMES
from spectracut.studio.departments.shots import tier_recipe
from spectracut.studio.ledger import Usage
from spectracut.studio.node import Tier
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
        deps = self.deps()
        config, opts = deps.config, deps.options or ds.ExecutorOptions()
        source, concept = self._stage_source(frame)
        recipe = style_frame_recipe(frame, art, concept, tier=self.tier)
        assets_dir, _sha = ds.stage_for(config, str(source), recipe)
        base_id = f"{self.production_id}-{self.node_id}-{frame.id}"[:96]
        job_id = ds.fresh_job_id(config, f"{base_id}-{recipe.content_hash[7:19]}")
        envelope = ds.envelope(job_id, recipe, config, opts.now())
        job_file = ds.sign_to_shared(envelope, deps.key, config.shared)
        out = config.out_dir(job_id)
        job = ds.blender_job(envelope, job_file, out, config, self.tier, assets_dir)
        job.node_id = self.node_id
        queue = JobQueue([ds.executor(deps.client, out, config, opts, assets_dir)], now=opts.now)
        queue.submit(job)
        queue.run_until_idle(sleep=opts.sleep, jobs=[job.id])
        if job.status is not JobStatus.SUCCEEDED or job.result is None:
            raise JobFailed(
                f"style frame {frame.id!r}: job {job.id} {job.status.value}: {job.error}",
                usage=job.failed_usage or Usage(),
            )
        _scene, frames = ds.render_record(out)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out / sorted(frames)[0], path)
        return record_image(path, PROVIDER), Usage(gpu_minutes=job.result.usage.gpu_minutes)
