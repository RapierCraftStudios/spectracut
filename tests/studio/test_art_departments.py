# SPDX-License-Identifier: AGPL-3.0-or-later
"""Art direction, style frames and storyboard read the approved concept and the brand kit (#270)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import JsonValue

from spectracut.providers.mock_text import MockTextProvider
from spectracut.spec.brand import BrandKit, load_brand_kit
from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.studio.department import DepartmentOutputError
from spectracut.studio.departments import all_departments
from spectracut.studio.departments.art import (
    ART_DEPARTMENTS,
    ArtDirector,
    StoryboardArtist,
    StyleFrameArtist,
    preproduction_nodes,
)
from spectracut.studio.graph import ProductionGraph, RunContext, RunResult
from spectracut.studio.node import NodeSpec, NodeStatus, Tier
from spectracut.studio.producer import Producer, edge_problems
from spectracut.studio.profiles import load_profile
from spectracut.studio.runner import CapacityLimits, DepartmentRunner

KIT_PATH = Path(__file__).parents[2] / "brands" / "alterlab" / "brand-kit.yaml"


def _brief() -> Brief:
    return Brief(
        id="spellwise",
        product="Spellwise",
        audience=AudienceProfile(personas=[Persona(name="Dev", description="ships often")]),
        sources=[ClaimSource(id="docs", title="Docs", ref="https://example.test/docs")],
        proof_points=[ProofPoint(claim="Fast spellcheck", source_id="docs")],
        cta="Try it",
    )


@pytest.fixture(scope="module")
def kit() -> BrandKit:
    return load_brand_kit(KIT_PATH)


def _art(swatches: dict[str, str], look: str) -> JsonValue:
    return {
        "mood_board": [{"ref": "glass stack", "take": "light"}],
        "palette": {"swatches": swatches},
        "look": {"id": look},
    }


CONCEPT: JsonValue = {
    "title": "Glass Stack",
    "logline": "The stack assembles itself in glass.",
    "structure": ["Panels slide into a stack", "The dashboard lights up"],
    "fit": "Engineers see the product.",
    "visuals": ["Frosted glass panels catching a rim light"],
}


def _frames(*refs: str) -> JsonValue:
    return {
        "frames": [
            {"id": f"f{i}", "description": "a frame", "prompt": "glass", "concept_ref": ref}
            for i, ref in enumerate(refs)
        ]
    }


def _kinds(*ids: str) -> dict[str, str]:
    return {i: i for i in ids}


# ---- R1/R8: declared inputs and edges ---------------------------------------


def test_art_nodes_declare_the_concept_and_the_brand_kit_as_inputs() -> None:
    for dept in (ArtDirector, StyleFrameArtist, StoryboardArtist):
        assert "concept" in dept.inputs, dept.name
        assert "brand_kit" in dept.inputs, dept.name
    nodes = {
        n.id: n.inputs for n in preproduction_nodes(concept_id="concept", brand_kit_id="brand_kit")
    }
    assert nodes["art_direction"] == ("brief", "concept", "brand_kit")
    assert nodes["style_frames"] == ("brief", "concept", "brand_kit", "art_direction")
    assert nodes["storyboard"] == ("brief", "concept", "script", "brand_kit", "art_direction")


def test_preproduction_nodes_without_new_kwargs_are_unchanged() -> None:
    nodes = {n.id: n.inputs for n in preproduction_nodes()}
    assert nodes["art_direction"] == ("brief",)
    assert nodes["style_frames"] == ("brief", "art_direction")
    assert nodes["storyboard"] == ("brief", "script", "art_direction")


def test_producer_wires_the_brand_kit_into_every_profile(kit: BrandKit) -> None:
    profile = load_profile("standard")
    plan = Producer().breakdown(_brief(), profile, duration_s=60, brand_kit=kit)
    nodes = {n.id: n for n in plan.nodes}
    assert nodes["art_direction"].inputs == ("brief", "concept", "brand_kit")
    assert nodes["style_frames"].inputs == ("brief", "concept", "brand_kit", "art_direction")
    assert nodes["storyboard"].inputs == (
        "brief",
        "concept",
        "script",
        "brand_kit",
        "art_direction",
    )
    assert [n.id for n in plan.nodes][:2] == ["brief", "brand_kit"]
    assert nodes["brand_kit"].department == "brand-kit"
    graph = Producer().new_graph("p", plan, profile)
    assert edge_problems(graph, all_departments()) == []

    draft = load_profile("draft")
    draft_plan = Producer().breakdown(_brief(), draft, duration_s=60, brand_kit=kit)
    assert "brand_kit" in {n.id for n in draft_plan.nodes}
    assert edge_problems(Producer().new_graph("d", draft_plan, draft), all_departments()) == []


def test_producer_without_a_kit_has_no_brand_kit_node() -> None:
    profile = load_profile("standard")
    plan = Producer().breakdown(_brief(), profile, duration_s=60)
    assert [n.id for n in plan.nodes] == list(profile.stages)


# ---- R2: palette and Look come from the brand kit --------------------------


def test_art_direction_palette_and_look_come_from_the_brand_kit(kit: BrandKit) -> None:
    inputs = {"brand_kit": kit.model_dump(mode="json")}
    hexes = list(kit.palette.swatches.values())
    ok = _art({"ink": hexes[0], "accent": hexes[2].upper()}, "clean")
    ArtDirector.check_output("art_direction", ok, inputs, _kinds("brand_kit"))

    off = _art({"bg": "#0d1117", "ink": hexes[0]}, "alterlab-clean-terminal")
    with pytest.raises(DepartmentOutputError) as err:
        ArtDirector.check_output("art_direction", off, inputs, _kinds("brand_kit"))
    assert "#0d1117" in str(err.value)
    assert "alterlab-clean-terminal" in str(err.value)


def test_art_direction_check_is_a_no_op_without_a_kit() -> None:
    off = _art({"bg": "#0d1117"}, "alterlab-clean-terminal")
    ArtDirector.check_output("art_direction", off, {}, {})


# ---- R3: style frames reference the concept --------------------------------


def test_style_frames_reference_a_beat_or_signature_visual_of_the_concept() -> None:
    inputs = {"concept": CONCEPT}
    ok = _frames("Panels slide into a stack", "  frosted glass panels catching a RIM light ")
    StyleFrameArtist.check_output("style_frames", ok, inputs, _kinds("concept"))

    for bad in ("", "A GitHub dark-mode terminal"):
        with pytest.raises(DepartmentOutputError, match="concept 'Glass Stack'"):
            StyleFrameArtist.check_output(
                "style_frames", _frames("Panels slide into a stack", bad), inputs, _kinds("concept")
            )


# ---- runner and the brand kit node -----------------------------------------


def test_department_runner_fails_an_off_kit_art_direction(kit: BrandKit) -> None:
    from spectracut.studio.departments.art import brand_kit_node, build_brand_kit

    graph = ProductionGraph("p")
    graph.add_nodes(
        [
            brand_kit_node(kit),
            NodeSpec(
                id="art_direction",
                kind="art_direction",
                department="art-director",
                inputs=("brand_kit",),
            ),
        ]
    )
    off = _art({"bg": "#0d1117"}, "clean")
    inner = DepartmentRunner(
        ART_DEPARTMENTS,
        MockTextProvider(respond=lambda s, p, schema: off),
        limits=CapacityLimits(gpu_leases=1),
        graph=graph,
    )

    def run(ctx: RunContext) -> RunResult:
        return build_brand_kit(ctx) if ctx.spec.kind == "brand_kit" else inner(ctx)

    report = graph.run(run)
    assert report.failed == ["art_direction"]
    state = graph.state("art_direction")
    assert state.status is NodeStatus.FAILED
    assert state.error is not None and "#0d1117" in state.error


def test_build_brand_kit_returns_the_kit_and_needs_its_param(kit: BrandKit) -> None:
    from spectracut.studio.departments.art import brand_kit_node, build_brand_kit

    spec = brand_kit_node(kit)
    ctx = RunContext(spec=spec, tier=Tier.FINAL, key="k", inputs={}, attempt=1)
    assert BrandKit.model_validate(build_brand_kit(ctx).output) == kit
    bare = spec.model_copy(update={"params": {}})
    with pytest.raises(ValueError, match="--brand-kit"):
        build_brand_kit(ctx.model_copy(update={"spec": bare}))
