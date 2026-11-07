# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scene builder (#280): description -> harness recipe -> farm render -> separate verifier.

Fakes stand in for the builder's text model, the farm renderer and the Critic's
vision judge; each is counted so the tests see one text and one vision call per
candidate, and that the two never coincide.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue
from spectracut_host.guard import runtime_tool_names

from spectracut.critique.revise import RevisionRequest
from spectracut.critique.scene_verifier import scene_critic, scene_verifier_prompt
from spectracut.harness.errors import HarnessError
from spectracut.harness.tools import build_tools, get_tool
from spectracut.providers.base import Completion, VisionScore
from spectracut.spec.brand import BrandKit
from spectracut.spec.recipe import Recipe
from spectracut.storyboard.explore import record_image
from spectracut.storyboard.models import ArtDirection, ImageRecord
from spectracut.studio.graph import ProductionGraph, RunContext, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import NodeSpec, Tier
from spectracut.studio.profiles import CritiqueSettings, QualityProfile
from spectracut.studio.scene_builder import (
    CHEAT_SHEET,
    PACK,
    REPAIR_BUDGET,
    SCENE_BUILD,
    SceneBrief,
    SceneBuild,
    SceneBuilder,
    compile_calls,
    scene_build_runner,
    tool_cheat_sheet,
)

ROOT = Path(__file__).resolve().parents[2]
TOOLS_PY = ROOT / "host" / "bpy_runtime" / "tools.py"

ART = ArtDirection.model_validate(
    {
        "mood_board": [{"ref": "glass product macro", "take": "rim-lit edges"}],
        "palette": {"swatches": {"ink": "#101820", "beam": "#00c2a8"}},
        "materials": ["clear glass", "brushed metal"],
        "look": {"id": "clean"},
    }
)
DESCRIPTION = 'A stack of five glass slabs, one emissive beam, an engraved readout "ONE BEAM".'
KIT = BrandKit.model_validate(
    {
        "id": "alterlab",
        "name": "AlterLab",
        "palette": {"swatches": {"brand": "#ff5500"}},
        "typography": {"families": {"display": "Inter"}},
        "tone": "calm",
        "looks": [{"id": "clean"}],
        "default_look_id": "clean",
        "forbidden_words": ["revolutionary"],
    }
)


def calls(light: str = "key", text: str | None = "ONE BEAM") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [
        {"tool": "scene.clear", "args": {}},
        {"tool": "object.add_primitive", "args": {"kind": "cube", "name": "slab"}},
        {"tool": "material.apply_preset", "args": {"object": "slab", "preset": "matte_plastic"}},
        {"tool": "light.add", "args": {"kind": "area", "name": light, "energy": 500}},
        {"tool": "camera.add", "args": {"name": "cam", "location": [0, -6, 1]}},
        {"tool": "camera.use", "args": {"name": "cam"}},
    ]
    if text is not None:
        out.append({"tool": "text.add", "args": {"body": text, "name": "readout"}})
    return out


class FakeText:
    """The builder's text model: answers from a script, records every prompt."""

    name = "fake-text"

    def __init__(self, outputs: list[JsonValue]) -> None:
        self.outputs = list(outputs)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def complete(self, system: str, prompt: str, *, schema: dict[str, Any]) -> Completion:
        assert set(schema["required"]) == {"plan", "calls"}
        self.systems.append(system)
        self.prompts.append(prompt)
        out = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        return Completion(output=out, usage={"claude_requests": 1, "claude_input_tokens": 40})


class FakeVision:
    """The Critic's judge: scores by candidate folder name (``a1-c0``), default 0.3."""

    name = "fake-vision"

    def __init__(self, scores: Mapping[str, float], notes: str = "add a rim light") -> None:
        self.scores = dict(scores)
        self.notes = notes
        self.calls: list[tuple[Path, str]] = []
        self.last_usage: dict[str, int] = {}

    def score(self, frame_path: Path, rubric: str) -> VisionScore:
        self.calls.append((Path(frame_path), rubric))
        self.last_usage = {"input_tokens": 100, "output_tokens": 10}
        name = Path(frame_path).parent.name
        return VisionScore(score=self.scores.get(name, 0.3), notes=f"{name}: {self.notes}")


