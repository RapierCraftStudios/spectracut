# SPDX-License-Identifier: AGPL-3.0-or-later
"""Docs tools: ranked search over the bpy API and manual indexes.

Agents look up exact bpy paths and signatures here instead of guessing. A hit
for an allowlisted property carries ``settable_via``: the typed tool call that
sets it (``render.set_property``). Agents still act only through typed tools.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from spectracut.harness.docs.search import DocsKind, search
from spectracut.harness.tools import Handler, HarnessTool, Output, Session, ToolArgs


class SearchDocs(ToolArgs):
    query: Annotated[str, Field(min_length=2, max_length=200, description="words to look up")]
    limit: Annotated[int, Field(ge=1, le=20)] = 5


HINT = "a hit with settable_via is set by calling settable_via.tool with its args plus a value"


def _searcher(kind: DocsKind) -> Handler:
    def handler(session: Session, args: SearchDocs) -> Output:
        return Output({"hits": search(args.query, kind, args.limit), "hint": HINT})

    return handler


TOOLS = [
    HarnessTool(
        "docs.search_api",
        "Search the bpy API index for exact paths and signatures (shows the setting tool).",
        SearchDocs,
        "meta",
        _searcher("api"),
    ),
    HarnessTool(
        "docs.search_manual",
        "Search the Blender manual notes for how-to topics.",
        SearchDocs,
        "meta",
        _searcher("manual"),
    ),
]
