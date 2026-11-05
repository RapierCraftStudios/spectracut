# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discover tool: list tools, or fetch full input schemas on demand.

The server lists only a small fixed core; every other tool is found here and
invoked through ``call``, so the fixed tool context stays small.
"""

from __future__ import annotations

from pydantic import Field

from spectracut.harness.tools import HarnessTool, Output, Session, ToolArgs, get_tool, registry


class Discover(ToolArgs):
    names: list[str] | None = Field(default=None, description="return these tools' schemas")
    prefix: str | None = Field(default=None, description="only tools starting with this")


def discover(session: Session, args: Discover) -> Output:
    tools = registry()
    if args.names:
        return Output({"schemas": {n: get_tool(n).schema() for n in args.names}})
    listed = [
        {"name": t.name, "kind": t.kind, "summary": t.summary}
        for t in tools.values()
        if not args.prefix or t.name.startswith(args.prefix)
    ]
    return Output({"tools": listed, "hint": "discover names=[...] for schemas, then call"})


TOOLS = [
    HarnessTool(
        "discover",
        "List harness tools (optionally by prefix) or get full schemas for named tools.",
        Discover,
        "meta",
        discover,
    ),
]
