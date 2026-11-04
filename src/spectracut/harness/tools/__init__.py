# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed harness tools, one module per area, found by a package scan.

Every module in this package that defines a module-level ``TOOLS`` list of
:class:`HarnessTool` joins the registry, so a new tool module needs no server
edit. Tool kinds:

- ``build`` tools record recipe steps under their own name, which is always a
  bpy runtime tool name (``host/bpy_runtime/tools.py`` ``TOOL_NAMES``);
- ``query`` tools run a preview job with runtime query steps appended to a copy
  of the recipe and return the host's report;
- ``meta`` tools (discover, preview, look) record no steps.

``steps`` lists every runtime tool name a tool may send to a host.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from spectracut.harness.dispatch import PreviewDispatcher
from spectracut.harness.errors import HarnessError
from spectracut.harness.recorder import CallKind, Recorder

Vec3 = Annotated[list[float], Field(min_length=3, max_length=3)]
"""An ``[x, y, z]`` triple."""


class ToolArgs(BaseModel):
    """Base for tool argument models: unknown keys are refused."""

    model_config = ConfigDict(extra="forbid")


@dataclass
class Session:
    """State shared by the tools of one MCP session."""

    recorder: Recorder
    dispatcher: PreviewDispatcher
    recipe_path: Path | None = None
    """Where the recipe is saved after every build call (for replay)."""

    def record_build(self, tool: str, args: dict[str, JsonValue], steps: list[Step]) -> Output:
        call = self.recorder.record(tool, "build", args, steps)
        if self.recipe_path is not None:
            self.recorder.save(self.recipe_path)
        return Output(
            {
                "recorded": [{"index": i, "tool": steps[n][0]} for n, i in enumerate(call.steps)],
                "recipe_steps": len(self.recorder.steps),
            }
        )


Step = tuple[str, dict[str, JsonValue]]


@dataclass
class Output:
    """What a tool returns: JSON data and optional PNG images (label, bytes)."""

    data: dict[str, Any]
    images: list[tuple[str, bytes]] = field(default_factory=list)


Handler = Callable[[Session, Any], Output]


@dataclass(frozen=True)
class HarnessTool:
    name: str
    summary: str
    model: type[ToolArgs]
    kind: CallKind
    handler: Handler
    steps: frozenset[str] = frozenset()

    def schema(self) -> dict[str, Any]:
        return self.model.model_json_schema()

    def parse(self, args: Mapping[str, Any] | None) -> ToolArgs:
        try:
            return self.model.model_validate(dict(args or {}))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'args'}: {e['msg']}" for e in exc.errors()
            )
            raise HarnessError("args.invalid", f"{self.name}: {problems}") from None

    def call(self, session: Session, args: Mapping[str, Any] | None) -> Output:
        parsed = self.parse(args)
        if self.kind != "build":
            session.recorder.record(self.name, self.kind, parsed.model_dump(mode="json"))
        return self.handler(session, parsed)


def dump(args: ToolArgs) -> dict[str, JsonValue]:
    """Arguments as the bpy runtime expects them: JSON, unset fields left out."""
    data: dict[str, JsonValue] = args.model_dump(mode="json", exclude_none=True)
    return data


def build_tool(name: str, summary: str, model: type[ToolArgs]) -> HarnessTool:
    """A build tool that records one runtime step of the same name."""

    def handler(session: Session, args: ToolArgs) -> Output:
        step_args = dump(args)
        return session.record_build(name, step_args, [(name, step_args)])

    return HarnessTool(name, summary, model, "build", handler, frozenset({name}))


@cache
def registry() -> Mapping[str, HarnessTool]:
    """Every harness tool, by name, from every module in this package."""
    tools: dict[str, HarnessTool] = {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{__name__}.{info.name}")
        for tool in getattr(module, "TOOLS", ()):
            if tool.name in tools:
                raise ValueError(f"duplicate harness tool {tool.name!r} in {info.name}")
            tools[tool.name] = tool
    return dict(sorted(tools.items()))


def get_tool(name: str) -> HarnessTool:
    tool = registry().get(name)
    if tool is None:
        raise HarnessError("tool.unknown", f"unknown tool {name!r}: call discover to list tools")
    return tool


def build_tools() -> list[HarnessTool]:
    """Every tool that records recipe steps."""
    return [t for t in registry().values() if t.kind == "build"]


def schema_for(name: str) -> dict[str, Any]:
    """The full JSON input schema of a harness tool."""
    return get_tool(name).schema()
