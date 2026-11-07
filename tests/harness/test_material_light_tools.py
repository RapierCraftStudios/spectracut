# SPDX-License-Identifier: AGPL-3.0-or-later
"""Harness material presets/overrides/brand colour, light size/colour/spot, rim rig, world tools."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from spectracut_host.guard import runtime_tool_names

from spectracut.finishing.color import srgb_encode
from spectracut.harness.errors import HarnessError
from spectracut.harness.recorder import Recorder
from spectracut.harness.server import call
from spectracut.harness.tools import Session, registry
from spectracut.harness.tools.light import HAZE_DENSITY_MAX, RIGS

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_NAMES = runtime_tool_names(ROOT / "host" / "bpy_runtime" / "tools.py")
DOC = ROOT / "docs" / "engineering" / "harness-tools.md"
WORLD_PY = ROOT / "host" / "bpy_runtime" / "ops" / "world.py"
NEW_PRESETS = ["clear_glass", "frosted_glass", "smoked_glass", "emissive", "anodised_metal"]
WORLD_TOOLS = ["world.set_hdri", "world.set_gradient", "world.set_haze"]
SHA = "a" * 64


def error_of(session: Session, name: str, args: dict[str, Any]) -> HarnessError:
    with pytest.raises(HarnessError) as info:
        call(session, name, args)
    return info.value


def steps_of(session: Session) -> list[tuple[str, dict[str, Any]]]:
    return [(s.tool, dict(s.args)) for s in session.recorder.steps]


# --- material.apply_preset ------------------------------------------------------------------


@pytest.mark.parametrize("preset", NEW_PRESETS)
def test_new_presets_record_one_step(session: Session, preset: str) -> None:
    call(session, "material.apply_preset", {"object": "Slab", "preset": preset})
    assert steps_of(session) == [("material.apply_preset", {"object": "Slab", "preset": preset})]


def test_overrides_are_typed_and_recorded(session: Session) -> None:
    args = {
        "object": "Slab",
        "preset": "smoked_glass",
        "roughness": 0.1,
        "ior": 1.45,
        "transmission": 0.95,
        "emission_color": [0, 0.2, 1],
        "emission_strength": 12,
        "anisotropy": 0.4,
        "metallic": 0.0,
    }
    call(session, "material.apply_preset", args)
    [(tool, recorded)] = steps_of(session)
    assert tool == "material.apply_preset"
    assert recorded == {**args, "emission_strength": 12.0, "emission_color": [0.0, 0.2, 1.0]}


def test_brand_colour_is_recorded_as_a_numeric_colour(session: Session) -> None:
    call(
        session,
        "material.apply_preset",
        {"object": "Slab", "preset": "anodised_metal", "brand_color": "#3366FF"},
    )
    [(_, recorded)] = steps_of(session)
    assert recorded == {
        "object": "Slab",
        "preset": "anodised_metal",
        "color": [0.033105, 0.132868, 1.0],
    }
    assert session.recorder.calls[-1].args["brand_color"] == "#3366FF"


def test_brand_colour_is_linearised_so_the_swatch_round_trips(session: Session) -> None:
    call(
        session,
        "material.apply_preset",
        {"object": "Backdrop", "preset": "matte_plastic", "brand_color": "#0b1220"},
    )
    [(_, recorded)] = steps_of(session)
    colour = recorded["color"]
    assert colour == pytest.approx([0.003347, 0.006049, 0.014444], abs=1e-4)
    assert [round(srgb_encode(c) * 255) for c in colour] == [0x0B, 0x12, 0x20]


def test_brand_emissive_defaults_to_strength_one(session: Session) -> None:
    call(
        session,
        "material.apply_preset",
        {"object": "Beam", "preset": "emissive", "brand_color": "#2f6bff"},
    )
    [(_, recorded)] = steps_of(session)
    assert recorded == {
        "object": "Beam",
        "preset": "emissive",
        "color": [0.028426, 0.147027, 1.0],
        "emission_strength": 1.0,
    }


def test_brand_emissive_refuses_strength_above_one(session: Session) -> None:
    args = {
        "object": "Beam",
        "preset": "emissive",
        "brand_color": "#2f6bff",
        "emission_strength": 1.5,
    }
    error = error_of(session, "material.apply_preset", args)
    assert error.code == "args.invalid"
    assert "emission_strength" in str(error)
    assert session.recorder.steps == []


def test_numeric_emissive_colour_keeps_its_strength_unbounded(session: Session) -> None:
    args = {
        "object": "Beam",
        "preset": "emissive",
        "color": [0.1, 0.4, 1.0],
        "emission_strength": 40,
    }
    call(session, "material.apply_preset", args)
    [(_, recorded)] = steps_of(session)
    assert recorded["emission_strength"] == 40.0


@pytest.mark.parametrize(
    "extra",
    [
        {"brand_color": "#ffffff", "color": [1, 1, 1]},
        {"brand_color": "blue"},
        {"roughness": 2},
        {"ior": 0.9},
        {"emission_strength": -1},
        {"anisotropy": 1.1},
        {"seed": -1},
        {"seed": "3"},
        {"seed": 2**31},
        {"bump_kind": "perlin"},
        {"bump_strength": 1.2},
        {"bump_scale": 0},
        {"brushing_angle": 360},
        {"scratches": -0.1},
        {"edge_wear": 2},
    ],
)
def test_material_refusals(session: Session, extra: dict[str, Any]) -> None:
    args = {"object": "Slab", "preset": "clear_glass", **extra}
    assert error_of(session, "material.apply_preset", args).code == "args.invalid"
    assert session.recorder.steps == []


DETAIL = {
    "bump_kind": "voronoi",
    "bump_strength": 0.3,
    "bump_scale": 220.0,
    "brushing": 0.6,
    "brushing_angle": 45.0,
    "scratches": 0.2,
    "edge_wear": 0.5,
    "seed": 11,
}


def test_surface_detail_is_typed_and_recorded_in_one_step(session: Session) -> None:
    args = {"object": "Slab", "preset": "brushed_metal", **DETAIL}
    call(session, "material.apply_preset", args)
    assert steps_of(session) == [("material.apply_preset", args)]


def test_surface_detail_recording_is_deterministic(dispatcher: Any) -> None:
    def record() -> list[tuple[str, dict[str, Any]]]:
        session = Session(Recorder(), dispatcher)
        call(session, "material.apply_preset", {"object": "S", "preset": "rubber", **DETAIL})
        return steps_of(session)

    assert record() == record()


# --- light.add ------------------------------------------------------------------------------


def test_single_light_size_colour_spot_angle(session: Session) -> None:
    call(session, "light.add", {"kind": "area", "name": "Soft", "size": 2, "color": [1, 0.9, 0.8]})
    call(session, "light.add", {"kind": "spot", "name": "Beam", "spot_angle": 25})
    assert steps_of(session) == [
        ("light.add", {"kind": "area", "name": "Soft", "size": 2.0, "color": [1.0, 0.9, 0.8]}),
        ("light.add", {"kind": "spot", "name": "Beam", "spot_angle": 25.0}),
    ]


def test_single_light_rectangle_hidden_from_camera(session: Session) -> None:
    rim = {"kind": "area", "name": "Rim", "size": 0.06, "size_y": 1.2, "camera_visible": False}
    call(session, "light.add", rim)
    assert steps_of(session) == [("light.add", rim)]


@pytest.mark.parametrize(
    "args",
    [
        {"rig": "rim_area", "size": 2},
        {"rig": "three_point", "color": [1, 1, 1]},
        {"kind": "point", "size": 1},
        {"kind": "area", "spot_angle": 30},
        {"kind": "spot", "spot_angle": 0},
        {"kind": "area", "size": 0},
        {"kind": "point", "size_y": 1},
        {"rig": "rim_area", "camera_visible": False},
    ],
)
def test_light_refusals(session: Session, args: dict[str, Any]) -> None:
    assert error_of(session, "light.add", args).code == "args.invalid"
    assert session.recorder.steps == []


def test_rim_area_rig_aims_area_lights_at_the_target(session: Session) -> None:
    out = call(session, "light.add", {"rig": "rim_area", "name": "Hero", "target": [0, 0, 1]})
    steps = session.recorder.steps
    assert len(out.data["recorded"]) == len(RIGS["rim_area"]) == len(steps) == 3
    assert [s.args["name"] for s in steps] == ["HeroKey", "HeroRimLeft", "HeroRimRight"]
    assert {s.args["kind"] for s in steps} == {"area"}
    rims: list[Any] = [s.args["location"] for s in steps[1:]]
    assert all(loc[1] > 0 for loc in rims)  # behind the subject (camera looks along +Y)


# --- world tools ----------------------------------------------------------------------------


def test_world_tools_record_one_runtime_step_each(session: Session) -> None:
    call(session, "world.set_hdri", {"path": "hdri/studio.hdr", "sha256": SHA, "strength": 2})
    call(session, "world.set_gradient", {"bottom": [0, 0, 0.1], "top": [0.1, 0.2, 0.4]})
    call(session, "world.set_haze", {"density": 0.01, "anisotropy": 0.2})
    assert steps_of(session) == [
        ("world.set_hdri", {"path": "hdri/studio.hdr", "sha256": SHA, "strength": 2.0}),
        ("world.set_gradient", {"bottom": [0.0, 0.0, 0.1], "top": [0.1, 0.2, 0.4]}),
        ("world.set_haze", {"density": 0.01, "anisotropy": 0.2}),
    ]


def test_gradient_brand_ends_record_linear_numeric_colours(session: Session) -> None:
    call(session, "world.set_gradient", {"brand_bottom": "#0b1220", "top": [0.1, 0.2, 0.4]})
    call(
        session, "world.set_gradient", {"bottom": [0, 0, 0], "brand_top": "#1a2b3c", "strength": 1}
    )
    assert steps_of(session) == [
        ("world.set_gradient", {"bottom": [0.003347, 0.006049, 0.014444], "top": [0.1, 0.2, 0.4]}),
        (
            "world.set_gradient",
            {"bottom": [0.0, 0.0, 0.0], "top": [0.01033, 0.024158, 0.045186], "strength": 1.0},
        ),
    ]
    assert session.recorder.calls[0].args["brand_bottom"] == "#0b1220"


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("world.set_gradient", {"bottom": [0, 0, 0], "brand_bottom": "#000000", "top": [0, 0, 0]}),
        ("world.set_gradient", {"bottom": [0, 0, 0], "top": [0, 0, 0], "brand_top": "#000000"}),
        ("world.set_gradient", {"top": [0, 0, 0]}),
        ("world.set_gradient", {"brand_bottom": "#000000"}),
        ("world.set_gradient", {"brand_bottom": "navy", "brand_top": "#000000"}),
        ("world.set_haze", {"density": HAZE_DENSITY_MAX * 2}),
        ("world.set_haze", {"density": 0}),
        ("world.set_gradient", {"bottom": [0, 0, 0]}),
        ("world.set_gradient", {"bottom": [0, 0, 0], "top": [1, 1, 2]}),
        ("world.set_hdri", {"path": "a.hdr", "sha256": "XYZ"}),
    ],
)
def test_world_refusals(session: Session, tool: str, args: dict[str, Any]) -> None:
    assert error_of(session, tool, args).code == "args.invalid"
    assert session.recorder.steps == []


def test_harness_haze_cap_matches_the_runtime_cap() -> None:
    match = re.search(r"^HAZE_DENSITY_MAX = ([0-9.]+)$", WORLD_PY.read_text(), re.M)
    assert match and float(match.group(1)) == HAZE_DENSITY_MAX


# --- typed, runtime-backed, deterministic ---------------------------------------------------


def test_new_tools_are_typed_and_record_only_runtime_steps() -> None:
    tools = registry()
    for name in ["material.apply_preset", "light.add", *WORLD_TOOLS]:
        tool = tools[name]
        assert tool.steps <= RUNTIME_NAMES, name
        assert tool.schema()["additionalProperties"] is False, name


def test_recording_is_deterministic(dispatcher: Any) -> None:
    def record() -> list[Any]:
        session = Session(Recorder(), dispatcher)
        call(session, "material.apply_preset", {"object": "S", "preset": "emissive"})
        call(session, "light.add", {"rig": "rim_area", "target": [0, 0, 1]})
        call(session, "world.set_gradient", {"bottom": [0, 0, 0], "top": [0, 0, 1]})
        call(session, "world.set_haze", {"density": 0.02})
        return [(s.tool, s.args) for s in session.recorder.steps]

    assert record() == record()


# --- docs -----------------------------------------------------------------------------------


def test_harness_tools_doc_has_all_sections_and_names_every_tool() -> None:
    text = DOC.read_text()
    headings = re.findall(r"^## (.+)$", text, re.M)
    assert headings == [
        "Materials",
        "Lighting & world",
        "Geometry",
        "Text",
        "Animation",
        "Particles",
    ]
    materials = text.split("## Materials")[1].split("## Lighting & world")[0]
    lighting = text.split("## Lighting & world")[1].split("## Geometry")[0]
    preset_field = registry()["material.apply_preset"].schema()["properties"]["preset"]
    for preset in preset_field["enum"]:
        assert f"`{preset}`" in materials, preset
    for field in [
        "roughness",
        "ior",
        "transmission",
        "emission_color",
        "emission_strength",
        "anisotropy",
        "metallic",
        "brand_color",
        *DETAIL,
    ]:
        assert f"`{field}`" in materials, field
    for rig in RIGS:
        assert f"`{rig}`" in lighting, rig
    for name in ["light.add", "size", "spot_angle", *WORLD_TOOLS]:
        assert f"`{name}`" in lighting, name
