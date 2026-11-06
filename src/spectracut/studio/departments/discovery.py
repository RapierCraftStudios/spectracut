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
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar

from pydantic import BaseModel, Field, JsonValue, ValidationInfo, field_validator

from spectracut.spec._base import NonEmptyStr, Slug, StrictModel, ensure_unique_ids
from spectracut.spec.brief import ClaimSource, CreativeBrief
from spectracut.studio.department import SKILLS_ROOT, CostEstimate, Department
from spectracut.studio.node import NodeSpec

#: Skill packs of the story departments: ``spectracut/skills/story/<department>/``.
STORY_SKILLS = SKILLS_ROOT / "story"

INTAKE = "intake"
PRODUCT = "product"
BRIEF = "brief"


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


class ProductNotes(StrictModel):
    """Product understanding: a summary and features, each traced to a source."""

    summary: NonEmptyStr
    sources: list[ClaimSource] = Field(min_length=1)
    features: list[ProductFeature] = Field(min_length=1)

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
    inputs: ClassVar[dict[str, type[BaseModel]]] = {INTAKE: IntakeNotes}
    output = ProductNotes
    skill_pack = STORY_SKILLS / "product-understanding"
    cost = CostEstimate(ai_requests=1, ai_input_tokens=8_000)


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
