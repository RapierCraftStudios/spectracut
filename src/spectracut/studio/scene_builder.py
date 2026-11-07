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
from spectracut.queue.queue import JobFailed
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
    :meth:`HarnessTool.parse` and recorded by the tool's own handler. Every call is
    validated before anything renders: a query, meta or unknown tool, a malformed
    call or bad arguments in *any* call raise one :class:`HarnessError` whose message
    lists each problem as ``calls[i] <tool>: <message>`` and whose ``details`` carry
    ``problems=[{index, tool, message}]``.
    """
    if not isinstance(calls, list) or not calls:
        raise HarnessError("recipe.empty", "calls must be a non-empty list of {tool, args}")
    recorder = Recorder(recipe_id=recipe_id, shot_id=shot_id)
    session = Session(recorder=recorder, dispatcher=cast(PreviewDispatcher, _NoDispatcher()))
    problems: list[dict[str, JsonValue]] = []
    codes: list[str] = []
    for i, call in enumerate(calls):
        name: str | None = None
        try:
            if not isinstance(call, Mapping) or set(call) - {"tool", "args"}:
                raise HarnessError("args.invalid", "expected exactly {tool, args}")
            tool_name, args = call.get("tool"), call.get("args", {})
            if not isinstance(tool_name, str):
                raise HarnessError("args.invalid", "tool must be a string")
            name = tool_name
            if args is None:
                args = {}
            if not isinstance(args, Mapping):
                raise HarnessError("args.invalid", "args must be an object")
            tool = get_tool(name)
            if tool.kind != "build":
                raise HarnessError(
                    "host.refused", f"{name} is a {tool.kind} tool; only build tools"
                )
            tool.handler(session, tool.parse(args))
        except HarnessError as exc:
            message = exc.message
            if name is not None and message.startswith(f"{name}: "):
                message = message[len(name) + 2 :]
            problems.append({"index": i, "tool": name, "message": message})
            codes.append(exc.code)
    if problems:
        raise HarnessError(codes[0], "; ".join(problem_lines(problems)), problems=problems)
    return recorder.recipe()


def problem_lines(problems: Sequence[Mapping[str, JsonValue]]) -> list[str]:
    """``calls[i] <tool>: <message>`` per problem from :func:`compile_calls`."""
    return [
        f"calls[{p.get('index')}]"
        + (f" {p['tool']}" if p.get("tool") else "")
        + f": {p.get('message')}"
        for p in problems
    ]


# --------------------------------------------------------------------------- cheat sheet

CHEAT_SHEET = "tools.md"
"""The checked-in :func:`tool_cheat_sheet` in the scene-builder skill pack."""


def _type_of(schema: Mapping[str, Any], defs: Mapping[str, Any]) -> str:
    if "$ref" in schema:
        name = str(schema["$ref"]).rsplit("/", 1)[-1]
        target = defs.get(name, {})
        if "enum" in target or "anyOf" in target or "type" in target and target["type"] != "object":
            return _type_of(target, defs)
        return name
    if "anyOf" in schema:
        kinds = [_type_of(s, defs) for s in schema["anyOf"] if s.get("type") != "null"]
        return " | ".join(kinds) or "null"
    if "enum" in schema:
        return " | ".join(json.dumps(v) for v in schema["enum"])
    if "const" in schema:
        return json.dumps(schema["const"])
    kind = schema.get("type")
    if kind == "array":
        item = _type_of(schema.get("items", {}), defs)
        lo, hi = schema.get("minItems"), schema.get("maxItems")
        if lo is not None and lo == hi:
            size = f"[{lo}]"
        else:
            size = "".join(
                f", {op} {n}" for op, n in (("len >=", lo), ("len <=", hi)) if n is not None
            )
            size = f" ({size[2:]})" if size else ""
        return f"list[{item}]{size}"
    bounds = [
        f"{op} {schema[key]}"
        for key, op in (
            ("minimum", ">="),
            ("exclusiveMinimum", ">"),
            ("maximum", "<="),
            ("exclusiveMaximum", "<"),
            ("minLength", "len >="),
            ("maxLength", "len <="),
            ("pattern", "matches"),
        )
        if key in schema
    ]
    text = str(kind or "any")
    return f"{text} ({', '.join(bounds)})" if bounds else text


def tool_cheat_sheet() -> str:
    """The build-tool cheat sheet, generated from the harness argument schemas.

    Per build tool: its summary, the rules on which arguments go together (the
    argument model's description, e.g. light rig vs single light) and each argument
    with its type, whether it is required, and its description. Nested argument
    models are listed with their own rules. ``skills/scene-builder/tools.md`` is
    this text, checked in.
    """
    lines = [
        "# Scene builder build-tool cheat sheet",
        "",
        "Generated from the harness argument schemas by",
        "`spectracut.studio.scene_builder.tool_cheat_sheet()`; do not edit by hand.",
        "Arguments not listed are refused.",
    ]
    for tool in sorted(build_tools(), key=lambda t: t.name):
        schema = tool.schema()
        defs: Mapping[str, Any] = schema.get("$defs", {})
        lines += ["", f"## {tool.name}", "", " ".join(tool.summary.split())]
        models = [("", schema)] + [
            (name, d) for name, d in sorted(defs.items()) if d.get("type") == "object"
        ]
        for name, model in models:
            if name:
                lines += ["", f"`{name}`:"]
            rules = model.get("description")
            if rules:
                lines += ["", "Rules: " + " ".join(str(rules).split())]
            required = set(model.get("required", []))
            props: Mapping[str, Any] = model.get("properties", {})
            if props:
                lines.append("")
            for arg, prop in props.items():
                need = "required" if arg in required else "optional"
                entry = f"- `{arg}` ({need}): {_type_of(prop, defs)}"
                if prop.get("description"):
                    entry += f". {' '.join(str(prop['description']).split())}"
                lines.append(entry)
            if not props:
                lines += ["", "- no arguments"]
    return "\n".join(lines) + "\n"


def check_recipe_copy(recipe: Recipe, allowed: Sequence[str], kit: BrandKit | None) -> None:
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


REPAIR_BUDGET = 3
"""Repair turns per candidate after validation errors; they never use a revise attempt."""


class SceneRepair(StrictModel):
    """One repair turn: the validation errors the builder was asked to fix."""

    round: int = Field(ge=1)
    errors: list[str] = Field(default_factory=list)


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
    repairs: list[SceneRepair] = Field(default_factory=list)
    """Repair turns spent on this candidate before it compiled (or ran out)."""


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
    repairs: list[SceneRepair] = field(default_factory=list)
    prompt: str = ""
    calls: JsonValue = None


HOST_REFUSAL = re.compile(r"^.*\brefused: .*$", re.MULTILINE)


def host_error_lines(message: str) -> list[str]:
    """The render host's refusal lines in a failed render's message, else the message."""
    lines = [m.group(0).strip() for m in HOST_REFUSAL.finditer(message)]
    return list(dict.fromkeys(lines)) or [message.strip()]


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
        parts += ["", "Build tool cheat sheet (which arguments go together):", tool_cheat_sheet()]
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

    def repair_prompt(
        self,
        prompt: str,
        calls: JsonValue,
        errors: Sequence[str],
        *,
        host: bool = False,
    ) -> str:
        """The repair prompt: the original prompt, the rejected calls and their exact errors.

        ``host`` says the render host refused the compiled recipe (the runner's message,
        ``step N`` counts recipe steps) instead of validation rejecting it before rendering.
        """
        if host:
            rejected = "Your previous answer compiled but the render host refused it. Its calls:"
            heading = "Render host errors (step N is the recipe step for calls[N]):"
        else:
            rejected = "Your previous answer was rejected before rendering. Its calls:"
            heading = "Validation errors (calls[index] tool: message):"
        return "\n".join(
            [
                prompt,
                "",
                rejected,
                _dumps(calls),
                "",
                heading,
                *(f"- {e}" for e in errors),
                "",
                "Fix every error and answer again with the whole corrected recipe, "
                'as JSON: {"plan": "...", "calls": [...]}.',
            ]
        )

    def build(self, brief: SceneBrief) -> SceneBuild:
        root = self.workdir / brief.id
        spec = NodeSpec(id=brief.id, kind=SCENE_BUILD, department=DEPARTMENT)
        critic = _BuildCritic(self.critic)
        system = self.system_prompt()
        candidates: dict[tuple[int, int], _Candidate] = {}

        def answer(out: JsonValue) -> tuple[str, JsonValue]:
            if isinstance(out, Mapping):
                return str(out.get("plan") or ""), out.get("calls")
            return "", None

        def folder_of(cand: _Candidate) -> Path:
            folder = root / f"a{cand.attempt}-c{cand.candidate}"
            folder.mkdir(parents=True, exist_ok=True)
            return folder

        def repair(cand: _Candidate, errors: list[str], *, host: bool) -> None:
            """One repair turn: the exact errors go back to the builder; no revise attempt."""
            turn = SceneRepair(round=len(cand.repairs) + 1, errors=errors)
            cand.repairs.append(turn)
            (folder_of(cand) / f"repair-{turn.round}.json").write_text(
                _dumps({"plan": cand.plan, "calls": cand.calls, "errors": errors}) + "\n",
                encoding="utf-8",
            )
            completion = self.text.complete(
                system,
                self.repair_prompt(cand.prompt, cand.calls, errors, host=host),
                schema=CALLS_SCHEMA,
            )
            cand.usage = _add(cand.usage, _prefixed(completion.usage))
            cand.plan, cand.calls = answer(completion.output)

        def settle(cand: _Candidate) -> None:
            """Compile the candidate's calls, repairing validation errors within the budget."""
            while True:
                errors: list[str] = []
                try:
                    recipe = compile_calls(
                        cand.calls,
                        recipe_id=f"{brief.id}-a{cand.attempt}-c{cand.candidate}"[:128],
                        shot_id=brief.id,
                    )
                    check_recipe_copy(recipe, brief.copy_lines(), brief.brand_kit)
                    cand.recipe, cand.error = recipe, ""
                    break
                except HarnessError as exc:
                    cand.recipe, cand.error = None, str(exc)
                    problems = exc.details.get("problems")
                    errors = problem_lines(problems) if isinstance(problems, list) else [str(exc)]
                except CopyRefused as exc:
                    cand.recipe, cand.error = None, str(exc)
                    errors = [str(exc)]
                if len(cand.repairs) >= REPAIR_BUDGET:
                    break
                repair(cand, errors, host=False)
            folder = folder_of(cand)
            (folder / "calls.json").write_text(
                _dumps(
                    {
                        "plan": cand.plan,
                        "calls": cand.calls,
                        "error": cand.error,
                        "repairs": len(cand.repairs),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            if cand.recipe is not None:
                (folder / "recipe.json").write_text(
                    cand.recipe.model_dump_json(indent=2) + "\n", encoding="utf-8"
                )

        def state_of(cand: _Candidate) -> dict[str, JsonValue]:
            return {
                "attempt": cand.attempt,
                "candidate": cand.candidate,
                "plan": cand.plan,
                "recipe": cand.recipe.model_dump(mode="json") if cand.recipe else None,
                "error": cand.error or None,
            }

        def generate(request: RevisionRequest) -> JsonValue:
            cand = _Candidate(attempt=request.attempt, candidate=request.candidate)
            candidates[(cand.attempt, cand.candidate)] = cand
            cand.prompt = self.prompt(brief, request)
            completion = self.text.complete(system, cand.prompt, schema=CALLS_SCHEMA)
            cand.usage = _prefixed(completion.usage)
            cand.plan, cand.calls = answer(completion.output)
            settle(cand)
            return state_of(cand)

        def render(state: JsonValue) -> JsonValue:
            assert isinstance(state, dict)
            cand = candidates[(cast(int, state["attempt"]), cast(int, state["candidate"]))]
            frame = root / f"a{cand.attempt}-c{cand.candidate}" / "frame.png"
            while cand.recipe is not None:
                try:
                    record, used = self.renderer(cand.recipe, frame)
                except JobFailed as exc:
                    # A host refusal or render failure is a repairable candidate error:
                    # the runner's message goes back to the builder (#330).
                    cand.usage = _add(cand.usage, exc.usage)
                    cand.error = str(exc)
                    errors = host_error_lines(cand.error)
                    if len(cand.repairs) >= REPAIR_BUDGET:
                        cand.recipe = None
                        break
                    repair(cand, errors, host=True)
                    settle(cand)
                    continue
                cand.render = record
                cand.usage = _add(cand.usage, used)
                cand.output = {"frames": [record.path]}
                state.update(state_of(cand))
                return cand.output
            # The loop keeps this dict as the candidate's recipe: show what was refused.
            state.update(state_of(cand))
            cand.output = {"frames": [], "error": cand.error, "candidate": dict(state)}
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
                    repairs=list(cand.repairs),
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
    "CHEAT_SHEET",
    "REPAIR_BUDGET",
    "SCENE_BUILD",
    "CopyRefused",
    "HOST_REFUSAL",
    "RecipeRenderer",
    "SceneBrief",
    "SceneBuild",
    "SceneBuilder",
    "SceneIteration",
    "SceneRepair",
    "check_recipe_copy",
    "compile_calls",
    "host_error_lines",
    "problem_lines",
    "scene_build_runner",
    "tool_cheat_sheet",
]