class FakeRenderer:
    """The farm: writes a frame per recipe, reports 0.5 GPU minutes."""

    def __init__(self) -> None:
        self.recipes: list[Recipe] = []

    def __call__(self, recipe: Recipe, path: Path) -> tuple[ImageRecord, Usage]:
        self.recipes.append(recipe)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(recipe.content_hash.encode())
        return record_image(path, "fake-farm"), Usage(gpu_minutes=0.5)


def profile(budget: int = 2, n: int = 1, window: int = 2) -> QualityProfile:
    return QualityProfile(
        name="test",
        description="test profile",
        stages=("art_direction", SCENE_BUILD),
        revision_budget={"art_direction": 0, SCENE_BUILD: budget},
        tier={"art_direction": Tier.PREVIEW, SCENE_BUILD: Tier.PREVIEW},
        critique=CritiqueSettings(
            pass_score=0.6,
            window=window,
            best_of_n={SCENE_BUILD: n},
            verify_samples={SCENE_BUILD: 1},
        ),
    )


def builder(
    tmp: Path,
    text: FakeText,
    vision: FakeVision,
    *,
    budget: int = 2,
    n: int = 1,
    kit: BrandKit | None = None,
) -> tuple[SceneBuilder, FakeRenderer]:
    prof = profile(budget, n)
    critic = scene_critic(vision, profile=prof, description=DESCRIPTION, art=ART, brand_kit=kit)
    renderer = FakeRenderer()
    return SceneBuilder(text, renderer, critic, prof, tmp / "work"), renderer


def brief(**kw: Any) -> SceneBrief:
    return SceneBrief(id="one-beam", description=DESCRIPTION, art=ART, **kw)


def answer(light: str = "key", text: str | None = "ONE BEAM") -> JsonValue:
    return {"plan": f"slab stack lit by {light}", "calls": calls(light, text)}


# ---- R10: harness build tools only ---------------------------------------------------


def test_compiled_recipe_uses_only_harness_build_tools_and_runtime_ops() -> None:
    recipe = compile_calls(calls(), recipe_id="r", shot_id="s")
    names = runtime_tool_names(TOOLS_PY)
    assert [s.tool for s in recipe.steps][0] == "scene.clear"
    assert all(s.tool in names for s in recipe.steps)
    assert all(get_tool(s.tool).kind == "build" for s in recipe.steps)
    assert Recipe.model_validate_json(recipe.model_dump_json()) == recipe


@pytest.mark.parametrize(
    "bad",
    [
        [{"tool": "preview.render", "args": {}}],
        [{"tool": "inspect.bounds", "args": {"names": ["slab"]}}],
        [{"tool": "look", "args": {}}],
        [{"tool": "python.exec", "args": {"code": "import os"}}],
        [{"tool": "light.add", "args": {"kind": "laser"}}],
        [{"tool": "scene.clear", "args": {}, "code": "import os"}],
        [],
    ],
)
def test_query_unknown_or_bad_calls_are_refused_and_never_rendered(
    tmp_path: Path, bad: list[dict[str, Any]]
) -> None:
    with pytest.raises(HarnessError):
        compile_calls(bad, recipe_id="r", shot_id="s")
    text = FakeText([{"plan": "bad", "calls": bad}])
    vision = FakeVision({})
    b, renderer = builder(tmp_path, text, vision, budget=0)
    build = b.build(brief())
    assert renderer.recipes == [] and vision.calls == []
    assert build.status == "escalated" and build.recipe is None
    (it,) = build.iterations
    assert it.recipe is None and it.render is None and not it.passed
    assert it.scores["overall"] == 0.0 and it.notes.startswith("failed candidate:")


# ---- R6: separate verifier, stop on pass, budget, best-of-N --------------------------


