# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pre-production (#29): art direction, style frames, storyboard and the animatic.

Runs the art departments through the real graph and department runner with
``MockTextProvider`` answering as each department, a mock image provider and a
mock style-frame renderer (the farm renders Blender; the real check is
``tests/acceptance/test_m2_preproduction.py``).
"""

from __future__ import annotations

from math import ceil
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import JsonValue

from spectracut.audio.vo import VoiceoverLine, VoiceoverTrack
from spectracut.edit.timeline import Edit
from spectracut.providers.base import ProviderError
from spectracut.providers.local.registry import MockLocalProvider
from spectracut.providers.mock_image import MockImageProvider
from spectracut.providers.mock_text import MockTextProvider
from spectracut.storyboard.animatic import (
    TempAudio,
    build_animatic,
    check_locked_timing,
    resolve_beats,
)
from spectracut.storyboard.explore import (
    ExploreProvider,
    choose_exploration_provider,
    record_image,
)
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
from spectracut.studio.department import DepartmentOutputError
from spectracut.studio.departments.art import (
    ART_DEPARTMENTS,
    PREPRODUCTION_GATES,
    PreproductionRunner,
    StoryboardArtist,
    preproduction_nodes,
)
from spectracut.studio.graph import RunContext, RunResult
from spectracut.studio.ledger import Usage
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


class _BoardRenderer:
    """Stands in for the farm at the preview tier: writes each board's PNG."""

    def __init__(self) -> None:
        self.frames: list[StyleFrame] = []

    def __call__(
        self, frame: StyleFrame, art: ArtDirection, path: Path
    ) -> tuple[ImageRecord, Usage]:
        self.frames.append(frame)
        MockImageProvider().generate(f"board {frame.id}", path)
        return record_image(path, "blender-farm"), Usage(gpu_minutes=0.5)


class _Preview:
    """Stands in for ffmpeg: writes a stub mp4 and keeps the edit."""

    def __init__(self) -> None:
        self.calls: list[tuple[Edit, Path]] = []

    def __call__(self, edit: Edit, out: Path) -> Path:
        out.write_bytes(b"mp4")
        self.calls.append((edit, out))
        return out


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
        board_renderer=_BoardRenderer(),
        preview=_Preview(),
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
    local = MockLocalProvider("image")
    chatgpt = cast(ExploreProvider, MockImageProvider())
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


# ---- the farm renderer's GPU minutes; optional explorer and voice (#271) -----


class _UsageRenderer:
    """Returns the render with the GPU minutes it used, and takes frames with no concept."""

    def __init__(self) -> None:
        self.concepts: list[ImageRecord | None] = []

    def __call__(
        self, frame: StyleFrame, art: ArtDirection, path: Path
    ) -> tuple[ImageRecord, Usage]:
        self.concepts.append(frame.concept)
        MockImageProvider().generate(f"render {frame.id}", path)
        return record_image(path, "blender-farm"), Usage(gpu_minutes=2.0)


def _runner(
    tmp: Path,
    *,
    explorer: Any,
    renderer: Any,
    voice: Any,
    board_renderer: Any = None,
    preview: Any = None,
) -> Any:
    inner = DepartmentRunner(ART_DEPARTMENTS, MockTextProvider(respond=_respond))
    return PreproductionRunner(
        inner,
        {d.name: d for d in ART_DEPARTMENTS},
        explorer=explorer,
        renderer=renderer,
        voice=voice,
        workdir=tmp / "work",
        board_renderer=board_renderer or _BoardRenderer(),
        preview=preview or _Preview(),
    )


def _ctx(node: str, inputs: dict[str, Any]) -> RunContext:
    spec = {n.id: n for n in preproduction_nodes()}[node]
    return RunContext(spec=spec, tier=Tier.FINAL, key="k", inputs=inputs, attempt=1)


def test_style_frames_sum_the_renderers_gpu_minutes_into_the_node_usage(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path, explorer=MockLocalProvider("image"), renderer=_UsageRenderer(), voice=_voice
    )
    result = runner(_ctx("style_frames", {"brief": BRIEF_OUT, "art_direction": ART}))
    assert result.usage.gpu_minutes == 2.0 * len(FRAMES["frames"])
    assert result.usage.ai_usage["image_exploration_requests"] == len(FRAMES["frames"])


def test_style_frames_with_no_explorer_render_without_a_concept(tmp_path: Path) -> None:
    renderer = _UsageRenderer()
    runner = _runner(tmp_path, explorer=None, renderer=renderer, voice=_voice)
    result = runner(_ctx("style_frames", {"brief": BRIEF_OUT, "art_direction": ART}))
    frames = StyleFrames.model_validate(result.output)
    assert renderer.concepts == [None, None]
    assert all(f.concept is None and f.render is not None for f in frames.frames)
    assert "image_exploration_requests" not in result.usage.ai_usage


