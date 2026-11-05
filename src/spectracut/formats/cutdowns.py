# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cut-downs: ads and demos cut from one production, never made separately.

A cut-down is a new :class:`~spectracut.edit.timeline.Edit` built from the master
edit's shots (spike § Production pipeline: from shots to a finished film):

- every delivery aspect has its own camera, so each shot is rendered once per
  aspect. A cut-down in 9:16 uses only the **approved** 9:16 render of each shot,
  matched on shot id, shot content hash and aspect. A shot with no such render
  raises :class:`MissingRender`; another aspect's render is never borrowed, so a
  frame is never cropped and a cut-down never asks for a new render;
- the cut-down is exactly ``length_s`` long at the master's frame rate (6, 15 or
  30 s). Its shots share the length in proportion to their length in the master
  and keep the master's in point, so a per-aspect render, which shares the shot's
  animation timing, lines up frame for frame;
- :func:`variant_matrix` crosses hooks, visuals and CTAs into stable variants, and
  :meth:`CutDownSpec.for_variant` turns one into a cut-down.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from itertools import product
from typing import Literal

from pydantic import Field, model_validator

from spectracut.edit.timeline import Clip, Edit, Track
from spectracut.spec._base import ContentHash, Slug, StrictModel
from spectracut.spec.spec import Aspect

CutDownLength = Literal[6, 15, 30]
"""Cut-down lengths in seconds."""


class MissingRender(LookupError):
    """Shots with no approved render in the cut-down's aspect."""

    def __init__(self, missing: Sequence[tuple[str, str]]) -> None:
        self.missing = tuple(missing)
        pairs = ", ".join(f"{shot}@{aspect}" for shot, aspect in self.missing)
        super().__init__(f"no approved render for {pairs}")


class AspectRender(StrictModel):
    """One shot rendered through one aspect's camera."""

    shot_id: Slug
    shot_hash: ContentHash
    aspect: Aspect
    media_ref: str = Field(min_length=1)
    frames: int = Field(gt=0)
    """Source frames in the render (frame 0 to ``frames - 1``)."""
    approved: bool = False


class RenderLibrary(StrictModel):
    """The production's per-aspect renders."""

    renders: tuple[AspectRender, ...] = ()

    @model_validator(mode="after")
    def _unique(self) -> RenderLibrary:
        seen: set[tuple[str, str, str]] = set()
        for r in self.renders:
            key = (r.shot_id, r.shot_hash, r.aspect)
            if key in seen:
                raise ValueError(f"duplicate render for {r.shot_id}@{r.aspect}")
            seen.add(key)
        return self

    def lookup(self, shot_id: str, shot_hash: str, aspect: str) -> AspectRender | None:
        """The approved render of exactly this shot version in exactly this aspect."""
        for r in self.renders:
            if (
                r.approved
                and r.shot_id == shot_id
                and r.shot_hash == shot_hash
                and r.aspect == aspect
            ):
                return r
        return None


class Variant(StrictModel):
    """One cell of the variant matrix: a hook, a visual and a CTA (shot ids)."""

    id: Slug
    hook: Slug
    visual: Slug
    cta: Slug


def _axis(name: str, values: Iterable[str]) -> list[str]:
    out = list(values)
    if not out:
        raise ValueError(f"variant matrix needs at least one {name}")
    if len(set(out)) != len(out):
        raise ValueError(f"duplicate {name} in variant matrix")
    return out


def variant_matrix(
    hooks: Iterable[str], visuals: Iterable[str], ctas: Iterable[str]
) -> list[Variant]:
    """Every hook × visual × CTA, in axis order, with ids ``hNN-vNN-cNN``."""
    h, v, c = _axis("hook", hooks), _axis("visual", visuals), _axis("cta", ctas)
    return [
        Variant(id=f"h{i:02d}-v{j:02d}-c{k:02d}", hook=hook, visual=visual, cta=cta)
        for (i, hook), (j, visual), (k, cta) in product(
            enumerate(h, 1), enumerate(v, 1), enumerate(c, 1)
        )
    ]


