# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scene builder: a style-frame description to a farm-rendered harness recipe (VIGA).

One build composes :class:`~spectracut.critique.revise.ReviseLoop` and
:class:`~spectracut.critique.critic.StudioCritic`:

1. **plan and generate**: one :meth:`TextProvider.complete` call per candidate
   returns a plan (objects, materials, lights, camera, Look) and harness
   *build*-tool calls; :func:`compile_calls` turns them into a :class:`Recipe`
   through the same typed tool models the MCP server uses. Query, meta and
   unknown tools are refused, and there is no free-form code field, so a host
   only ever gets declarative steps for its bpy runtime;
2. **render**: a :data:`RecipeRenderer` (the farm's
   :class:`~spectracut.studio.departments.style_frame_farm.FarmRecipeRenderer`)
   renders the recipe on a render host. Blender never runs in the control plane;
3. **verify**: the Critic scores the frame through its :class:`VisionJudge`, a
   separate model from the builder's :class:`TextProvider`;
4. **revise**: the loop hands the best recipe so far, the Critic's notes and a
   sliding window back to the builder; best-of-N, undo, the iteration budget and
   stop-on-pass come from the quality profile.

Every candidate's recipe and frame are kept under ``workdir/<id>/a<attempt>-c<n>/``
and in :attr:`SceneBuild.iterations`. :func:`scene_build_runner` adapts a builder
to the production graph so the ledger records its GPU minutes and AI usage.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field, JsonValue

from spectracut.critique.critic import OVERALL, StudioCritic
from spectracut.critique.revise import ReviseLoop, RevisionRequest
from spectracut.harness.dispatch import PreviewDispatcher
from spectracut.harness.errors import HarnessError
from spectracut.harness.recorder import Recorder
from spectracut.harness.tools import Session, build_tools, get_tool
from spectracut.providers.base import TextProvider
from spectracut.skills import pack_dir
from spectracut.spec._base import NonEmptyStr, Slug, StrictModel, content_hash
from spectracut.spec.brand import BrandKit
from spectracut.spec.recipe import Recipe
from spectracut.storyboard.models import ArtDirection, ImageRecord
from spectracut.studio.gates import CriticVerdict
from spectracut.studio.graph import RunContext, Runner, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import NodeSpec
from spectracut.studio.profiles import QualityProfile
from spectracut.templates.base import TemplateError, require_clean_copy

SCENE_BUILD = "scene_build"
"""Spec kind (quality-profile stage) of a scene build."""

DEPARTMENT = "scene-builder"
PACK = pack_dir(DEPARTMENT)
METRIC_PREFIX = "scene_builder_"
"""Prefix of the builder's own AI-usage metrics (the Critic's are ``critic_*``)."""

RecipeRenderer = Callable[[Recipe, Path], tuple[ImageRecord, Usage]]
"""``render(recipe, path) -> (image, usage)``: render ``recipe``'s first frame to ``path``."""

CALLS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "plan": {"type": "string"},
        "calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"tool": {"type": "string"}, "args": {"type": "object"}},
                "required": ["tool", "args"],
            },
        },
    },
    "required": ["plan", "calls"],
}

_QUOTED = re.compile(r"\"([^\"]+)\"|“([^”]+)”")


class CopyRefused(ValueError):
    """A ``text.add`` body is not brief-sourced copy, or uses a forbidden word."""


# --------------------------------------------------------------------------- compile


class _NoDispatcher:
    """Build tools never dispatch; any use is a bug, not a silent preview job."""

    def __getattr__(self, name: str) -> Any:
        raise HarnessError("host.refused", f"scene builder compiles build tools only ({name})")


def compile_calls(calls: JsonValue, *, recipe_id: str, shot_id: str) -> Recipe:
    """Compile harness ``[{tool, args}]`` calls into a declarative :class:`Recipe`.

    Only ``build`` tools are accepted; their typed arguments are validated by
    :meth:`HarnessTool.parse` and recorded by the tool's own handler. A query,
    meta or unknown tool, a malformed call or bad arguments raise
    :class:`HarnessError`.
    """
    if not isinstance(calls, list) or not calls:
        raise HarnessError("recipe.empty", "calls must be a non-empty list of {tool, args}")
    recorder = Recorder(recipe_id=recipe_id, shot_id=shot_id)
    session = Session(recorder=recorder, dispatcher=cast(PreviewDispatcher, _NoDispatcher()))
    for i, call in enumerate(calls):
        if not isinstance(call, Mapping) or set(call) - {"tool", "args"}:
            raise HarnessError("args.invalid", f"calls[{i}]: expected exactly {{tool, args}}")
        name, args = call.get("tool"), call.get("args", {})
        if not isinstance(name, str):
            raise HarnessError("args.invalid", f"calls[{i}]: tool must be a string")
        if args is None:
            args = {}
        if not isinstance(args, Mapping):
            raise HarnessError("args.invalid", f"calls[{i}] {name}: args must be an object")
        tool = get_tool(name)
        if tool.kind != "build":
            raise HarnessError(
                "host.refused", f"calls[{i}]: {name} is a {tool.kind} tool; only build tools"
            )
        tool.handler(session, tool.parse(args))
    return recorder.recipe()


