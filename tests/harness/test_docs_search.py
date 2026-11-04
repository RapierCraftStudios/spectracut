# SPDX-License-Identifier: AGPL-3.0-or-later
"""Docs search tools: ranking, allowlist ``settable_via``, overrides, and the fixture task.

The fixture task (``fixtures/tasks/transparent_background.json``) needs a bpy setting
the client does not know. A scripted MCP stdio client solves it only from a
``docs.search_api`` hit: the hit's path and ``settable_via`` drive the build call.
The opt-in variant hands the same task to a real agent (``SPECTRACUT_E2E=1``).
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, get_args

import pytest
from mcp import ClientSession
from test_server_stdio import run_client, text_json  # type: ignore[import-not-found]

from spectracut.harness.docs.search import (
    BPY_DOCS_ENV,
    RNA_TO_PROPERTY,
    load_index,
    search,
    settable_via,
)
from spectracut.harness.errors import HarnessError
from spectracut.harness.recorder import load_recipe
from spectracut.harness.server import FIXED_TOOLS, call
from spectracut.harness.tools import Session, registry
from spectracut.harness.tools.scene import RenderProperty

ROOT = Path(__file__).resolve().parents[2]
DOCS_SRC = ROOT / "src" / "spectracut" / "harness" / "docs"
TASK: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "tasks" / "transparent_background.json").read_text()
)


def test_api_search_ranks_film_transparent_first() -> None:
    hits = search(TASK["query"], "api", env={})
    assert hits[0]["path"] == "bpy.types.RenderSettings.film_transparent"
    assert hits[0]["signature"].startswith("film_transparent: bool")
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    assert search(TASK["query"], "api", env={}) == hits


def test_manual_search_returns_in_house_topics() -> None:
    hits = search("transparent background", "manual", env={})
    assert hits[0]["path"] == "manual/render/transparent-background"
    assert "bpy.types.RenderSettings.film_transparent" in hits[0]["see"]
    assert all("settable_via" not in h for h in hits)
    assert search("depth of field", "manual", limit=1, env={})[0]["path"].endswith("depth-of-field")


def test_every_allowlisted_property_hit_names_its_setting_tool() -> None:
    allowlist = set(get_args(RenderProperty))
    assert set(RNA_TO_PROPERTY.values()) == allowlist
    index = {e["path"]: e for e in load_index("api", env={})}
    for path, key in RNA_TO_PROPERTY.items():
        hit = search(path, "api", limit=1, env={})[0]
        assert hit["path"] == path
        via = {"tool": "render.set_property", "args": {"property": key}}
        assert hit["settable_via"] == via
        assert index[path]["settable_via"] == via
    others = [e for p, e in index.items() if p not in RNA_TO_PROPERTY]
    assert others and all("settable_via" not in e for e in others)
    assert settable_via("bpy.types.Camera.lens") is None


def test_override_dump_replaces_seed_and_gets_settable_via(tmp_path: Path) -> None:
    dump = tmp_path / "full.json"
    dump.write_text(
        json.dumps(
            [
                {"path": "bpy.types.RenderSettings.fps", "title": "FPS", "text": "rate"},
                {
                    "path": "bpy.types.Mesh.vertices",
                    "title": "Vertices",
                    "settable_via": {"tool": "x"},
                },
            ]
        )
    )
    env = {BPY_DOCS_ENV: str(dump)}
    entries = load_index("api", env=env)
    assert [e["path"] for e in entries] == [
        "bpy.types.RenderSettings.fps",
        "bpy.types.Mesh.vertices",
    ]
    assert entries[0]["settable_via"]["args"] == {"property": "render.fps"}
    assert "settable_via" not in entries[1]
    assert search("vertices", "api", env=env)[0]["path"] == "bpy.types.Mesh.vertices"
    assert search("transparent", "api", env=env) == []
    assert search("transparent", "manual", env=env)


@pytest.mark.parametrize("content", ["{not json", '{"path": "x"}', '[{"title": "no path"}]'])
def test_bad_override_is_config_invalid(tmp_path: Path, content: str) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(content)
    with pytest.raises(HarnessError) as err:
        load_index("api", env={BPY_DOCS_ENV: str(bad)})
    assert err.value.code == "config.invalid"
    with pytest.raises(HarnessError) as missing:
        load_index("api", env={BPY_DOCS_ENV: str(tmp_path / "nope.json")})
    assert missing.value.code == "config.invalid"


def test_docs_tools_are_discoverable_meta_tools_that_record_no_steps(session: Session) -> None:
    for name in ("docs.search_api", "docs.search_manual"):
        assert registry()[name].kind == "meta" and not registry()[name].steps
        assert name not in FIXED_TOOLS
    listed = call(session, "discover", {"prefix": "docs."}).data["tools"]
    assert [t["name"] for t in listed] == ["docs.search_api", "docs.search_manual"]
    out = call(session, "call", {"tool": "docs.search_api", "args": {"query": "film transparent"}})
    assert out.data["hits"][0]["settable_via"]["tool"] == "render.set_property"
    assert session.recorder.steps == []
    for bad in ({"query": "x"}, {"query": "ok", "limit": 0}, {"query": "ok", "extra": 1}):
        with pytest.raises(HarnessError) as err:
            call(session, "docs.search_api", bad)
        assert err.value.code == "args.invalid"
    with pytest.raises(HarnessError) as err:
        call(session, "docs.search_manual", {"query": "!!"})
    assert err.value.code == "args.invalid"


def test_docs_code_never_imports_blender_and_dump_is_not_a_runtime_tool() -> None:
    sources = [*DOCS_SRC.rglob("*.py"), ROOT / "src/spectracut/harness/tools/docs.py"]
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(n.split(".")[0] in {"bpy", "bpy_runtime"} for n in names), path
    assert "dump_docs" not in (ROOT / "host/bpy_runtime/tools.py").read_text()
    assert "dump_docs" not in (ROOT / "host/bpy_runtime/runner.py").read_text()


def test_dump_docs_writes_the_index_schema(tmp_path: Path) -> None:
    fake = tmp_path / "bpy.py"
    fake.write_text(
        "from types import SimpleNamespace as N\n"
        "p = N(identifier='film_transparent', name='Transparent', description='alpha bg',\n"
        "      type='BOOLEAN', array_length=0, is_readonly=False)\n"
        "rt = N(identifier='rna_type')\n"
        "f = N(identifier='frame_path', description='path of a frame',\n"
        "      parameters=[N(identifier='frame', is_output=False),\n"
        "                  N(identifier='filepath', is_output=True)])\n"
        "types = N(RenderSettings=N(bl_rna=N(properties=[rt, p], functions=[f])), Plain=1)\n"
    )
    out = tmp_path / "out.json"
    script = ROOT / "host" / "bpy_runtime" / "dump_docs.py"
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    run = subprocess.run(
        [sys.executable, str(script), "--", str(out)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    entries = json.loads(out.read_text())
    assert entries[0]["path"] == "bpy.types.RenderSettings.film_transparent"
    assert entries[1]["signature"] == "frame_path(frame)"
    env_ok = {BPY_DOCS_ENV: str(out)}
    hit = search("transparent", "api", env=env_ok)[0]
    assert hit["settable_via"]["args"]["property"] == "render.film_transparent"


# --- fixture task over a real MCP stdio client ------------------------------------------


async def solve_from_search(client: ClientSession) -> dict[str, Any]:
    """Build the base shot, then reach the goal only through a docs search hit."""
    for tool, args in TASK["base"]:
        result = await client.call_tool("call", {"tool": tool, "args": args})
        assert not result.isError, result.content
    found = text_json(
        await client.call_tool(
            "call", {"tool": "docs.search_api", "args": {"query": TASK["query"], "limit": 3}}
        )
    )
    hit = found["hits"][0]
    via = hit["settable_via"]
    args = {**via["args"], "value": TASK["value"]}
    built = await client.call_tool("call", {"tool": via["tool"], "args": args})
    assert not built.isError, built.content
    preview = await client.call_tool("preview.render", {})
    return {"hit": hit, "preview_error": preview.isError, "preview": text_json(preview)}


def assert_task_solved(recipe_path: Path) -> None:
    steps = [(s.tool, s.args) for s in load_recipe(recipe_path).steps]
    expect = TASK["expect"]
    assert (expect["tool"], expect["args"]) in steps


def test_scripted_client_solves_fixture_only_via_search_hit(server_env: dict[str, str]) -> None:
    got = run_client(server_env, solve_from_search)
    assert got["hit"]["path"] == "bpy.types.RenderSettings.film_transparent"
    assert got["hit"]["settable_via"]["tool"] == TASK["expect"]["tool"]
    assert not got["preview_error"], got["preview"]
    assert got["preview"]["recipe_hash"].startswith("sha256:")
    assert_task_solved(Path(server_env["SPECTRACUT_HARNESS_DIR"]) / "recipe.json")


CLAUDE = os.environ.get("SPECTRACUT_CLAUDE_BIN") or shutil.which("claude")


@pytest.mark.e2e
@pytest.mark.skipif(os.environ.get("SPECTRACUT_E2E") != "1", reason="set SPECTRACUT_E2E=1")
@pytest.mark.skipif(CLAUDE is None, reason="needs the claude CLI or SPECTRACUT_CLAUDE_BIN")
def test_real_agent_solves_fixture_with_docs_search(server_env: dict[str, str]) -> None:
    assert CLAUDE is not None
    config = {
        "mcpServers": {
            "spectracut": {
                "command": sys.executable,
                "args": ["-m", "spectracut.cli.app", "mcp"],
                "env": server_env,
            }
        }
    }
    base = json.dumps(TASK["base"])
    prompt = (
        f"Use only the spectracut MCP tools. First build this scene with the `call` tool, "
        f"one call per step [tool, args]: {base}. Then: {TASK['goal']} You do not know the "
        "setting: find it with docs.search_api (through `call`) and set it exactly as the "
        "hit's settable_via says. Finish with preview.render."
    )
    run = subprocess.run(
        [
            CLAUDE,
            "-p",
            prompt,
            "--strict-mcp-config",
            "--mcp-config",
            json.dumps(config),
            "--allowedTools",
            "mcp__spectracut__*",
            "--no-session-persistence",
        ],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    assert_task_solved(Path(server_env["SPECTRACUT_HARNESS_DIR"]) / "recipe.json")
