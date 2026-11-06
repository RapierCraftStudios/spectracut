# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pre-production (#29): art direction, style frames, storyboard and the animatic.

Runs the art departments through the real graph and department runner with
``MockTextProvider`` answering as each department, a mock image provider and a
mock style-frame renderer (the farm renders Blender; the real check is
``tests/acceptance/test_m2_preproduction.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from spectracut.edit.timeline import Edit
from spectracut.providers.base import ProviderError
from spectracut.providers.local.registry import MockLocalProvider
from spectracut.providers.mock_image import MockImageProvider
from spectracut.providers.mock_text import MockTextProvider
from spectracut.storyboard.animatic import TempAudio, build_animatic, check_locked_timing
from spectracut.storyboard.explore import choose_exploration_provider, record_image
from spectracut.storyboard.models import (
    ArtDirection,
    ImageRecord,
    Storyboard,
    StyleFrame,
    StyleFrames,
)
from spectracut.studio import (
    CapacityLimits,
    DepartmentRunner,
    GateMode,
    GatePolicy,
    NodeSpec,
    NodeStatus,
    ProductionGraph,
)
from spectracut.studio.departments.art import (
    ART_DEPARTMENTS,
    PREPRODUCTION_GATES,
    PreproductionRunner,
    preproduction_nodes,
)
from spectracut.studio.graph import RunContext, RunResult
from spectracut.studio.node import Tier

SCRIPT: dict[str, Any] = {
    "concept_id": "concept-1",
    "lines": [
        {"beat": "hook", "vo": "Send a URL."},
        {"beat": "proof", "vo": "Get clean data back, rendered and parsed."},
        {"beat": "cta"},
    ],
}
ART: dict[str, Any] = {
    "mood_board": [{"ref": "clean studio product shot", "take": "soft key light"}],
    "palette": {"swatches": {"ink": "#101820", "signal": "#00c2a8"}},
    "materials": ["matte anodised aluminium"],
    "look": {"id": "clean"},
}
BOARDS: dict[str, Any] = {
    "boards": [
        {"shot_id": "s1", "beat": "hook", "action": "Cursor pastes a URL", "duration_s": 1.0},
        {"shot_id": "s2", "beat": "proof", "action": "Data fills the panel", "duration_s": 1.0},
        {"shot_id": "s3", "beat": "cta", "action": "Logo and link", "duration_s": 2.0},
    ]
}
FRAMES: dict[str, Any] = {
    "frames": [
        {"id": "hero", "description": "Dashboard hero", "prompt": "clean dashboard on a plinth"},
        {"id": "cta", "description": "End card", "prompt": "logo on dark ink"},
    ]
}
SOURCES = [{"id": "home", "title": "Home page", "ref": "https://alterlab.io/"}]
BRIEF_OUT: dict[str, Any] = {
    "id": "launch",
    "message": "Send a URL, get clean data back.",
    "audience": {"personas": [{"name": "Backend developer", "description": "Builds pipelines"}]},
    "sources": SOURCES,
    "proof_points": [{"claim": "Pages come back as markdown", "source_id": "home"}],
    "tone": "plain, technical",
    "success_measure": "API sign-ups",
    "cta": "Get an API key",
}


def _respond(system: str, prompt: str, schema: dict[str, Any]) -> JsonValue:
    return {"ArtDirection": ART, "StyleFrames": FRAMES, "Storyboard": BOARDS}[schema["title"]]


class _Stub:
    """Answers the upstream brief and script nodes; passes the art nodes on."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def __call__(self, ctx: RunContext) -> RunResult:
        if ctx.spec.kind == "script":
            return RunResult(output=SCRIPT)
        if ctx.spec.kind == "brief":
            return RunResult(output=BRIEF_OUT)
        return self.inner(ctx)  # type: ignore[no-any-return]


def _voice(text: str, path: Path) -> TempAudio:
    return TempAudio(media_ref=str(path), duration_s=0.25 * len(text.split()))


class _Renderer:
    def __init__(self, tmp: Path) -> None:
        self.tmp, self.calls = tmp, []  # type: ignore[var-annotated]

    def __call__(self, frame: StyleFrame, art: ArtDirection, path: Path) -> ImageRecord:
        assert frame.concept is not None, "the Blender frame starts from the concept image"
        self.calls.append(frame.id)
        MockImageProvider().generate(f"render {frame.id}", path)
        return record_image(path, "blender-farm")


def _studio(tmp: Path, **kw: Any) -> tuple[ProductionGraph, _Renderer, Any]:
    gates = {**PREPRODUCTION_GATES, "art_direction": GateMode.AUTO, "storyboard": GateMode.AUTO}
    graph = ProductionGraph("launch", gates=GatePolicy(defaults=gates))
    graph.add_nodes(
        [
            NodeSpec(id="brief", kind="brief", department="stub"),
            NodeSpec(id="script", kind="script", department="stub", inputs=("brief",)),
            *preproduction_nodes(),
        ]
    )
    inner = DepartmentRunner(
        ART_DEPARTMENTS, MockTextProvider(respond=_respond), limits=CapacityLimits(), graph=graph
    )
    renderer = _Renderer(tmp)
    runner = PreproductionRunner(
        inner,
        {d.name: d for d in ART_DEPARTMENTS},
        explorer=kw.get("explorer", MockLocalProvider("image")),
        renderer=renderer,
        voice=_voice,
        workdir=tmp / "work",
    )
    return graph, renderer, _Stub(runner)


# ---- AC: style frames rendered at final tier, approved at a human gate -------


def test_style_frames_render_at_final_tier_and_wait_at_the_human_gate(tmp_path: Path) -> None:
    graph, renderer, runner = _studio(tmp_path)
    report = graph.run(runner)
    assert report.failed == [], {n: graph.state(n).error for n in report.failed}
    assert graph.status("style_frames") is NodeStatus.AWAITING_APPROVAL
    assert graph.status("animatic") is not NodeStatus.CLEAN  # blocked behind the gate
    assert renderer.calls == ["hero", "cta"]
    frames = StyleFrames.model_validate(graph._pending["style_frames"][1].output)
    assert frames.rendered
    assert all(f.render and f.render.provider == "blender-farm" for f in frames.frames)
    assert all(f.concept and f.concept.provider == "mock-image" for f in frames.frames)


def test_style_frames_refuse_a_preview_tier(tmp_path: Path) -> None:
    graph, _, runner = _studio(tmp_path)
    spec = graph.spec("style_frames").model_copy(update={"tier": Tier.PREVIEW})
    ctx = RunContext(
        spec=spec,
        tier=Tier.PREVIEW,
        key="k",
        inputs={"brief": BRIEF_OUT, "art_direction": ART},
        attempt=1,
    )
    with pytest.raises(ValueError, match="final tier"):
        runner(ctx)


def test_preproduction_nodes_build_style_frames_at_final() -> None:
    nodes = {n.id: n for n in preproduction_nodes()}
    assert nodes["style_frames"].tier is Tier.FINAL
    assert PREPRODUCTION_GATES["style_frames"] is GateMode.HUMAN
    assert PREPRODUCTION_GATES["animatic"] is GateMode.HUMAN


# ---- AC: the animatic is an Edit with temp VO at locked timing ---------------


def test_animatic_is_an_edit_with_temp_vo_after_the_gates(tmp_path: Path) -> None:
    graph, _, runner = _studio(tmp_path)
    graph.run(runner)
    graph.approve("style_frames")
    graph.run(runner)
    assert graph.status("animatic") is NodeStatus.AWAITING_APPROVAL
    graph.approve("animatic")
    edit = Edit.model_validate(graph.output("animatic"))
    storyboard = Storyboard.model_validate(graph.output("storyboard"))
    assert check_locked_timing(edit, storyboard) == []
    vo = next(t for t in edit.tracks if t.id == "vo")
    assert [c.id for c in vo.clips()] == ["vo-s1", "vo-s2"]


def test_animatic_holds_a_board_for_a_vo_longer_than_planned() -> None:
    sb = Storyboard.model_validate(BOARDS)
    edit = build_animatic(
        "anim", sb, {"hook": TempAudio("vo.wav", 2.5)}, TempAudio("bed.wav", 60.0), fps=24
    )
    picture = edit.tracks[0]
    assert picture.clips()[0].duration == 60  # 2.5 s at 24 fps, not the planned 1 s
    assert edit.duration == 60 + 24 + 48
    assert check_locked_timing(edit, sb) == []
    assert {t.duration for t in edit.tracks} == {edit.duration}


def test_locked_timing_flags_reordered_boards() -> None:
    sb = Storyboard.model_validate(BOARDS)
    edit = build_animatic("anim", sb, {})
    swapped = Storyboard(boards=list(reversed(sb.boards)))
    assert any("shot order" in p for p in check_locked_timing(edit, swapped))


def test_storyboard_has_one_board_per_shot() -> None:
    bad = {"boards": [BOARDS["boards"][0], BOARDS["boards"][0]]}
    with pytest.raises(ValueError, match="one board per shot"):
        Storyboard.model_validate(bad)


# ---- AC: image exploration is local unless ChatGPT is configured -------------


def test_image_exploration_is_local_by_default_and_chatgpt_only_when_configured() -> None:
    local, chatgpt = MockLocalProvider("image"), MockImageProvider()
    assert choose_exploration_provider(None, local=local, chatgpt=chatgpt) is local
    assert choose_exploration_provider({}, local=local, chatgpt=chatgpt) is local
    assert choose_exploration_provider({"image_provider": "local"}, local=local) is local
    assert (
        choose_exploration_provider({"image_provider": "chatgpt"}, local=local, chatgpt=chatgpt)
        is chatgpt
    )


def test_chatgpt_without_a_provider_or_unknown_choice_is_an_error_not_a_fallback() -> None:
    local = MockLocalProvider("image")
    with pytest.raises(ProviderError, match="no ChatGPT provider"):
        choose_exploration_provider({"image_provider": "chatgpt"}, local=local)
    with pytest.raises(ProviderError, match="unknown image_provider"):
        choose_exploration_provider({"image_provider": "dalle"}, local=local)


def test_exploration_runs_on_the_local_provider(tmp_path: Path) -> None:
    graph, _, runner = _studio(tmp_path)
    graph.run(runner)
    frames = StyleFrames.model_validate(graph._pending["style_frames"][1].output)
    assert {f.concept.provider for f in frames.frames if f.concept} == {"mock-image"}
