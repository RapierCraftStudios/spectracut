# SPDX-License-Identifier: AGPL-3.0-or-later
"""Departments receive the reference films and cite the craft points they apply (#342)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from spectracut.edit.timeline import Clip, Edit, Track
from spectracut.providers.mock_image import MockImageProvider
from spectracut.providers.mock_text import MockTextProvider
from spectracut.providers.mock_vision import MockVisionJudge
from spectracut.queue.signing import generate_keypair, load_private_key
from spectracut.spec.brand import BrandKit, load_brand_kit
from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.spec.references import ReferenceSet, load_reference_set
from spectracut.storyboard.explore import record_image
from spectracut.storyboard.models import ArtDirection, ImageRecord, StyleFrame, StyleFrames
from spectracut.studio import device_shot as ds
from spectracut.studio.department import DepartmentOutputError
from spectracut.studio.departments import STAGE_DEPARTMENTS, all_departments
from spectracut.studio.departments import finishing as fd
from spectracut.studio.departments.art import (
    ART_DEPARTMENTS,
    STYLE_FRAMES,
    ArtDirector,
    PreproductionRunner,
    StoryboardArtist,
    preproduction_nodes,
)
from spectracut.studio.departments.references import (
    REFERENCES,
    ReferencesDepartment,
    build_references,
    references_node,
)
from spectracut.studio.departments.shots import RenderedShots
from spectracut.studio.departments.writer import (
    DEVELOPMENT_DEPARTMENTS,
    ConceptWriter,
    development_nodes,
)
from spectracut.studio.graph import ProductionGraph, RunContext, RunResult
from spectracut.studio.node import NodeSpec, NodeStatus, Tier
from spectracut.studio.producer import Producer, edge_problems
from spectracut.studio.profiles import load_profile
from spectracut.studio.runner import DepartmentRunner
from spectracut.templates import TEMPLATES

ROOT = Path(__file__).parents[2]
KIT = ROOT / "brands" / "alterlab" / "brand-kit.yaml"
BENCHMARKS = ROOT / "benchmarks"

KINETIC = "base44-launch:kinetic-type-as-the-spine"
REAL_UI = "base44-launch:real-ui-as-the-hero"
LIGHT_DARK = "base44-launch:rhythm-of-light-and-dark"


@pytest.fixture(scope="module")
def kit() -> BrandKit:
    return load_brand_kit(KIT)


def _store(tmp: Path) -> Path:
    store = tmp / "store"
    (store / "keyframes").mkdir(parents=True)
    (store / "keyframes" / "base44-launch.png").write_bytes(b"base44 keyframe")
    return store


def _refs(store: Path | None = None) -> ReferenceSet:
    return load_reference_set(root=BENCHMARKS, store=store)


def _brief() -> Brief:
    return Brief(
        id="alterlab",
        product="AlterLab",
        audience=AudienceProfile(personas=[Persona(name="Dev", description="scrapes the web")]),
        sources=[ClaimSource(id="docs", title="Docs", ref="https://example.test/docs")],
        proof_points=[ProofPoint(claim="Structured data", source_id="docs")],
        cta="Start free",
    )


def _ref_input(refs: ReferenceSet) -> dict[str, JsonValue]:
    return {REFERENCES: refs.model_dump(mode="json")}


def _kinds(*ids: str) -> dict[str, str]:
    return {i: i for i in ids}


CONCEPT: dict[str, JsonValue] = {
    "title": "Your Data, Live",
    "logline": "A URL becomes structured data in seconds.",
    "structure": ["Paste a URL", "Data appears"],
    "fit": "Developers see the product.",
    "craft_refs": [KINETIC, REAL_UI],
}


def _art(kit: BrandKit, cited: list[str]) -> dict[str, JsonValue]:
    name, hex_ = next(iter(kit.palette.swatches.items()))
    return {
        "mood_board": [{"ref": "white space and dark UI", "take": "rhythm"}],
        "palette": {"swatches": {name: hex_}},
        "look": {"id": kit.default_look_id},
        "craft_refs": list[JsonValue](cited),
    }


def _boards(duration: float, cited: list[str], text: str = "Build") -> dict[str, JsonValue]:
    return {
        "boards": [
            {
                "shot_id": f"s{i}",
                "beat": "hook",
                "action": "a",
                "text": text,
                "duration_s": duration,
            }
            for i in range(3)
        ],
        "craft_refs": list[JsonValue](cited),
    }


# ---- wiring ----------------------------------------------------------------------


def test_producer_wires_the_references_into_the_creative_departments(kit: BrandKit) -> None:
    profile = load_profile("standard")
    plan = Producer().breakdown(_brief(), profile, duration_s=60, brand_kit=kit, references=_refs())
    nodes = {n.id: n for n in plan.nodes}
    assert [n.id for n in plan.nodes][:3] == ["brief", "brand_kit", REFERENCES]
    assert nodes[REFERENCES].department == "references"
    for node in ("concept", "art_direction", STYLE_FRAMES, "storyboard", "edit"):
        assert REFERENCES in nodes[node].inputs, node
    assert "storyboard" in nodes["edit"].inputs
    assert edge_problems(Producer().new_graph("p", plan, profile), all_departments()) == []

    bare = Producer().breakdown(_brief(), profile, duration_s=60, brand_kit=kit)
    assert REFERENCES not in {n.id for n in bare.nodes}
    bare_nodes = {n.id: n for n in bare.nodes}
    assert all(REFERENCES not in n.inputs for n in bare.nodes)
    assert "storyboard" not in bare_nodes["edit"].inputs, "no new edge without references"
    plain = Producer().breakdown(_brief(), profile, duration_s=60)
    assert [n.id for n in plain.nodes] == list(profile.stages)


def test_node_helpers_wire_the_references_when_given() -> None:
    assert all(
        n.inputs == ("brief", REFERENCES) for n in development_nodes(references_id=REFERENCES)
    )
    assert all(n.inputs == ("brief",) for n in development_nodes())
    nodes = {n.id: n for n in preproduction_nodes(references_id=REFERENCES)}
    assert REFERENCES in nodes["art_direction"].inputs
    assert REFERENCES in nodes["storyboard"].inputs
    assert REFERENCES in nodes[STYLE_FRAMES].inputs
    assert all(REFERENCES not in n.inputs for n in preproduction_nodes())


def test_references_department_is_registered_and_builds_in_code() -> None:
    assert all_departments()["references"] is ReferencesDepartment
    assert STAGE_DEPARTMENTS[REFERENCES] == "references"
    refs = _refs()
    spec = references_node(refs)
    ctx = RunContext(spec=spec, tier=Tier.FINAL, key="k", inputs={}, attempt=1)
    assert ReferenceSet.model_validate(build_references(ctx).output) == refs
    bare = spec.model_copy(update={"params": {}})
    with pytest.raises(ValueError, match="'references'"):
        build_references(ctx.model_copy(update={"spec": bare}))


def test_department_runner_passes_the_references_in_the_task_context() -> None:
    refs = _refs()
    graph = ProductionGraph("p")
    concept = development_nodes(references_id=REFERENCES)[0]
    graph.add_nodes([references_node(refs), concept.model_copy(update={"inputs": (REFERENCES,)})])
    prompts: list[str] = []

    def respond(system: str, prompt: str, schema: dict[str, Any]) -> JsonValue:
        prompts.append(prompt)
        return dict(CONCEPT)

    inner = DepartmentRunner(
        DEVELOPMENT_DEPARTMENTS, MockTextProvider(respond=respond), graph=graph
    )

    def run(ctx: RunContext) -> RunResult:
        return build_references(ctx) if ctx.spec.kind == REFERENCES else inner(ctx)

    report = graph.run(run)
    assert report.failed == []
    assert prompts and KINETIC in prompts[0] and "Kinetic type as the spine" in prompts[0]
    assert graph.state(concept.id).status is not NodeStatus.FAILED


# ---- outputs cite craft points --------------------------------------------------


def test_alterlab_outputs_citing_base44_craft_points_pass(kit: BrandKit) -> None:
    refs = _ref_input(_refs())
    ConceptWriter.check_output("concept-1", CONCEPT, refs, _kinds(REFERENCES))
    ArtDirector.check_output(
        "art_direction",
        _art(kit, [REAL_UI, LIGHT_DARK]),
        {**refs, "brand_kit": kit.model_dump(mode="json")},
        _kinds(REFERENCES, "brand_kit"),
    )
    StoryboardArtist.check_output(
        "storyboard", _boards(1.3, [KINETIC, LIGHT_DARK]), refs, _kinds(REFERENCES)
    )


@pytest.mark.parametrize(
    ("cited", "match"),
    [([], "cites no craft point"), (["base44-launch:made-up"], "base44-launch:made-up")],
)
def test_uncited_or_unknown_craft_points_fail(kit: BrandKit, cited: list[str], match: str) -> None:
    refs = _ref_input(_refs())
    with pytest.raises(DepartmentOutputError, match=match):
        ConceptWriter.check_output(
            "c", {**CONCEPT, "craft_refs": list[JsonValue](cited)}, refs, _kinds(REFERENCES)
        )
    with pytest.raises(DepartmentOutputError, match=match):
        ArtDirector.check_output("a", _art(kit, cited), refs, _kinds(REFERENCES))
    with pytest.raises(DepartmentOutputError, match=match):
        StoryboardArtist.check_output("s", _boards(1.3, cited), refs, _kinds(REFERENCES))


def test_without_references_the_citation_check_is_a_no_op(kit: BrandKit) -> None:
    ConceptWriter.check_output("c", {**CONCEPT, "craft_refs": []}, {}, {})
    ArtDirector.check_output("a", _art(kit, []), {}, {})
    StoryboardArtist.check_output("s", _boards(9.0, [], "a very long card " * 3), {}, {})


def test_storyboard_pace_and_type_are_measured_against_the_cited_references() -> None:
    refs = _ref_input(_refs())
    with pytest.raises(DepartmentOutputError, match="outside the cited references' range"):
        StoryboardArtist.check_output("s", _boards(5.0, [KINETIC]), refs, _kinds(REFERENCES))
    long = "one two three four five six seven eight nine"
    with pytest.raises(DepartmentOutputError, match="one idea per card"):
        StoryboardArtist.check_output("s", _boards(1.3, [KINETIC], long), refs, _kinds(REFERENCES))


# ---- reference-comparison scores on the node -------------------------------------


class _Renderer:
    def __call__(self, frame: StyleFrame, art: ArtDirection, path: Path) -> ImageRecord:
        MockImageProvider().generate(f"render {frame.id}", path)
        return record_image(path, "blender-farm")


_FRAMES_OUT: JsonValue = {
    "frames": [
        {"id": "hero", "description": "Dashboard hero", "prompt": "real UI in white space"},
        {"id": "cta", "description": "End card", "prompt": "logo on dark ink"},
    ]
}


def _style_frames(tmp_path: Path, refs: ReferenceSet, kit: BrandKit) -> StyleFrames:
    runner = PreproductionRunner(
        DepartmentRunner(ART_DEPARTMENTS, MockTextProvider(respond=lambda s, p, c: _FRAMES_OUT)),
        {d.name: d for d in ART_DEPARTMENTS},
        explorer=None,
        renderer=_Renderer(),
        voice=None,
        workdir=tmp_path / "work",
        board_renderer=_Renderer(),
        preview=lambda edit, out: out,
        reference_judge=MockVisionJudge(),
    )
    spec = {n.id: n for n in preproduction_nodes()}[STYLE_FRAMES]
    spec = spec.model_copy(update={"inputs": (*spec.inputs, REFERENCES)})
    inputs: dict[str, JsonValue] = {"art_direction": _art(kit, [REAL_UI, LIGHT_DARK])}
    inputs.update(_ref_input(refs))
    ctx = RunContext(spec=spec, tier=Tier.FINAL, key="k", inputs=inputs, attempt=1)
    return StyleFrames.model_validate(runner(ctx).output)


def test_style_frames_record_a_reference_comparison_score(tmp_path: Path, kit: BrandKit) -> None:
    frames = _style_frames(tmp_path, _refs(_store(tmp_path)), kit)
    comparison = frames.reference_comparison
    assert comparison is not None
    assert comparison.references == ["base44-launch"]
    assert len(comparison.frames) == 2 and comparison.score is not None
    assert comparison.judge == "mock-vision" and comparison.pace is None


def test_style_frames_without_local_keyframes_record_a_null_score_with_a_note(
    tmp_path: Path, kit: BrandKit
) -> None:
    frames = _style_frames(tmp_path, _refs(tmp_path / "no-store"), kit)
    comparison = frames.reference_comparison
    assert comparison is not None and comparison.score is None
    assert "no local keyframes" in comparison.note


def _finishing(tmp_path: Path) -> fd.FinishingRunner:
    private, _ = generate_keypair()
    config = ds.DeviceShotConfig(
        flamenco_url="http://fake",
        shared=tmp_path / "shared",
        worker_tag="tag",
        assets_dir=str(tmp_path / "a"),
    )
    return fd.FinishingRunner(
        lambda ctx: RunResult(output={"ok": True}),
        all_departments(),
        config=config,
        client=object(),  # type: ignore[arg-type]
        key=load_private_key(private),
        judge=MockVisionJudge(),
        production_id="film",
        workdir=tmp_path / "finishing",
    )


def _shots(tmp_path: Path, kit: BrandKit) -> RenderedShots:
    rows = []
    for s in ("s0", "s1"):
        out = tmp_path / "renders" / s
        out.mkdir(parents=True)
        (out / "frame_0001.png").write_bytes(f"render {s}".encode())
        recipe = TEMPLATES["typography"].build(kit, {"body": "Build"})
        rows.append(
            {
                "shot_id": s,
                "template": "typography",
                "recipe": recipe.model_dump(mode="json"),
                "recipe_hash": recipe.content_hash,
                "tier": "final",
                "scene_graph_hash": "sha256:" + "1" * 64,
                "frames": {"frame_0001.png": "a"},
                "output_dir": str(out),
                "job_id": f"job-{s}",
                "worker_tag": "tag",
                "qa": {"passed": True},
                "vision_qa": {"score": 0.9},
            }
        )
    return RenderedShots.model_validate({"shots": rows})


def test_the_cut_records_its_pace_and_a_reference_comparison_score(
    tmp_path: Path, kit: BrandKit
) -> None:
    refs = _refs(_store(tmp_path))
    picture = [
        Clip(id=f"pic-{s}", shot_id=s, media_ref=f"board:{s}", source_out=31) for s in ("s0", "s1")
    ]
    animatic = Edit(id="animatic", tracks=[Track(id="picture", kind="video", items=picture)])
    spec = NodeSpec(
        id="edit",
        kind="edit",
        department=fd.Editor.name,
        inputs=("animatic", "shots", REFERENCES, "storyboard"),
    )
    inputs: dict[str, JsonValue] = {
        "animatic": animatic.model_dump(mode="json"),
        "shots": _shots(tmp_path, kit).model_dump(mode="json"),
        "storyboard": _boards(1.3, [KINETIC]),
        **_ref_input(refs),
    }
    ctx = RunContext(spec=spec, tier=Tier.FINAL, key="k", inputs=inputs, attempt=1)
    cut = fd.EditCut.model_validate(_finishing(tmp_path)(ctx).output)
    comparison = cut.reference_comparison
    assert comparison is not None
    assert comparison.pace is not None and comparison.pace.ok
    assert comparison.pace.avg_shot_s == pytest.approx(31 / 24, abs=0.01)
    assert len(comparison.frames) == 2 and comparison.score is not None

    no_refs = ctx.model_copy(
        update={"inputs": {k: v for k, v in inputs.items() if k != REFERENCES}}
    )
    plain = fd.EditCut.model_validate(_finishing(tmp_path / "plain")(no_refs).output)
    assert plain.reference_comparison is None