def test_each_candidate_makes_one_text_and_one_vision_call_and_stops_on_pass(
    tmp_path: Path,
) -> None:
    text = FakeText([answer("a"), answer("b"), answer("c")])
    vision = FakeVision({"a1-c0": 0.4, "a2-c0": 0.9})
    b, renderer = builder(tmp_path, text, vision, budget=4)
    build = b.build(brief())
    assert build.status == "accepted" and build.attempts == 2
    assert len(text.prompts) == 2 and len(vision.calls) == 2 and len(renderer.recipes) == 2
    assert text.systems[0] == (b.pack / "prompt.md").read_text(encoding="utf-8").strip()
    assert "Description:" in vision.calls[0][1] and DESCRIPTION in vision.calls[0][1]
    assert build.recipe is not None
    assert build.recipe.content_hash == build.iterations[1].recipe_hash


def test_budget_spent_escalates_with_the_best_recipe_kept(tmp_path: Path) -> None:
    text = FakeText([answer("a"), answer("b"), answer("c")])
    vision = FakeVision({"a1-c0": 0.3, "a2-c0": 0.5, "a3-c0": 0.4})
    b, _ = builder(tmp_path, text, vision, budget=2)
    build = b.build(brief())
    assert build.status == "escalated" and build.attempts == 3
    assert [it.kept for it in build.iterations] == [True, True, False]
    assert build.recipe is not None
    assert build.recipe.content_hash == build.iterations[1].recipe_hash
    assert build.render is not None and build.render.path.endswith("a2-c0/frame.png")


def test_best_of_n_keeps_the_highest_scoring_candidate(tmp_path: Path) -> None:
    text = FakeText([answer("a"), answer("b"), answer("c")])
    vision = FakeVision({"a1-c0": 0.2, "a1-c1": 0.55, "a1-c2": 0.5})
    b, _ = builder(tmp_path, text, vision, budget=0, n=3)
    build = b.build(brief())
    assert len(text.prompts) == 3 and len(vision.calls) == 3
    assert [(it.candidate, it.kept) for it in build.iterations] == [
        (0, False),
        (1, True),
        (2, False),
    ]
    assert build.recipe is not None
    assert build.recipe.content_hash == build.iterations[1].recipe_hash


def test_window_notes_and_best_recipe_reach_the_next_prompt(tmp_path: Path) -> None:
    text = FakeText([answer("a"), answer("b")])
    vision = FakeVision({"a2-c0": 0.9}, notes="move the key behind the stack")
    b, _ = builder(tmp_path, text, vision, budget=2)
    b.build(brief())
    first, second = text.prompts
    assert "Best recipe so far" not in first and "Recent iterations" not in first
    assert "Best recipe so far" in second and "Recent iterations" in second
    assert "move the key behind the stack" in second
    assert str(tmp_path / "work" / "one-beam" / "a1-c0" / "frame.png") in second
    assert '"name": "a"' in second  # the previous full recipe
    for tool in ("scene.clear", "light.add", "text.add"):
        assert tool in first
    assert "inspect.bounds" not in first and "preview.render" not in first
    assert "#00c2a8" in first and "clear glass" in first and '"ONE BEAM"' in first


# ---- R7: every iteration on disk and in the record -----------------------------------


def test_every_candidate_is_stored_and_replayable(tmp_path: Path) -> None:
    text = FakeText([answer("a"), answer("b")])
    vision = FakeVision({"a1-c1": 0.7})
    b, _ = builder(tmp_path, text, vision, budget=1, n=2)
    build = b.build(brief())
    assert len(build.iterations) == 2
    for it in build.iterations:
        folder = tmp_path / "work" / "one-beam" / f"a{it.attempt}-c{it.candidate}"
        stored = Recipe.model_validate_json((folder / "recipe.json").read_text("utf-8"))
        assert stored.content_hash == it.recipe_hash
        assert Recipe.model_validate(it.recipe).content_hash == it.recipe_hash
        assert (folder / "frame.png").is_file()
        assert it.render is not None and it.render.path == str(folder / "frame.png")
        assert it.notes and it.scores and it.plan
    again = SceneBuild.model_validate_json(build.model_dump_json())
    assert again == build


