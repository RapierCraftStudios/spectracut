# SPDX-License-Identifier: AGPL-3.0-or-later
"""A real MCP stdio client against ``spectracut mcp``.

The server runs as a subprocess (``python -m spectracut.cli.app mcp``). Its previews
are signed queue jobs run by the real host agent and execution guard, which launch
the fake Blender from ``conftest.py``. Shared with the opt-in real-Blender variant
in ``test_harness_e2e.py``.
"""

from __future__ import annotations

import base64
import json
import sys
from typing import Any

import anyio
import mcp.types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from spectracut.harness.errors import ERROR_CODES
from spectracut.harness.imagecmp import decode_png
from spectracut.harness.server import FIXED_TOOLS
from spectracut.queue.signing import SIGNING_KEY_ENV, generate_keypair

PRODUCT_SHOT: list[tuple[str, dict[str, Any]]] = [
    ("scene.clear", {}),
    ("object.add_primitive", {"kind": "plane", "name": "Floor", "scale": [4, 4, 1]}),
    ("object.add_primitive", {"kind": "cube", "name": "Product", "location": [0, 0, 1]}),
    (
        "material.apply_preset",
        {"object": "Product", "preset": "glossy_plastic", "color": [0.9, 0.2, 0.1]},
    ),
    ("light.add", {"rig": "three_point", "name": "Studio", "target": [0, 0, 1]}),
    ("camera.add", {"name": "Cam", "location": [0, -7, 1], "rotation": [1.5707963, 0, 0]}),
    ("render.set_resolution", {"width": 640, "height": 360}),
]


def text_json(result: types.CallToolResult) -> dict[str, Any]:
    first = result.content[0]
    assert isinstance(first, types.TextContent)
    data: dict[str, Any] = json.loads(first.text)
    return data


def run_client(env: dict[str, str], body: Any) -> Any:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "spectracut.cli.app", "mcp"], env=env
    )

    async def main() -> Any:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            return await body(client)

    return anyio.run(main)


async def product_shot(client: ClientSession) -> dict[str, Any]:
    listed = await client.list_tools()
    names = [t.name for t in listed.tools]
    found = text_json(await client.call_tool("discover", {}))
    schemas = text_json(
        await client.call_tool("discover", {"names": ["light.add", "material.apply_preset"]})
    )
    for tool, args in PRODUCT_SHOT:
        result = await client.call_tool("call", {"tool": tool, "args": args})
        assert not result.isError, result.content
    preview = await client.call_tool("preview.render", {})
    framing = await client.call_tool("inspect.framing", {"object": "Product"})
    return {
        "names": names,
        "discovered": [t["name"] for t in found["tools"]],
        "schemas": schemas["schemas"],
        "preview": preview,
        "framing": framing,
    }


def check_product_shot(got: dict[str, Any]) -> None:
    assert set(got["names"]) == {*FIXED_TOOLS, "call"}
    assert {"camera.add", "light.add", "material.apply_preset", "scene.clear"} <= set(
        got["discovered"]
    )
    assert "rig" in got["schemas"]["light.add"]["properties"]
    preview: types.CallToolResult = got["preview"]
    assert not preview.isError, preview.content
    meta = text_json(preview)
    assert meta["recipe_hash"].startswith("sha256:") and meta["recipe_steps"] == 9
    images = [c for c in preview.content if isinstance(c, types.ImageContent)]
    assert len(images) == 1 and images[0].mimeType == "image/png"
    png = decode_png(base64.b64decode(images[0].data))
    assert png.width > 0 and png.height > 0
    framing: types.CallToolResult = got["framing"]
    assert not framing.isError, framing.content
    report = text_json(framing)
    assert 0 < report["coverage_pct"] <= 100 and report["unit"] == "percent_of_frame"


def test_mcp_client_builds_lit_product_shot_and_gets_preview(server_env: dict[str, str]) -> None:
    check_product_shot(run_client(server_env, product_shot))


def test_errors_are_is_error_results_with_taxonomy_codes(server_env: dict[str, str]) -> None:
    async def body(client: ClientSession) -> list[types.CallToolResult]:
        bad_args = await client.call_tool("call", {"tool": "camera.set_lens", "args": {"mm": 0}})
        unknown = await client.call_tool("call", {"tool": "no.such_tool"})
        await client.call_tool("call", {"tool": "scene.clear"})
        refused = await client.call_tool("preview.render", {})
        return [bad_args, unknown, refused]

    # Sign with a key the host does not trust: the guard refuses the job.
    env = {**server_env, SIGNING_KEY_ENV: generate_keypair()[0]}
    results = run_client(env, body)
    codes = []
    for result in results:
        assert result.isError
        codes.append(text_json(result)["error"]["code"])
    assert codes == ["args.invalid", "tool.unknown", "host.refused"]
    assert set(codes) <= ERROR_CODES