def check_copy(recipe: Recipe, allowed: Sequence[str], kit: BrandKit | None) -> None:
    """Every ``text.add`` body is allowed copy and free of the kit's forbidden words."""
    allowed_set = {a.strip() for a in allowed}
    for step in recipe.steps:
        if step.tool != "text.add":
            continue
        body = str(step.args.get("body", "")).strip()
        if body not in allowed_set:
            raise CopyRefused(
                f"text.add {body!r} is not brief copy; allowed: {sorted(allowed_set) or 'none'}"
            )
        if kit is not None:
            try:
                require_clean_copy(kit, body)
            except TemplateError as exc:
                raise CopyRefused(str(exc)) from None


# --------------------------------------------------------------------------- records


class SceneBrief(StrictModel):
    """What one build is for: the description, the art direction and the allowed copy."""

    id: Slug
    description: NonEmptyStr
    art: ArtDirection
    brand_kit: BrandKit | None = None
    allowed_copy: tuple[NonEmptyStr, ...] | None = None
    """On-screen strings sourced from the brief; ``None`` takes the description's quotes."""

    def copy_lines(self) -> tuple[str, ...]:
        if self.allowed_copy is not None:
            return self.allowed_copy
        return tuple(a or b for a, b in _QUOTED.findall(self.description))

    def palette(self) -> dict[str, str]:
        colours = {f"art.{k}": v for k, v in self.art.palette.swatches.items()}
        if self.brand_kit is not None:
            colours.update({f"kit.{k}": v for k, v in self.brand_kit.palette.swatches.items()})
        return colours


class SceneIteration(StrictModel):
    """One candidate as built, rendered and judged."""

    attempt: int = Field(ge=1)
    candidate: int = Field(ge=0)
    plan: str = ""
    recipe: dict[str, JsonValue] | None = None
    """The full :class:`Recipe` JSON; ``None`` when the calls failed to compile."""
    recipe_hash: str | None = None
    render: ImageRecord | None = None
    passed: bool
    scores: dict[str, float] = Field(default_factory=dict)
    notes: str = ""
    kept: bool = False
    """The attempt's winner and it became the best so far."""


class SceneBuild(StrictModel):
    """The outcome of one build: the best recipe and frame, and the whole trail."""

    id: Slug
    status: Literal["accepted", "escalated"]
    attempts: int = Field(ge=1)
    recipe: Recipe | None = None
    render: ImageRecord | None = None
    notes: str = ""
    iterations: list[SceneIteration] = Field(default_factory=list)
    usage: Usage = Usage()


# --------------------------------------------------------------------------- critic


class _BuildCritic(StudioCritic):
    """A per-build copy of the Critic that remembers each first verdict (with its usage).

    Candidates that never rendered (``{"error": ...}``) score 0 with the error as notes.
    """

    def __init__(self, inner: StudioCritic) -> None:
        fallback = inner.text_judge

        def failed(spec: NodeSpec, prompt: str, output: JsonValue) -> CriticVerdict:
            if isinstance(output, Mapping) and output.get("error"):
                return CriticVerdict(
                    passed=False,
                    scores={OVERALL: 0.0},
                    comment=f"failed candidate: {output['error']}",
                )
            if fallback is not None:
                return fallback(spec, prompt, output)
            return CriticVerdict(passed=False, scores={OVERALL: 0.0}, comment="no frame rendered")

        super().__init__(
            inner.vision,
            rubric_for=inner.rubric_for,
            anchors_for=inner.anchors_for,
            brief=inner.brief,
            canon=inner.canon,
            text_judge=failed,
            pairwise=inner.pairwise,
            profile=inner.profile,
            settings=inner.settings,
            extract=inner.extract,
        )
        self.verdicts: dict[str, CriticVerdict] = {}

    def review(self, spec: NodeSpec, output: JsonValue) -> CriticVerdict:
        verdict = super().review(spec, output)
        self.verdicts.setdefault(content_hash(output), verdict)
        return verdict


# --------------------------------------------------------------------------- builder


@dataclass
class _Candidate:
    attempt: int
    candidate: int
    plan: str = ""
    recipe: Recipe | None = None
    error: str = ""
    render: ImageRecord | None = None
    output: JsonValue = None
    usage: Usage = field(default_factory=Usage)


