# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discovery: intake, product understanding and the Strategist's creative brief.

Three departments, run in this order on a production graph:

- ``intake`` (node kind ``intake``): turns the user's answers (node param
  ``answers``) into :class:`IntakeNotes`: goal, product, offer, deadline, platforms.
- ``product-understanding`` (kind ``product``): reads the site and doc excerpts it
  is given (node param ``pages``, a list of ``{url, text}``) and returns
  :class:`ProductNotes`. Each page it uses becomes a
  :class:`~spectracut.spec.brief.ClaimSource` and every feature cites one. It
  never fetches a URL: departments do not touch the network
  (``docs/engineering/contracts/departments.md``); live capture is
  ``packages/capture``.
- ``strategist`` (kind ``brief``): writes the
  :class:`~spectracut.spec.brief.CreativeBrief` (one message, sourced proof
  points, tone, audience, success measure) from the intake and product notes.

:func:`discovery_nodes` declares the three nodes. The copy gate
(:mod:`spectracut.writer.copy_gate`) checks the brief's sources against the
product notes.

Screen map: a production made with ``studio new --product product.yaml`` gets a
``product`` node (:func:`product_screen_node`) wired after the script. Its params
carry the scanned front-end ``routes``, ``pages`` excerpts of them and the brief's
``proof_points``; its output's ``screens`` maps every proof point and every script
beat to a route plus interaction steps (a beat may instead be an abstract
``interlude``). :func:`check_screen_map` enforces that; ``check_output`` checks
the beats against the ``script`` input and ``check_params`` checks routes and
proof points against the params.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationInfo,
    field_validator,
    model_validator,
)

from spectracut.spec._base import NonEmptyStr, Slug, StrictModel, ensure_unique_ids
from spectracut.spec.brief import ClaimSource, CreativeBrief
from spectracut.spec.product import FrontendRoute, ProductSource
from spectracut.studio.department import (
    SKILLS_ROOT,
    CostEstimate,
    Department,
    DepartmentOutputError,
)
from spectracut.studio.node import NodeSpec

#: Skill packs of the story departments: ``spectracut/skills/story/<department>/``.
STORY_SKILLS = SKILLS_ROOT / "story"

INTAKE = "intake"
PRODUCT = "product"
BRIEF = "brief"
SCRIPT = "script"
"""Kind of the script node (``departments.writer.SCRIPT``; writer imports this module)."""


class IntakeNotes(StrictModel):
    """What the user wants: goal, product, offer, deadline and where it will run."""

    goal: NonEmptyStr
    product: NonEmptyStr
    offer: str = ""
    deadline: str = ""
    """When the video is needed, as the user gave it (ISO date preferred)."""
    platforms: list[Slug] = Field(min_length=1)


class ProductFeature(StrictModel):
    """One thing the product does, citing the source that shows it."""

    text: NonEmptyStr
    source_id: Slug


class ScreenStep(StrictModel):
    """One interaction on a screen: visit a URL, click, type, scroll, hover or wait."""

    action: Literal["visit", "click", "type", "scroll", "hover", "wait"]
    target: NonEmptyStr
    """Route or URL (``visit``), element description or selector, or a duration (``wait``)."""
    value: str = ""
    """Text typed (``type``) or scroll amount; empty otherwise."""


class ScreenMapping(StrictModel):
    """Which screen of the real front end shows a proof point or a script beat, and how."""

    proof_point: str = ""
    """A proof point's claim, exactly as the brief words it."""
    beat: str = ""
    """A script beat, exactly as the script names it."""
    route: str = ""
    """A scanned front-end route (``[param]`` segments may be filled in)."""
    steps: list[ScreenStep] = Field(default_factory=list)
    source_id: Slug | None = None
    interlude: bool = False
    """The beat is an abstract interlude: no screen, no route."""

    @model_validator(mode="after")
    def _shape(self) -> ScreenMapping:
        if bool(self.proof_point) == bool(self.beat):
            raise ValueError("a screen mapping names exactly one of proof_point or beat")
        if self.interlude:
            if self.proof_point:
                raise ValueError("a proof point cannot be an interlude; it needs a screen")
            if self.route or self.steps:
                raise ValueError("an interlude has no route or steps")
        elif not self.route or not self.steps:
            raise ValueError("a screen mapping needs a route and at least one step")
        return self


