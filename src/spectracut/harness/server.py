# SPDX-License-Identifier: AGPL-3.0-or-later
"""The harness MCP server (official MCP Python SDK, low-level ``Server``).

``list_tools`` returns only a small fixed core: ``discover``, ``call``,
``preview.render``, ``look`` and ``inspect.framing``. Every other harness tool is
found through ``discover`` and run through ``call`` (``{tool, args}``), so the
tool context a client loads up front stays under :data:`FIXED_CONTEXT_BUDGET_BYTES`.

A failed call returns ``isError`` with a JSON ``{"error": {"code", "message"}}``
whose code is from :data:`spectracut.harness.errors.ERROR_CODES`. Images come back
as base64 PNG ``ImageContent``.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server

from spectracut.harness.dispatch import HarnessConfig, PreviewDispatcher
from spectracut.harness.errors import HarnessError
from spectracut.harness.recorder import Recorder
from spectracut.harness.tools import Output, Session, get_tool, registry

SERVER_NAME = "spectracut-harness"
FIXED_TOOLS: tuple[str, ...] = ("discover", "preview.render", "look", "inspect.framing")
CALL_TOOL = "call"
FIXED_CONTEXT_BUDGET_BYTES = 6000
"""Upper bound on the JSON size of the ``list_tools`` result."""
RECIPE_FILE = "recipe.json"

_CALL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "description": "a tool name from discover"},
        "args": {"type": "object", "description": "the tool's arguments (see discover names)"},
    },
    "required": ["tool"],
    "additionalProperties": False,
}


def fixed_tools() -> list[types.Tool]:
    """The tools ``list_tools`` returns: the fixed core plus ``call``."""
    tools = registry()
    listed = [
        types.Tool(name=name, description=tools[name].summary, inputSchema=tools[name].schema())
        for name in FIXED_TOOLS
    ]
    listed.insert(
        1,
        types.Tool(
            name=CALL_TOOL,
            description="Run any harness tool by name (find tools and schemas with discover).",
            inputSchema=_CALL_SCHEMA,
        ),
    )
    return listed


def _tools_bytes(tools: list[types.Tool]) -> int:
    result = types.ListToolsResult(tools=tools)
    return len(result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))


def fixed_context_bytes() -> int:
    """JSON size of the ``list_tools`` result a client loads up front."""
    return _tools_bytes(fixed_tools())


def all_tools_bytes() -> int:
    """JSON size if every harness tool and its schema were listed up front."""
    return _tools_bytes(
        [
            types.Tool(name=t.name, description=t.summary, inputSchema=t.schema())
            for t in registry().values()
        ]
    )


def _content(output: Output) -> list[types.TextContent | types.ImageContent]:
    content: list[types.TextContent | types.ImageContent] = [
        types.TextContent(type="text", text=json.dumps(output.data, sort_keys=True))
    ]
    for label, png in output.images:
        content.append(types.TextContent(type="text", text=json.dumps({"image": label})))
        content.append(
            types.ImageContent(
                type="image", data=base64.b64encode(png).decode("ascii"), mimeType="image/png"
            )
        )
    return content


def _error_result(error: HarnessError) -> types.CallToolResult:
    text = json.dumps(error.to_payload(), sort_keys=True, default=str)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=True)


def call(session: Session, name: str, arguments: Mapping[str, Any] | None) -> Output:
    """Run one MCP tool call (``call`` unwraps to the named tool)."""
    args = dict(arguments or {})
    if name == CALL_TOOL:
        target = args.get("tool")
        if not isinstance(target, str) or set(args) - {"tool", "args"}:
            raise HarnessError("args.invalid", "call: pass {tool: <name>, args: {...}}")
        inner = args.get("args") or {}
        if not isinstance(inner, dict):
            raise HarnessError("args.invalid", "call: args must be an object")
        name, args = target, inner
    return get_tool(name).call(session, args)


def build_server(dispatcher: PreviewDispatcher, recorder: Recorder) -> Server[Any, Any]:
    """An MCP server whose session records into ``recorder`` and renders via ``dispatcher``."""
    work = dispatcher.config.work_dir
    session = Session(recorder, dispatcher, recipe_path=work / RECIPE_FILE)
    server: Server[Any, Any] = Server(SERVER_NAME)

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _list_tools() -> list[types.Tool]:
        return fixed_tools()

    @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        try:
            output = await anyio.to_thread.run_sync(call, session, name, arguments)
        except HarnessError as exc:
            return _error_result(exc)
        return types.CallToolResult(content=list(_content(output)), isError=False)

    return server


def session_from_env(env: Mapping[str, str] | None = None) -> tuple[PreviewDispatcher, Recorder]:
    """The dispatcher and recorder for a stdio session, configured from env."""
    env = os.environ if env is None else env
    config = HarnessConfig.from_env(env)
    Path(config.work_dir).mkdir(parents=True, exist_ok=True)
    return PreviewDispatcher(config, env=env), Recorder(blender_version=config.blender_version)


async def _serve(server: Server[Any, Any]) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def run_stdio() -> None:
    """Serve the harness over stdio until the client disconnects."""
    dispatcher, recorder = session_from_env()
    anyio.run(_serve, build_server(dispatcher, recorder))