def _add(a: Usage, b: Usage) -> Usage:
    ai = dict(a.ai_usage)
    for k, v in b.ai_usage.items():
        ai[k] = ai.get(k, 0) + v
    return Usage(
        gpu_minutes=a.gpu_minutes + b.gpu_minutes,
        ai_usage=ai,
        elevenlabs_characters=a.elevenlabs_characters + b.elevenlabs_characters,
    )


def _prefixed(usage: Mapping[str, int]) -> Usage:
    return Usage(
        ai_usage={
            (k if k.startswith(METRIC_PREFIX) else f"{METRIC_PREFIX}{k}"): int(v)
            for k, v in usage.items()
        }
    )


def _dumps(value: object) -> str:
    return json.dumps(value, indent=1, sort_keys=True, default=str)


class SceneBuilder:
    """Builds one scene from a :class:`SceneBrief` through the revise loop."""

    def __init__(
        self,
        text: TextProvider,
        renderer: RecipeRenderer,
        critic: StudioCritic,
        profile: QualityProfile,
        workdir: Path,
        *,
        pack: Path = PACK,
    ) -> None:
        self.text = text
        self.renderer = renderer
        self.critic = critic
        self.profile = profile
        self.workdir = Path(workdir)
        self.pack = Path(pack)

    def system_prompt(self) -> str:
        return (self.pack / "prompt.md").read_text(encoding="utf-8").strip()

    def prompt(self, brief: SceneBrief, request: RevisionRequest) -> str:
        """The generate prompt: brief, art direction, tools, best so far, notes, window."""
        tools = {t.name: {"summary": t.summary, "args": t.schema()} for t in build_tools()}
        state = request.state if isinstance(request.state, Mapping) else None
        parts = [
            f"Scene {brief.id}, attempt {request.attempt}, candidate {request.candidate}.",
            "",
            "Description:",
            brief.description,
            "",
            "Palette (name: hex):",
            _dumps(brief.palette()),
            "",
            "Materials:",
            _dumps(list(brief.art.materials)),
            "",
            "Look:",
            brief.art.look.model_dump_json(),
            "",
            "Allowed on-screen copy (text.add body must be one of these, verbatim):",
            _dumps(list(brief.copy_lines())),
        ]
        if brief.brand_kit is not None and brief.brand_kit.forbidden_words:
            parts += ["", "Forbidden words:", _dumps(brief.brand_kit.forbidden_words)]
        parts += ["", "Build tools (name: summary and JSON args schema):", _dumps(tools)]
        if state is not None:
            parts += [
                "",
                "Best recipe so far (the full state; emit the whole scene again):",
                _dumps(state.get("recipe")),
                "",
                "Critic notes on it:",
                request.notes or "(none)",
            ]
        if request.window:
            parts += ["", "Recent iterations (oldest first):"]
            for it in request.window:
                verdict: dict[str, object] = {
                    "passed": it.verdict.passed,
                    "scores": it.verdict.scores,
                    "notes": it.verdict.comment,
                }
                entry: dict[str, object] = {
                    "attempt": it.attempt,
                    "recipe": it.recipe,
                    "diff": it.diff,
                    "frames": [str(p) for p in it.frames],
                    "verdict": verdict,
                    "kept": it.kept,
                }
                parts.append(_dumps(entry))
        parts += ["", 'Answer as JSON: {"plan": "...", "calls": [{"tool": ..., "args": {...}}]}.']
        return "\n".join(parts)

    def build(self, brief: SceneBrief) -> SceneBuild:
        root = self.workdir / brief.id
        spec = NodeSpec(id=brief.id, kind=SCENE_BUILD, department=DEPARTMENT)
        critic = _BuildCritic(self.critic)
        system = self.system_prompt()
        candidates: dict[tuple[int, int], _Candidate] = {}

        def generate(request: RevisionRequest) -> JsonValue:
            cand = _Candidate(attempt=request.attempt, candidate=request.candidate)
            candidates[(cand.attempt, cand.candidate)] = cand
            completion = self.text.complete(
                system, self.prompt(brief, request), schema=CALLS_SCHEMA
            )
            cand.usage = _prefixed(completion.usage)
            out = completion.output
            calls: JsonValue = None
            if isinstance(out, Mapping):
                cand.plan = str(out.get("plan") or "")
                calls = out.get("calls")
            try:
                recipe = compile_calls(
                    calls,
                    recipe_id=f"{brief.id}-a{cand.attempt}-c{cand.candidate}"[:128],
                    shot_id=brief.id,
                )
                check_copy(recipe, brief.copy_lines(), brief.brand_kit)
                cand.recipe = recipe
            except (HarnessError, CopyRefused) as exc:
                cand.error = str(exc)
            folder = root / f"a{cand.attempt}-c{cand.candidate}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "calls.json").write_text(
                _dumps({"plan": cand.plan, "calls": calls, "error": cand.error}) + "\n",
                encoding="utf-8",
            )
            recipe_json: JsonValue = None
            if cand.recipe is not None:
                (folder / "recipe.json").write_text(
                    cand.recipe.model_dump_json(indent=2) + "\n", encoding="utf-8"
                )
                recipe_json = cand.recipe.model_dump(mode="json")
            return {
                "attempt": cand.attempt,
                "candidate": cand.candidate,
                "plan": cand.plan,
                "recipe": recipe_json,
                "error": cand.error or None,
            }

        def render(state: JsonValue) -> JsonValue:
            assert isinstance(state, Mapping)
            cand = candidates[(cast(int, state["attempt"]), cast(int, state["candidate"]))]
            if cand.recipe is None:
                cand.output = {"frames": [], "error": cand.error, "candidate": dict(state)}
                return cand.output
            frame = root / f"a{cand.attempt}-c{cand.candidate}" / "frame.png"
            record, used = self.renderer(cand.recipe, frame)
            cand.render = record
            cand.usage = _add(cand.usage, used)
            cand.output = {"frames": [record.path]}
            return cand.output

        loop = ReviseLoop.from_profile(generate, render, critic, spec, self.profile)
        outcome = loop.run()

        kept = {
            (cast(int, it.recipe["attempt"]), cast(int, it.recipe["candidate"]))
            for it in outcome.history
            if it.kept and isinstance(it.recipe, Mapping)
        }
        iterations: list[SceneIteration] = []
        total = Usage()
        for key in sorted(candidates):
            cand = candidates[key]
            verdict = critic.verdicts.get(content_hash(cand.output))
            total = _add(total, cand.usage)
            if verdict is not None:
                total = _add(total, Usage(ai_usage=dict(verdict.usage)))
            iterations.append(
                SceneIteration(
                    attempt=cand.attempt,
                    candidate=cand.candidate,
                    plan=cand.plan,
                    recipe=cand.recipe.model_dump(mode="json") if cand.recipe else None,
                    recipe_hash=cand.recipe.content_hash if cand.recipe else None,
                    render=cand.render,
                    passed=verdict.passed if verdict else False,
                    scores=dict(verdict.scores) if verdict else {},
                    notes=verdict.comment if verdict else cand.error,
                    kept=key in kept,
                )
            )
        best = outcome.recipe if isinstance(outcome.recipe, Mapping) else {}
        best_cand = candidates.get(
            (cast(int, best.get("attempt", 0)), cast(int, best.get("candidate", 0)))
        )
        return SceneBuild(
            id=brief.id,
            status=outcome.status,
            attempts=outcome.attempts,
            recipe=best_cand.recipe if best_cand else None,
            render=best_cand.render if best_cand else None,
            notes=outcome.notes,
            iterations=iterations,
            usage=total,
        )


