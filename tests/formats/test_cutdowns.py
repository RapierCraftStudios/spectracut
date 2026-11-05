# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cut-downs reuse approved per-aspect renders and hit exact lengths (#26)."""

from __future__ import annotations

import hashlib

import pytest

from spectracut.edit.timeline import FPS_23_976, FPS_24, Clip, Edit, FrameRate, Track
from spectracut.formats.cutdowns import (
    AspectRender,
    CutDownSpec,
    MissingRender,
    RenderLibrary,
    cut_down,
    required_renders,
    variant_matrix,
)

SHOTS = ("hook", "product", "cta")


def shot_hash(shot: str, version: int = 1) -> str:
    return "sha256:" + hashlib.sha256(f"{shot}:{version}".encode()).hexdigest()


def master(rate: FrameRate = FPS_24) -> Edit:
    fps = round(float(rate))
    clips = [
        Clip(
            id=f"m-{s}",
            shot_id=s,
            shot_hash=shot_hash(s),
            media_ref=f"renders/{s}-16x9.exr",
            source_in=fps,
            source_out=fps * (5 + n),
        )
        for n, s in enumerate(SHOTS)
    ]
    return Edit(id="film", rate=rate, tracks=[Track(id="v1", items=list(clips))])


def library(aspects: tuple[str, ...] = ("16:9", "9:16"), approved: bool = True) -> RenderLibrary:
    return RenderLibrary(
        renders=tuple(
            AspectRender(
                shot_id=s,
                shot_hash=shot_hash(s),
                aspect=a,
                media_ref=f"renders/{s}-{a.replace(':', 'x')}.exr",
                frames=60 * 30,
                approved=approved,
            )
            for s in SHOTS
            for a in aspects
        )
    )


def spec(length: int = 15, aspect: str = "9:16") -> CutDownSpec:
    return CutDownSpec(
        id="ad",
        length_s=length,
        aspect=aspect,
        platform="tiktok",
        shots=list(SHOTS),
    )


@pytest.mark.parametrize("rate", [FPS_24, FPS_23_976])
@pytest.mark.parametrize("length", [6, 15, 30])
def test_cut_down_is_frame_exact(rate: FrameRate, length: int) -> None:
    cut = cut_down(master(rate), library(), spec(length))
    assert cut.duration == rate.frames(length)
    assert cut.rate == rate


def test_cut_down_uses_only_the_aspect_render_and_keeps_shot_identity() -> None:
    m = master()
    cut = cut_down(m, library(), spec())
    clips = cut.tracks[0].clips()
    assert [c.shot_id for c in clips] == list(SHOTS)
    assert all(c.media_ref.endswith("-9x16.exr") for c in clips)
    assert [c.shot_hash for c in clips] == [shot_hash(s) for s in SHOTS]
    assert [c.source_in for c in clips] == [c.source_in for c in m.tracks[0].clips()]


def test_other_aspect_render_is_never_borrowed() -> None:
    lib = library(aspects=("16:9",))
    assert required_renders(master(), lib, spec()) == [(s, "9:16") for s in SHOTS]
    with pytest.raises(MissingRender) as err:
        cut_down(master(), lib, spec())
    assert err.value.missing == tuple((s, "9:16") for s in SHOTS)


def test_unapproved_render_is_not_used() -> None:
    with pytest.raises(MissingRender):
        cut_down(master(), library(approved=False), spec())


def test_stale_render_of_an_older_shot_version_is_not_used() -> None:
    stale = RenderLibrary(
        renders=tuple(
            r.model_copy(update={"shot_hash": shot_hash(r.shot_id, 0)}) for r in library().renders
        )
    )
    with pytest.raises(MissingRender):
        cut_down(master(), stale, spec())


def test_unknown_shot_is_rejected() -> None:
    bad = spec().model_copy(update={"shots": ["nope"]})
    with pytest.raises(ValueError, match="not in master"):
        cut_down(master(), library(), bad)


def test_render_too_short_is_rejected() -> None:
    short = RenderLibrary(
        renders=tuple(r.model_copy(update={"frames": 30}) for r in library().renders)
    )
    with pytest.raises(ValueError, match="frames"):
        cut_down(master(), short, spec())


def test_cut_down_length_must_be_6_15_or_30() -> None:
    with pytest.raises(ValueError):
        spec(length=20)


def test_variant_matrix_is_the_full_cross_product_with_stable_ids() -> None:
    variants = variant_matrix(["h1", "h2", "h3"], ["v1", "v2"], ["c1"])
    assert len(variants) == 6
    assert [v.id for v in variants][:3] == ["h01-v01-c01", "h01-v02-c01", "h02-v01-c01"]
    assert variants == variant_matrix(["h1", "h2", "h3"], ["v1", "v2"], ["c1"])
    assert len({(v.hook, v.visual, v.cta) for v in variants}) == 6


@pytest.mark.parametrize("axes", [([], ["v"], ["c"]), (["h", "h"], ["v"], ["c"])])
def test_variant_matrix_rejects_empty_or_duplicate_axes(
    axes: tuple[list[str], list[str], list[str]],
) -> None:
    with pytest.raises(ValueError):
        variant_matrix(*axes)


def test_spec_for_variant_orders_hook_visual_cta() -> None:
    (v,) = variant_matrix(["hook"], ["product"], ["cta"])
    s = CutDownSpec.for_variant(v, length_s=15, aspect="9:16", platform="tiktok")
    assert s.shots == ["hook", "product", "cta"]
    assert s.variant_id == v.id
    assert s.id == "h01-v01-c01-15s-9x16"