class ScriptBeats(BaseModel):
    """The beats of a script, as product understanding reads them (other fields ignored).

    A view of :class:`~spectracut.studio.departments.writer.Script`, which cannot be
    imported here: ``writer`` imports this module.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    class Line(BaseModel):
        model_config = ConfigDict(extra="ignore", frozen=True)
        beat: NonEmptyStr

    lines: list[Line] = Field(min_length=1)


class ProductNotes(StrictModel):
    """Product understanding: a summary and features, each traced to a source."""

    summary: NonEmptyStr
    sources: list[ClaimSource] = Field(min_length=1)
    features: list[ProductFeature] = Field(min_length=1)
    screens: list[ScreenMapping] = Field(default_factory=list)
    """Screen map: route and interaction steps for each proof point and script beat."""

    @field_validator("sources")
    @classmethod
    def _unique_sources(cls, v: list[ClaimSource]) -> list[ClaimSource]:
        ensure_unique_ids(v, "source")
        return v

    @field_validator("features")
    @classmethod
    def _features_cite_sources(
        cls, v: list[ProductFeature], info: ValidationInfo
    ) -> list[ProductFeature]:
        sources = info.data.get("sources")
        if sources is None:  # sources failed validation; reported there
            return v
        known = {s.id for s in sources}
        for i, feature in enumerate(v):
            if feature.source_id not in known:
                raise ValueError(
                    f"features[{i}].source_id {feature.source_id!r} is not a declared source"
                )
        return v

    @field_validator("screens")
    @classmethod
    def _screens_cite_sources(
        cls, v: list[ScreenMapping], info: ValidationInfo
    ) -> list[ScreenMapping]:
        sources = info.data.get("sources")
        if sources is None:
            return v
        known = {s.id for s in sources}
        for i, screen in enumerate(v):
            if screen.source_id is not None and screen.source_id not in known:
                raise ValueError(
                    f"screens[{i}].source_id {screen.source_id!r} is not a declared source"
                )
        return v


def _segments(path: str) -> list[str]:
    return [p for p in path.split("?", 1)[0].split("#", 1)[0].strip("/").split("/") if p]


def route_matches(route: str, pattern: str) -> bool:
    """``route`` is ``pattern`` with its ``[x]``/``[...x]``/``[[...x]]`` segments filled."""
    got, want = _segments(route), _segments(pattern)
    for i, seg in enumerate(want):
        if seg.startswith("[[...") or seg.startswith("[..."):
            return len(got) >= i + (0 if seg.startswith("[[") else 1)
        if i >= len(got):
            return False
        if not (seg.startswith("[") and seg.endswith("]")) and got[i] != seg:
            return False
    return len(got) == len(want)


def check_screen_map(
    notes: ProductNotes,
    *,
    routes: Sequence[str] | None = None,
    proof_points: Sequence[str] = (),
    beats: Sequence[str] = (),
) -> list[str]:
    """Problems with ``notes.screens``: unknown routes, unmapped proof points or beats."""
    problems: list[str] = []
    if routes is not None:
        for i, screen in enumerate(notes.screens):
            if screen.route and not any(route_matches(screen.route, r) for r in routes):
                problems.append(f"screens[{i}].route {screen.route!r} is not a front-end route")
    mapped_points = {s.proof_point for s in notes.screens if s.proof_point and s.route}
    for claim in proof_points:
        if claim not in mapped_points:
            problems.append(f"proof point {claim!r} has no screen")
    mapped_beats = {s.beat for s in notes.screens if s.beat}
    for beat in beats:
        if beat not in mapped_beats:
            problems.append(f"beat {beat!r} has no screen or interlude")
    return problems


class Intake(Department):
    """Interviews the user: goal, product, offer, deadline, platforms."""

    name = "intake"
    produces = frozenset({INTAKE})
    output = IntakeNotes
    skill_pack = STORY_SKILLS / "intake"
    cost = CostEstimate(ai_requests=1, ai_input_tokens=1_500)


class ProductUnderstanding(Department):
    """Reads the supplied site and doc excerpts and captures what the product does."""

    name = "product-understanding"
    produces = frozenset({PRODUCT})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {INTAKE: IntakeNotes, SCRIPT: ScriptBeats}
    output = ProductNotes
    skill_pack = STORY_SKILLS / "product-understanding"
    cost = CostEstimate(ai_requests=1, ai_input_tokens=8_000)

    @classmethod
    def check_output(
        cls,
        node_id: str,
        output: JsonValue,
        inputs: Mapping[str, JsonValue],
        kinds: Mapping[str, str],
    ) -> None:
        """With a script input: every beat has a screen or an interlude."""
        raw = next((v for i, v in inputs.items() if kinds.get(i, i) == SCRIPT), None)
        if raw is None:
            return
        beats = [line.beat for line in ScriptBeats.model_validate(raw).lines]
        problems = check_screen_map(ProductNotes.model_validate(output), beats=beats)
        if problems:
            raise DepartmentOutputError(cls.name, node_id, "; ".join(problems))

    @classmethod
    def check_params(cls, node_id: str, output: JsonValue, params: Mapping[str, JsonValue]) -> None:
        """With ``routes``/``proof_points`` params: routes exist, every proof point is shown."""
        raw_routes = params.get("routes")
        raw_points = params.get("proof_points")
        if raw_routes is None and raw_points is None:
            return
        routes = (
            [FrontendRoute.model_validate(r).path for r in raw_routes]
            if isinstance(raw_routes, list)
            else None
        )
        points = [str(p) for p in raw_points] if isinstance(raw_points, list) else []
        problems = check_screen_map(
            ProductNotes.model_validate(output), routes=routes, proof_points=points
        )
        if problems:
            raise DepartmentOutputError(cls.name, node_id, "; ".join(problems))


class Strategist(Department):
    """Writes the creative brief: one message, sourced proof points, tone, audience."""

    name = "strategist"
    produces = frozenset({BRIEF})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {INTAKE: IntakeNotes, PRODUCT: ProductNotes}
    output = CreativeBrief
    skill_pack = STORY_SKILLS / "strategist"
    cost = CostEstimate(ai_requests=1, ai_input_tokens=4_000)


DISCOVERY_DEPARTMENTS: tuple[type[Department], ...] = (Intake, ProductUnderstanding, Strategist)


def discovery_nodes(
    answers: Mapping[str, JsonValue], pages: Sequence[Mapping[str, str]]
) -> list[NodeSpec]:
    """The Discovery nodes: ``intake`` -> ``product`` -> ``brief``.

    ``answers`` are the user's intake answers; ``pages`` are ``{url, text}``
    excerpts of the product's site and docs.
    """
    page_list: list[JsonValue] = []
    for i, page in enumerate(pages):
        if not page.get("url") or not page.get("text"):
            raise ValueError(f"pages[{i}] needs a non-empty url and text")
        page_list.append({"url": page["url"], "text": page["text"]})
    if not page_list:
        raise ValueError("product understanding needs at least one page")
    return [
        NodeSpec(id=INTAKE, kind=INTAKE, department=Intake.name, params={"answers": dict(answers)}),
        NodeSpec(
            id=PRODUCT,
            kind=PRODUCT,
            department=ProductUnderstanding.name,
            inputs=(INTAKE,),
            params={"pages": page_list},
        ),
        NodeSpec(id=BRIEF, kind=BRIEF, department=Strategist.name, inputs=(INTAKE, PRODUCT)),
    ]


def product_screen_node(
    source: ProductSource,
    routes: Sequence[FrontendRoute],
    pages: Sequence[Mapping[str, str]],
    proof_points: Sequence[str],
    script_id: str,
    *,
    extra: Mapping[str, JsonValue] | None = None,
) -> NodeSpec:
    """The ``product`` node that maps proof points and the script's beats to screens.

    It reads the script node ``script_id``; ``routes`` and ``pages`` come from the
    front-end scan (:func:`spectracut.spec.product.product_routes`), so the
    department never reads files. ``extra`` params (e.g. the production's brief)
    are carried as given.
    """
    if not pages:
        raise ValueError("product understanding needs at least one page")
    params: dict[str, JsonValue] = {
        **(dict(extra) if extra else {}),
        "product": source.model_dump(mode="json"),
        "routes": [r.model_dump(mode="json") for r in routes],
        "pages": [{"url": p["url"], "text": p["text"]} for p in pages],
        "proof_points": list(proof_points),
    }
    return NodeSpec(
        id=PRODUCT,
        kind=PRODUCT,
        department=ProductUnderstanding.name,
        inputs=(script_id,),
        params=params,
    )


def intake_inputs(
    idea: str, values: Mapping[str, str], sources: Sequence[Mapping[str, str]]
) -> tuple[dict[str, JsonValue], list[dict[str, str]]]:
    """Intake ``answers`` and product ``pages`` built from an idea, aspect values and sources.

    ``values`` maps aspect ids (``product``, ``platforms``, ``offer``, ``deadline``)
    to their resolved text; ``sources`` are ``{ref, text}`` mappings already
    loaded by the caller, so no department touches the network. The result
    validates as :class:`IntakeNotes` and feeds :func:`discovery_nodes`.
    """
    answers: dict[str, JsonValue] = {
        "goal": idea,
        "product": values["product"],
        "offer": values.get("offer", ""),
        "deadline": values.get("deadline", ""),
        "platforms": [p.strip() for p in values["platforms"].split(",") if p.strip()],
    }
    pages = [{"url": s["ref"], "text": s["text"]} for s in sources]
    return answers, pages