# --------------------------------------------------------------------------- graph


def _art_direction(inputs: Mapping[str, JsonValue]) -> ArtDirection:
    value = inputs.get("art_direction")
    if value is not None:
        return ArtDirection.model_validate(value)
    for candidate in inputs.values():
        try:
            return ArtDirection.model_validate(candidate)
        except ValueError:
            continue
    raise ValueError("scene build needs an art_direction input")


def scene_build_runner(builder: SceneBuilder) -> Runner:
    """A graph ``Runner``: params ``description`` (and ``brand_kit``, ``copy``), input art.

    The output is the :class:`SceneBuild` JSON and the usage is the build's summed
    usage, so the graph's ledger entry records GPU minutes and AI usage.
    """

    def run(ctx: RunContext) -> RunResult:
        params = ctx.spec.params
        kit = params.get("brand_kit")
        copy = params.get("copy")
        brief = SceneBrief(
            id=ctx.spec.id,
            description=str(params.get("description") or ""),
            art=_art_direction(ctx.inputs),
            brand_kit=BrandKit.model_validate(kit) if kit is not None else None,
            allowed_copy=tuple(str(c) for c in copy) if isinstance(copy, list) else None,
        )
        build = builder.build(brief)
        output: JsonValue = build.model_dump(mode="json")
        return RunResult(output=output, usage=build.usage)

    return run


__all__ = [
    "CALLS_SCHEMA",
    "SCENE_BUILD",
    "CopyRefused",
    "RecipeRenderer",
    "SceneBrief",
    "SceneBuild",
    "SceneBuilder",
    "SceneIteration",
    "check_copy",
    "compile_calls",
    "scene_build_runner",
]
