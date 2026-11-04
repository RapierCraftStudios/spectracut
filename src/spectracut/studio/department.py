# SPDX-License-Identifier: AGPL-3.0-or-later
"""Departments: the studio's subagents, declared as typed classes plus a skill pack.

A department declares the node kinds it produces, the models of the inputs it
reads, the model of its output, its skill-pack folder (system prompt, rubric,
failure corpus, taste anchors) and its cost estimate. The
:class:`~spectracut.studio.runner.DepartmentRunner` executes it through the AI
provider layer. Every department in later issues implements this contract
(``docs/engineering/contracts/departments.md``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, Field, JsonValue, ValidationError

from spectracut.spec._base import StrictModel

NonNegFloat = Annotated[float, Field(ge=0.0)]
NonNegInt = Annotated[int, Field(ge=0)]

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: Default root of the shipped skill packs: ``spectracut/skills/<department>/``.
SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


class DepartmentOutputError(ValueError):
    """A department's output (or an input it was given) failed its schema."""

    def __init__(self, department: str, node_id: str, detail: str) -> None:
        self.department = department
        self.node_id = node_id
        self.detail = detail
        super().__init__(f"department {department!r}, node {node_id!r}: {detail}")


class CostEstimate(StrictModel):
    """What one run of a department is expected to cost, for the Producer's estimate."""

    ai_requests: NonNegInt = 0
    ai_input_tokens: NonNegInt = 0
    gpu_minutes: NonNegFloat = 0.0
    elevenlabs_characters: NonNegInt = 0


class SkillPack(StrictModel):
    """A department's craft knowledge, loaded from its folder (``skills/README.md``)."""

    path: Path
    prompt: str
    """``prompt.md``: the system prompt."""
    rubric: str
    """``rubric.md``: what the Critic scores the output against."""
    failures: tuple[Path, ...] = ()
    """``failures/*.md``: the failure corpus."""
    anchors: tuple[Path, ...] = ()
    """``anchors/*.md``: taste anchors."""

    @classmethod
    def load(cls, path: Path) -> SkillPack:
        path = Path(path)
        if not path.is_dir():
            raise ValueError(f"skill pack {path}: folder not found")
        texts: dict[str, str] = {}
        for name in ("prompt", "rubric"):
            file = path / f"{name}.md"
            if not file.is_file():
                raise ValueError(f"skill pack {path}: missing {file.name}")
            text = file.read_text(encoding="utf-8").strip()
            if not text:
                raise ValueError(f"skill pack {path}: {file.name} is empty")
            texts[name] = text
        return cls(
            path=path,
            prompt=texts["prompt"],
            rubric=texts["rubric"],
            failures=tuple(sorted((path / "failures").glob("*.md"))),
            anchors=tuple(sorted((path / "anchors").glob("*.md"))),
        )


class Department:
    """Base class of every department. Subclasses declare the class variables below.

    A subclass that only shares declarations with others sets ``abstract = True``
    in its own body and is not validated.
    """

    name: ClassVar[str]
    """Department slug; matches ``NodeSpec.department``."""
    produces: ClassVar[frozenset[str]]
    """Node kinds this department makes."""
    inputs: ClassVar[Mapping[str, type[BaseModel]]] = {}
    """Input node kind -> model its output must validate against."""
    output: ClassVar[type[BaseModel]]
    """Model the department's output must validate against."""
    skill_pack: ClassVar[Path | None] = None
    """Skill-pack folder; ``None`` means ``spectracut/skills/<name>``."""
    cost: ClassVar[CostEstimate] = CostEstimate()
    uses_gpu: ClassVar[bool] = False
    """Runs hold a GPU lease (bounded by ``CapacityLimits.gpu_leases``)."""

    _packs: ClassVar[dict[type[Department], SkillPack]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("abstract", False):
            return
        where = f"department class {cls.__name__}"
        name = getattr(cls, "name", None)
        if not isinstance(name, str) or not _SLUG.match(name):
            raise TypeError(f"{where}: name must be a slug, got {name!r}")
        produces = getattr(cls, "produces", None)
        if not isinstance(produces, frozenset) or not produces:
            raise TypeError(f"{where}: produces must be a non-empty frozenset of node kinds")
        for kind in produces:
            if not isinstance(kind, str) or not _SLUG.match(kind):
                raise TypeError(f"{where}: produces has a bad node kind {kind!r}")
        output = getattr(cls, "output", None)
        if not (isinstance(output, type) and issubclass(output, BaseModel)):
            raise TypeError(f"{where}: output must be a pydantic model class")
        for kind, model in cls.inputs.items():
            if not _SLUG.match(kind):
                raise TypeError(f"{where}: inputs has a bad node kind {kind!r}")
            if not (isinstance(model, type) and issubclass(model, BaseModel)):
                raise TypeError(f"{where}: inputs[{kind!r}] must be a pydantic model class")
        if not isinstance(cls.cost, CostEstimate):
            raise TypeError(f"{where}: cost must be a CostEstimate")

    @classmethod
    def pack_path(cls) -> Path:
        return cls.skill_pack if cls.skill_pack is not None else SKILLS_ROOT / cls.name

    @classmethod
    def pack(cls) -> SkillPack:
        """The loaded skill pack (cached per class)."""
        pack = Department._packs.get(cls)
        if pack is None:
            pack = SkillPack.load(cls.pack_path())
            Department._packs[cls] = pack
        return pack

    @classmethod
    def rubric(cls) -> str:
        return cls.pack().rubric

    @classmethod
    def output_schema(cls) -> dict[str, Any]:
        return cls.output.model_json_schema()

    @classmethod
    def validate_output(cls, node_id: str, value: JsonValue) -> JsonValue:
        """Validate ``value`` against :attr:`output`; return it as plain JSON."""
        try:
            model = cls.output.model_validate(value)
        except ValidationError as exc:
            raise DepartmentOutputError(
                cls.name, node_id, f"output fails its schema: {_summary(exc)}"
            ) from None
        dumped: JsonValue = model.model_dump(mode="json")
        return dumped

    @classmethod
    def validate_inputs(
        cls, node_id: str, inputs: Mapping[str, JsonValue], kinds: Mapping[str, str]
    ) -> None:
        """Check each input's kind is declared and its output validates against its model.

        ``kinds`` maps each input node id to its kind.
        """
        for input_id, value in inputs.items():
            kind = kinds.get(input_id, input_id)
            model = cls.inputs.get(kind)
            if model is None:
                raise DepartmentOutputError(
                    cls.name, node_id, f"input {input_id!r} has undeclared kind {kind!r}"
                )
            try:
                model.model_validate(value)
            except ValidationError as exc:
                raise DepartmentOutputError(
                    cls.name, node_id, f"input {input_id!r} fails its schema: {_summary(exc)}"
                ) from None


def _summary(exc: ValidationError, limit: int = 5) -> str:
    parts = []
    for err in exc.errors()[:limit]:
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        parts.append(f"{loc}: {err['msg']}")
    more = exc.error_count() - limit
    if more > 0:
        parts.append(f"... {more} more")
    return "; ".join(parts)