class CutDownSpec(StrictModel):
    """What to cut: length, aspect, target platform and the shots in order."""

    id: Slug
    length_s: CutDownLength
    aspect: Aspect
    platform: Slug
    shots: list[Slug] = Field(min_length=1)
    variant_id: Slug | None = None

    @classmethod
    def for_variant(
        cls,
        variant: Variant,
        *,
        length_s: CutDownLength,
        aspect: Aspect,
        platform: str,
        id: str | None = None,
    ) -> CutDownSpec:
        """A cut-down of ``variant``: hook, then visual, then CTA."""
        slug = aspect.replace(":", "x")
        return cls(
            id=id or f"{variant.id}-{length_s}s-{slug}",
            length_s=length_s,
            aspect=aspect,
            platform=platform,
            shots=[variant.hook, variant.visual, variant.cta],
            variant_id=variant.id,
        )


def _master_clip(edit: Edit, shot_id: str) -> Clip | None:
    """The first picture clip of ``shot_id`` in ``edit``, searching nested edits."""
    for track in edit.tracks:
        if track.kind != "video":
            continue
        for clip in track.clips():
            if clip.shot_id == shot_id:
                return clip
            if clip.nested is not None:
                found = _master_clip(clip.nested, shot_id)
                if found is not None:
                    return found
    return None


def _sources(master: Edit, spec: CutDownSpec) -> list[Clip]:
    clips: list[Clip] = []
    for shot_id in spec.shots:
        clip = _master_clip(master, shot_id)
        if clip is None:
            raise ValueError(f"shot {shot_id!r} is not in master edit {master.id!r}")
        if clip.shot_hash is None:
            raise ValueError(f"shot {shot_id!r} has no shot_hash in the master edit")
        clips.append(clip)
    return clips


def required_renders(
    master: Edit, library: RenderLibrary, spec: CutDownSpec
) -> list[tuple[str, str]]:
    """``(shot_id, aspect)`` pairs the cut-down would need rendered; empty means none."""
    missing: list[tuple[str, str]] = []
    for clip in _sources(master, spec):
        assert clip.shot_id is not None and clip.shot_hash is not None
        pair = (clip.shot_id, spec.aspect)
        if library.lookup(clip.shot_id, clip.shot_hash, spec.aspect) is None:
            if pair not in missing:
                missing.append(pair)
    return missing


def _shares(total: int, weights: Sequence[int]) -> list[int]:
    """Split ``total`` frames in proportion to ``weights`` (largest remainder)."""
    whole = sum(weights)
    raw = [total * w for w in weights]
    shares = [r // whole for r in raw]
    order = sorted(range(len(weights)), key=lambda i: (-(raw[i] % whole), i))
    for i in order[: total - sum(shares)]:
        shares[i] += 1
    return shares


def cut_down(master: Edit, library: RenderLibrary, spec: CutDownSpec) -> Edit:
    """Cut ``spec`` from ``master`` using only approved renders in ``spec.aspect``."""
    missing = required_renders(master, library, spec)
    if missing:
        raise MissingRender(missing)
    sources = _sources(master, spec)
    target = master.rate.frames(spec.length_s)
    shares = _shares(target, [c.duration for c in sources])
    items: list[Clip] = []
    for n, (clip, share) in enumerate(zip(sources, shares, strict=True), 1):
        assert clip.shot_id is not None and clip.shot_hash is not None
        if share <= 0:
            raise ValueError(f"cut-down {spec.id!r} is too short for shot {clip.shot_id!r}")
        render = library.lookup(clip.shot_id, clip.shot_hash, spec.aspect)
        assert render is not None
        source_out = clip.source_in + share
        if source_out > render.frames:
            raise ValueError(
                f"render of {clip.shot_id}@{spec.aspect} has {render.frames} frames; "
                f"cut-down {spec.id!r} needs {source_out}"
            )
        items.append(
            Clip(
                id=f"c{n:02d}-{clip.shot_id}"[:128],
                name=clip.name,
                shot_id=clip.shot_id,
                shot_hash=clip.shot_hash,
                media_ref=render.media_ref,
                source_in=clip.source_in,
                source_out=source_out,
            )
        )
    return Edit(
        id=spec.id,
        name=spec.variant_id or spec.id,
        rate=master.rate,
        tracks=[Track(id="v1", kind="video", items=list(items))],
    )