# ---- R12: copy guard -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "allowed", "kit"),
    [
        ("BUY NOW", None, None),
        ("Revolutionary beam", ("Revolutionary beam",), KIT),
    ],
)
def test_text_copy_outside_the_brief_or_forbidden_is_refused(
    tmp_path: Path, body: str, allowed: tuple[str, ...] | None, kit: BrandKit | None
) -> None:
    text = FakeText([answer("a", body)])
    vision = FakeVision({})
    b, renderer = builder(tmp_path, text, vision, budget=0, kit=kit)
    build = b.build(brief(allowed_copy=allowed, brand_kit=kit))
    assert renderer.recipes == [] and vision.calls == []
    (it,) = build.iterations
    assert not it.passed and "failed candidate" in it.notes
    calls_json = json.loads((tmp_path / "work/one-beam/a1-c0/calls.json").read_text("utf-8"))
    assert calls_json["error"]


def test_brief_copy_defaults_to_quoted_strings_and_passes() -> None:
    assert brief().copy_lines() == ("ONE BEAM",)


# ---- R3: verifier prompt ---------------------------------------------------------------


def test_verifier_prompt_covers_adherence_craft_palette_and_look() -> None:
    text = scene_verifier_prompt(
        DESCRIPTION, ART, brand_kit=KIT, rubric="## adherence (weight 2.0)", anchors=("One Beam",)
    )
    for part in (DESCRIPTION, "object, material, light and camera", "composition", "#00c2a8"):
        assert part in text
    assert "#ff5500" in text and "revolutionary" in text and "Look clean" in text
    assert "## adherence" in text and "One Beam" in text and "revisions" in text


# ---- R8: ledger -------------------------------------------------------------------------


def test_runner_in_a_graph_records_gpu_minutes_and_ai_usage(tmp_path: Path) -> None:
    text = FakeText([answer("a"), answer("b")])
    vision = FakeVision({"a2-c0": 0.9})
    b, _ = builder(tmp_path, text, vision, budget=2)
    scene = scene_build_runner(b)

    def runner(ctx: RunContext) -> RunResult:
        if ctx.spec.kind == "art_direction":
            return RunResult(output=ART.model_dump(mode="json"))
        return scene(ctx)

    graph = ProductionGraph("p", profile=profile(2))
    graph.add_node(NodeSpec(id="art", kind="art_direction", department="art-director"))
    graph.add_node(
        NodeSpec(
            id="hero",
            kind=SCENE_BUILD,
            department="scene-builder",
            inputs=("art",),
            params={"description": DESCRIPTION},
        )
    )
    graph.run(runner)
    build = SceneBuild.model_validate(graph.output("hero"))
    assert build.status == "accepted" and len(build.iterations) == 2
    totals = graph.ledger.totals("hero")
    assert totals.gpu_minutes == pytest.approx(1.0)
    assert totals.ai_usage["scene_builder_claude_requests"] == 2
    assert totals.ai_usage["scene_builder_claude_input_tokens"] == 80
    assert totals.ai_usage["critic_requests"] == 2
    assert totals.ai_usage["critic_input_tokens"] == 200


# ---- #328: validation errors go back to the builder for bounded repair turns -----------


def rigless_distance() -> JsonValue:
    """The live failure: ``distance`` on a single light (it applies only with ``rig``)."""
    bad = calls("key")
    bad[3] = {"tool": "light.add", "args": {"kind": "area", "name": "key", "distance": 4}}
    return {"plan": "slab stack, key at 4 m", "calls": bad}