def test_animatic_with_no_voice_and_no_voiceover_input_is_an_error(tmp_path: Path) -> None:
    runner = _runner(tmp_path, explorer=None, renderer=_UsageRenderer(), voice=None)
    with pytest.raises(ValueError, match="animatic needs temp VO"):
        runner(_ctx("animatic", {"script": SCRIPT, "storyboard": BOARDS, "art_direction": ART}))


def test_animatic_with_an_unvoiced_script_needs_no_voice(tmp_path: Path) -> None:
    runner = _runner(tmp_path, explorer=None, renderer=_UsageRenderer(), voice=None)
    quiet = {**SCRIPT, "lines": [{"beat": b} for b in ("hook", "proof", "cta")]}
    result = runner(_ctx("animatic", {"script": quiet, "storyboard": BOARDS, "art_direction": ART}))
    edit = Edit.model_validate(result.output)
    assert check_locked_timing(edit, Storyboard.model_validate(BOARDS)) == []
    assert all(not t.clips() for t in edit.tracks if t.id == "vo")


def test_animatic_needs_art_direction_to_render_its_boards(tmp_path: Path) -> None:
    runner = _runner(tmp_path, explorer=None, renderer=_UsageRenderer(), voice=_voice)
    with pytest.raises(ValueError, match="art_direction"):
        runner(_ctx("animatic", {"script": SCRIPT, "storyboard": BOARDS}))


def test_animatic_fails_on_a_voiced_beat_with_no_board(tmp_path: Path) -> None:
    runner = _runner(tmp_path, explorer=None, renderer=_UsageRenderer(), voice=_voice)
    boards = {"boards": [b for b in BOARDS["boards"] if b["beat"] != "proof"]}
    with pytest.raises(ValueError, match=r"no board: \['proof'\]"):
        runner(_ctx("animatic", {"script": SCRIPT, "storyboard": boards, "art_direction": ART}))


def test_animatic_fails_when_the_preview_is_not_written(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        explorer=None,
        renderer=_UsageRenderer(),
        voice=_voice,
        preview=lambda edit, out: out,
    )
    with pytest.raises(ValueError, match="preview was not written"):
        runner(_ctx("animatic", {"script": SCRIPT, "storyboard": BOARDS, "art_direction": ART}))


# ---- #276: real VO durations, rendered boards, a playable preview -------------

LIVE_SCRIPT: dict[str, Any] = {
    "concept_id": "concept-1",
    "lines": [
        {"beat": "0:00\u20130:07 FRUSTRATION. Macro on a failing scraper", "vo": "Scrapers break."},
        {"beat": "0:07\u20130:12 RELIEF | the API call", "vo": "Send a URL. Get data."},
        {"beat": "0:12\u20130:15 CTA"},
    ],
}
LIVE_BOARDS: dict[str, Any] = {
    "boards": [
        {
            "shot_id": "b1",
            "beat": "0:00\u20130:03.5 FRUSTRATION (move in on the log)",
            "action": "Red errors scroll",
            "screen_content": "terminal",
            "duration_s": 3.5,
        },
        {
            "shot_id": "b2",
            "beat": "0:03.5\u20130:07 Frustration, wider",
            "action": "Developer sighs",
            "duration_s": 3.5,
        },
        {"shot_id": "b3", "beat": "0:08 relief", "action": "One curl call", "duration_s": 2.0},
        {"shot_id": "b4", "beat": "0:12\u20130:15 CTA", "action": "Logo", "duration_s": 3.0},
    ]
}


def _vo_track(tmp: Path, seconds: dict[str, float]) -> dict[str, Any]:
    lines = []
    for n, (beat, duration) in enumerate(seconds.items()):
        path = tmp / f"vo-{n}.wav"
        path.write_bytes(b"RIFF")
        lines.append(
            VoiceoverLine(
                id=f"line-{n}", beat=beat, text="line", media_ref=str(path), duration_s=duration
            )
        )
    return VoiceoverTrack(lines=lines, provider="local", temp_vo=True).model_dump(mode="json")


