# SPDX-License-Identifier: AGPL-3.0-or-later
"""Harness animation tools (object.keyframe, render.set_frame_range, object.animate) and
particles.add."""

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
NEW_TOOLS = ["object.keyframe", "render.set_frame_range", "object.animate", "particles.add"]

KEYS = [
    {"frame": 1, "location": [0, 0, 0], "inputs": {"emission_strength": 0}},
    {"frame": 24, "location": [0, 0, 1], "lens": 50, "inputs": {"alpha": 0.5}},
]
CALLS: list[tuple[str, dict[str, Any]]] = [
    ("object.keyframe", {"object": "Beam", "keys": KEYS, "easing": "ease_out", "material": "M"}),
    ("render.set_frame_range", {"frame_start": 1, "frame_end": 96}),
    (
        "object.animate",
        {
            "object": "Tier",
            "preset": "shatter",
            "frame_start": 1,
            "frame_end": 30,
            "pieces": 8,
            "seed": 3,
            "distance": 0.5,
        },
    ),
    (
        "particles.add",
        {
            "emitter": "Room",
            "name": "Dust",
            "count": 2000,
            "seed": 7,
            "lifetime": 240,
            "size": 0.004,
            "velocity": 0.05,
            "frame_start": 1,
            "frame_end": 96,
            "instance": "Mote",
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


def test_keyframe_step_keeps_only_set_channels(session: Session) -> None:
    call(session, "object.keyframe", {"object": "Beam", "keys": KEYS})
    ((_, args),) = steps_of(session)
    assert args["keys"] == [
        {"frame": 1, "location": [0.0, 0.0, 0.0], "inputs": {"emission_strength": 0.0}},
        {"frame": 24, "location": [0.0, 0.0, 1.0], "lens": 50.0, "inputs": {"alpha": 0.5}},
    ]


def test_animate_records_the_from_key(session: Session) -> None:
    args = {
        "object": "Tier",
        "preset": "reveal",
        "frame_start": 1,
        "frame_end": 30,
        "from": "position",
        "offset": [0, 0, -1],
    }
    call(session, "object.animate", args)
    assert steps_of(session) == [
        (
            "object.animate",
            {
                "object": "Tier",
                "preset": "reveal",
                "frame_start": 1,
                "frame_end": 30,
                "from": "position",
                "offset": [0.0, 0.0, -1.0],
            },
        )
    ]
    assert "from" in registry()["object.animate"].schema()["properties"]


TWO = [{"frame": 0, "location": [0, 0, 0]}, {"frame": 5, "location": [1, 0, 0]}]
ANIM = {"object": "Tier", "frame_start": 1, "frame_end": 30}


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("object.keyframe", {"object": "Box", "keys": TWO[:1]}),
        ("object.keyframe", {"object": "Box", "keys": [TWO[1], TWO[0]]}),
        ("object.keyframe", {"object": "Box", "keys": [{"frame": 0}, TWO[1]]}),
        ("object.keyframe", {"object": "Box", "keys": TWO, "easing": "bounce"}),
        ("object.keyframe", {"object": "Box", "keys": TWO, "material": "M"}),
        ("object.keyframe", {"object": "Box", "keys": TWO, "python": "import os"}),
        ("object.keyframe", {"object": "Box", "keys": [{"frame": 0, "inputs": {}}, TWO[1]]}),
        (
            "object.keyframe",
            {"object": "Box", "keys": [{"frame": 0, "inputs": {"ior": 1}}, TWO[1]]},
        ),
        (
            "object.keyframe",
            {"object": "Box", "keys": [{"frame": 0, "inputs": {"alpha": 2}}, TWO[1]]},
        ),
        ("render.set_frame_range", {"frame_start": 5, "frame_end": 5}),
        ("render.set_frame_range", {"frame_start": -1, "frame_end": 5}),
        ("object.animate", {**ANIM, "preset": "spin"}),
        ("object.animate", {**ANIM, "preset": "turntable", "seed": 1}),
        ("object.animate", {**ANIM, "preset": "reveal", "from": "position"}),
        ("object.animate", {**ANIM, "preset": "reveal", "offset": [0, 0, 1]}),
        ("object.animate", {**ANIM, "preset": "shatter", "pieces": 1}),
        ("object.animate", {**ANIM, "preset": "assemble", "axis": "x"}),
        ("object.animate", {**ANIM, "preset": "turntable", "frame_end": 1}),
        ("particles.add", {"emitter": "Room", "count": 0}),
        ("particles.add", {"emitter": "Room", "size": 0}),
        ("particles.add", {"emitter": "Room", "seed": -1}),
        ("particles.add", {"emitter": "Room", "instance": "Room"}),
        ("particles.add", {"emitter": "Room", "frame_start": 10, "frame_end": 5}),
        ("particles.add", {"emitter": "Room", "python": "import os"}),
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


def test_doc_animation_and_particles_sections_name_every_tool() -> None:
    text = DOC.read_text()
    assert "Filled in by a later sub-issue (#285)" not in text
    animation = text.split("## Animation")[1].split("## Particles")[0]
    particles = text.split("## Particles")[1]
    for name in NEW_TOOLS[:-1]:
        assert f"`{name}`" in animation, name
    for field in ["easing", "lens", "emission_strength", "alpha", "material", "from", "seed"]:
        assert f"`{field}`" in animation, field
    for easing in ["linear", "ease_in", "ease_out", "ease_in_out"]:
        assert f"`{easing}`" in animation, easing
    assert "`particles.add`" in particles
    for field in ["emitter", "count", "seed", "lifetime", "size", "velocity", "instance"]:
        assert f"`{field}`" in particles, field
