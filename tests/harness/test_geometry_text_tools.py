# SPDX-License-Identifier: AGPL-3.0-or-later
"""Harness geometry tools (curves, slabs, arrays, booleans, parenting) and text.add."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from spectracut_host.guard import runtime_tool_names

from spectracut.harness.errors import HarnessError
from spectracut.harness.recorder import Recorder
from spectracut.harness.server import call
from spectracut.harness.tools import Session, registry

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_NAMES = runtime_tool_names(ROOT / "host" / "bpy_runtime" / "tools.py")
DOC = ROOT / "docs" / "engineering" / "harness-tools.md"
SHA = "b" * 64
NEW_TOOLS = [
    "curve.add",
    "object.add_slab",
    "object.array",
    "object.boolean",
    "object.parent",
    "text.add",
]

CALLS: list[tuple[str, dict[str, Any]]] = [
    ("curve.add", {"name": "Beam", "points": [[0, 0, 0], [0, 0, 2]], "bevel_depth": 0.03}),
    ("object.add_slab", {"name": "Tier", "dimensions": [2, 2, 0.1], "bevel_width": 0.02}),
    ("object.array", {"object": "Tier", "count": 5, "offset": [0, 0, 0.3], "mode": "linked"}),
    ("object.boolean", {"object": "Tier", "cutter": "Bore", "operation": "difference"}),
    ("object.parent", {"child": "Beam", "parent": "Tier"}),
    (
        "text.add",
        {
            "body": "ONE BEAM",
            "mode": "cutout",
            "plate": "Tier",
            "font": {"path": "fonts/Inter.ttf", "sha256": SHA},
        },
    ),
]


def error_of(session: Session, name: str, args: dict[str, Any]) -> HarnessError:
    with pytest.raises(HarnessError) as info:
        call(session, name, args)
    return info.value


def steps_of(session: Session) -> list[tuple[str, dict[str, Any]]]:
    return [(s.tool, dict(s.args)) for s in session.recorder.steps]


@pytest.mark.parametrize(("tool", "args"), CALLS, ids=[c[0] for c in CALLS])
def test_each_tool_records_one_runtime_step_of_its_name(
    session: Session, tool: str, args: dict[str, Any]
) -> None:
    out = call(session, tool, args)
    assert out.data["recorded"] == [{"index": 0, "tool": tool}]
    ((recorded, recorded_args),) = steps_of(session)
    assert recorded == tool
    assert set(recorded_args) == set(args)


def test_unset_fields_are_left_out(session: Session) -> None:
    call(session, "text.add", {"body": "Hi"})
    call(session, "object.add_slab", {"dimensions": [1, 1, 1]})
    assert steps_of(session) == [
        ("text.add", {"body": "Hi"}),
        ("object.add_slab", {"dimensions": [1.0, 1.0, 1.0]}),
    ]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("curve.add", {"points": [[0, 0, 0]]}),
        ("curve.add", {"points": [[0, 0, 0], [1, 1, 1]], "kind": "nurbs"}),
        ("curve.add", {"points": [[0, 0, 0], [1, 1, 1]], "python": "import os"}),
        ("object.add_slab", {"dimensions": [1, 0, 1]}),
        ("object.add_slab", {"dimensions": [1, 1, 0.1], "bevel_width": 0.06}),
        ("object.array", {"object": "Box", "count": 1, "offset": [1, 0, 0]}),
        ("object.boolean", {"object": "Box", "cutter": "Box"}),
        ("object.parent", {"child": "Box", "parent": "Box"}),
        ("text.add", {"body": ""}),
        ("text.add", {"body": "Hi", "mode": "cutout"}),
        ("text.add", {"body": "Hi", "plate": "Box"}),
        ("text.add", {"body": "Hi", "font": {"path": "fonts/a.ttf", "sha256": "XYZ"}}),
        ("text.add", {"body": "a\x00b"}),
    ],
)
def test_bad_args_are_refused_without_recording(
    session: Session, tool: str, args: dict[str, Any]
) -> None:
    assert error_of(session, tool, args).code == "args.invalid"
    assert session.recorder.steps == []


def test_new_tools_are_typed_and_record_only_runtime_steps() -> None:
    tools = registry()
    for name in NEW_TOOLS:
        tool = tools[name]
        assert tool.kind == "build", name
        assert tool.steps == frozenset({name}) <= RUNTIME_NAMES, name
        assert tool.schema()["additionalProperties"] is False, name


def test_recording_is_deterministic(dispatcher: Any) -> None:
    def record() -> list[Any]:
        session = Session(Recorder(), dispatcher)
        for tool, args in CALLS:
            call(session, tool, args)
        return [(s.tool, s.args) for s in session.recorder.steps]

    assert record() == record()


def test_doc_geometry_and_text_sections_name_every_tool() -> None:
    text = DOC.read_text()
    geometry = text.split("## Geometry")[1].split("## Text")[0]
    text_section = text.split("## Text")[1].split("## Animation")[0]
    assert "Filled in by a later sub-issue (#284)" not in text
    for name in NEW_TOOLS[:-1]:
        assert f"`{name}`" in geometry, name
    assert "`text.add`" in text_section
    for field in ["body", "mode", "plate", "font", "sha256", "cutout", "depth"]:
        assert field in text_section, field
    for field in ["bevel_depth", "bevel_width", "segments", "count", "offset", "cutter"]:
        assert f"`{field}`" in geometry, field