def test_invalid_tool_argument_triggers_a_repair_turn_and_the_frame_renders(
    tmp_path: Path,
) -> None:
    text = FakeText([rigless_distance(), answer("key")])
    vision = FakeVision({"a1-c0": 0.9})
    b, renderer = builder(tmp_path, text, vision, budget=0)
    build = b.build(brief())
    assert build.status == "accepted" and build.attempts == 1
    assert build.recipe is not None and len(renderer.recipes) == 1
    assert len(text.prompts) == 2 and len(vision.calls) == 1
    repair_prompt = text.prompts[1]
    assert "calls[3] light.add" in repair_prompt and "apply only with rig" in repair_prompt
    assert repair_prompt.startswith(text.prompts[0])
    (it,) = build.iterations
    assert it.passed and it.recipe is not None
    (repair,) = it.repairs
    assert repair.round == 1
    assert repair.errors == [
        "calls[3] light.add: args: Value error, ['distance'] apply only with rig"
    ]
    assert build.usage.ai_usage["scene_builder_claude_requests"] == 2
    folder = tmp_path / "work/one-beam/a1-c0"
    assert json.loads((folder / "repair-1.json").read_text("utf-8"))["errors"] == repair.errors
    assert json.loads((folder / "calls.json").read_text("utf-8"))["error"] == ""
    node = build.model_dump(mode="json")
    assert node["iterations"][0]["repairs"][0]["errors"] == repair.errors


def test_repairs_are_bounded_then_the_candidate_fails(tmp_path: Path) -> None:
    text = FakeText([rigless_distance()])
    vision = FakeVision({})
    b, renderer = builder(tmp_path, text, vision, budget=0)
    build = b.build(brief())
    assert len(text.prompts) == 1 + REPAIR_BUDGET
    assert renderer.recipes == [] and vision.calls == []
    assert build.status == "escalated" and build.recipe is None
    (it,) = build.iterations
    assert [r.round for r in it.repairs] == list(range(1, REPAIR_BUDGET + 1))
    assert it.notes.startswith("failed candidate:") and "apply only with rig" in it.notes


def test_repair_turns_do_not_use_revise_attempts(tmp_path: Path) -> None:
    text = FakeText([rigless_distance(), rigless_distance(), answer("a"), answer("b")])
    vision = FakeVision({"a1-c0": 0.3, "a2-c0": 0.9})
    b, renderer = builder(tmp_path, text, vision, budget=1)
    build = b.build(brief())
    assert build.status == "accepted" and build.attempts == 2
    assert len(renderer.recipes) == 2
    assert [len(it.repairs) for it in build.iterations] == [2, 0]


def test_compile_calls_reports_every_invalid_call() -> None:
    bad = calls("key")
    bad[3] = {"tool": "light.add", "args": {"name": "key", "distance": 4}}
    bad.append({"tool": "inspect.bounds", "args": {"object": "slab"}})
    with pytest.raises(HarnessError) as info:
        compile_calls(bad, recipe_id="r", shot_id="s")
    problems = info.value.details["problems"]
    assert [(p["index"], p["tool"]) for p in problems] == [
        (3, "light.add"),
        (len(bad) - 1, "inspect.bounds"),
    ]
    assert "calls[3] light.add: " in str(info.value)


def test_tool_cheat_sheet_matches_the_harness_schemas() -> None:
    sheet = tool_cheat_sheet()
    assert (PACK / CHEAT_SHEET).read_text(encoding="utf-8") == sheet, (
        "regenerate skills/scene-builder/tools.md from tool_cheat_sheet()"
    )
    for tool in build_tools():
        assert f"## {tool.name}\n" in sheet
        for arg in tool.schema().get("properties", {}):
            assert f"`{arg}`" in sheet
    light = sheet.split("## light.add\n", 1)[1].split("\n## ", 1)[0]
    assert "Either one light or a preset rig" in light
    assert "- `distance` (optional): number (> 0, <= 1000). rig light distance" in light
    text = FakeText([answer()])
    b, _ = builder(Path("/nonexistent"), text, FakeVision({}))
    request = RevisionRequest(attempt=1, candidate=0, state=None, notes="", window=())
    assert sheet in b.prompt(brief(), request)