def test_animatic_has_real_vo_and_rendered_boards_when_beats_span_boards(tmp_path: Path) -> None:
    beats = [line["beat"] for line in LIVE_SCRIPT["lines"]]
    timing = {beats[0]: 6.2, beats[1]: 4.6}
    voice = _vo_track(tmp_path, timing)
    boards, preview = _BoardRenderer(), _Preview()
    runner = _runner(
        tmp_path,
        explorer=MockLocalProvider("image"),
        renderer=_UsageRenderer(),
        voice=None,
        board_renderer=boards,
        preview=preview,
    )
    ctx = _ctx(
        "animatic",
        {
            "script": LIVE_SCRIPT,
            "storyboard": LIVE_BOARDS,
            "voiceover": voice,
            "art_direction": ART,
        },
    )
    result = runner(ctx)
    edit = Edit.model_validate(result.output)
    fps = edit.rate.fps

    vo = next(t for t in edit.tracks if t.id == "vo")
    clips = vo.clips()
    assert [c.id for c in clips] == ["vo-b1", "vo-b3"]
    assert all(c.duration > 0 and Path(c.media_ref).is_file() for c in clips)
    assert abs(sum(float(c.duration / fps) for c in clips) - sum(timing.values())) <= 0.5
    for clip, seconds in zip(clips, timing.values(), strict=True):
        assert abs(float(clip.duration / fps) - seconds) <= 0.5
    # The 6.2 s FRUSTRATION line spans b1 and b2 instead of being cut at b1.
    assert vo.record_start("vo-b1") == 0 and clips[0].duration > ceil(3.5 * fps)

    picture = next(t for t in edit.tracks if t.id == "picture").clips()
    assert [c.shot_id for c in picture] == ["b1", "b2", "b3", "b4"]
    for clip in picture:
        assert Path(clip.media_ref).is_absolute() and Path(clip.media_ref).is_file()
        assert clip.media_ref == str(tmp_path / "work" / "boards" / f"{clip.shot_id}.png")
    assert [f.id for f in boards.frames] == ["b1", "b2", "b3", "b4"]
    assert boards.frames[0].prompt == "Red errors scroll terminal"
    assert all(f.concept is not None for f in boards.frames)
    assert {t.duration for t in edit.tracks} == {edit.duration}

    [(previewed, out)] = preview.calls
    assert previewed == edit and out == tmp_path / "work" / "animatic" / "animatic.mp4"
    assert out.is_file()
    assert result.usage.gpu_minutes == 0.5 * 4
    assert result.usage.ai_usage["image_exploration_requests"] == 4


def test_resolve_beats_matches_text_label_then_timecode() -> None:
    beats = [line["beat"] for line in LIVE_SCRIPT["lines"]]
    resolved = resolve_beats(Storyboard.model_validate(LIVE_BOARDS), beats)
    assert [b.beat for b in resolved.boards] == [beats[0], beats[0], beats[1], beats[2]]
    exact = Storyboard.model_validate(
        {"boards": [{"shot_id": "s1", "beat": "  HOOK ", "action": "a", "duration_s": 1.0}]}
    )
    assert resolve_beats(exact, ["hook", "hooked"]).boards[0].beat == "hook"
    loose = Storyboard.model_validate(
        {"boards": [{"shot_id": "s1", "beat": "0:30 outro", "action": "a", "duration_s": 1.0}]}
    )
    assert resolve_beats(loose, beats).boards[0].beat == ""
    timed = Storyboard.model_validate(
        {
            "boards": [
                {"shot_id": "s1", "beat": "0:13 logo sting", "action": "a", "duration_s": 1.0},
                {"shot_id": "s2", "beat": "3 cuts", "action": "a", "duration_s": 1.0},
            ]
        }
    )
    assert [b.beat for b in resolve_beats(timed, beats).boards] == [beats[2], ""]


def test_a_vo_shorter_than_its_beat_leaves_a_gap_and_a_longer_one_holds_the_last_board() -> None:
    sb = Storyboard.model_validate(
        {
            "boards": [
                {"shot_id": "a", "beat": "x", "action": "a", "duration_s": 1.0},
                {"shot_id": "b", "beat": "x", "action": "b", "duration_s": 1.0},
                {"shot_id": "c", "beat": "y", "action": "c", "duration_s": 1.0},
            ]
        }
    )
    edit = build_animatic("anim", sb, {"x": TempAudio("x.wav", 3.0), "y": TempAudio("y.wav", 0.5)})
    picture = [c.duration for c in edit.tracks[0].clips()]
    assert picture == [24, 48, 24]  # b holds 1 s more so the 3 s line is not cut
    vo = edit.tracks[1]
    assert [c.duration for c in vo.clips()] == [72, 12]
    assert vo.duration == edit.tracks[0].duration
    assert check_locked_timing(edit, sb) == []


def test_storyboard_check_output_names_loose_boards_and_unboarded_vo() -> None:
    kinds = {"script": "script"}
    StoryboardArtist.check_output("storyboard", LIVE_BOARDS, {"script": LIVE_SCRIPT}, kinds)
    bad: dict[str, Any] = {
        "boards": [
            {"shot_id": "b9", "beat": "0:30 outro", "action": "a", "duration_s": 1.0},
            LIVE_BOARDS["boards"][3],
        ]
    }
    with pytest.raises(DepartmentOutputError) as err:
        StoryboardArtist.check_output("storyboard", bad, {"script": LIVE_SCRIPT}, kinds)
    assert "b9='0:30 outro'" in str(err.value)
    assert "FRUSTRATION" in str(err.value) and "RELIEF" in str(err.value)


def test_preproduction_nodes_wire_the_voiceover_and_art_direction_into_the_animatic() -> None:
    nodes = {n.id: n for n in preproduction_nodes(voiceover_id="voiceover")}
    assert nodes["animatic"].inputs == ("script", "storyboard", "voiceover", "art_direction")
    assert {n.id: n for n in preproduction_nodes()}["animatic"].inputs == (
        "script",
        "storyboard",
        "art_direction",
    )
