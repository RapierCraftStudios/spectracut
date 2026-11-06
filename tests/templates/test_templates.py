# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shot template library (#199): brand-kit-driven recipes on the host's template ops.

Unit criteria: the same template renders two different brand kits with no code
change; screen emission is exposed so UI white reads as display white; the 9:16
camera is its own re-framed camera, never a crop; the final tier reads the Look
from the kit and writes render passes. The farm criteria (preview time, dolly
moire) are in ``test_templates_e2e.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from spectracut_host.guard import runtime_tool_names

from spectracut.finishing.color import (
    SCREEN_WHITE_TARGET,
    ColorPipeline,
    screen_white_strength,
    view_encode,
)
from spectracut.finishing.typography import FontFile
from spectracut.spec.asset import LicenseRecord
from spectracut.spec.brand import BrandKit, load_brand_kit
from spectracut.spec.recipe import Recipe
from spectracut.studio.device_shot import referenced_assets
from spectracut.templates import (
    ASPECTS,
    END_CARD,
    HERO_OBJECT,
    LAPTOP,
    PHONE,
    TEMPLATES,
    TYPOGRAPHY,
    CameraVariant,
    Template,
    TemplateError,
)
from spectracut.templates.base import CAMERA, VERTICAL_CAMERA, swatch
from spectracut.templates.devices import DeviceSlots

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "brands" / "alterlab" / "brand-kit.yaml"
TOOLS_PY = ROOT / "host" / "bpy_runtime" / "tools.py"
TEMPLATE_SOURCES = ROOT / "src" / "spectracut" / "templates"

SHA = "a" * 64
SHA_B = "b" * 64

# Argument keys each host op accepts (``check_only`` in host/bpy_runtime).
HOST_ARGS: dict[str, set[str]] = {
    "scene.clear": set(),
    "object.add_primitive": {"kind", "name", "location", "scale"},
    "object.import_gltf": {"path", "name", "location", "scale"},
    "material.apply_preset": {"object", "preset", "color", "name"},
    "material.apply_screen": {"object", "image", "image_sha256", "strength"},
    "camera.add": {"name", "location", "rotation", "lens"},
    "camera.set_lens": {"mm"},
    "camera.set_transform": {"location", "rotation"},
    "camera.add_aspect": {"aspect", "name"},
    "camera.use": {"name", "long_edge"},
    "light.add": {"kind", "name", "energy", "location", "rotation"},
    "device.add": {"kind", "name", "location", "rotation", "screen", "emission_strength", "grid"},
    "text.add": {
        "body",
        "name",
        "size",
        "extrude",
        "mode",
        "plate",
        "location",
        "rotation",
        "font",
    },
    "world.set_hdri": {"path", "sha256", "strength", "rotation"},
    "render.set_engine": {"engine"},
    "render.set_resolution": {"width", "height", "percentage"},
    "render.set_property": {"property", "value"},
    "render.set_passes": {"passes"},
    "render.set_output": {"format", "color_depth"},
}
ANIMATE_EXTRA = {
    "turntable": {"degrees", "axis"},
    "reveal": {"from", "offset"},
    "shatter": {"pieces", "seed", "distance"},
    "assemble": {"pieces", "seed", "distance"},
}
MATERIAL_PRESETS = {
    "matte_plastic",
    "glossy_plastic",
    "brushed_metal",
    "polished_metal",
    "glass",
    "rubber",
    "ceramic",
}


def alterlab() -> BrandKit:
    return load_brand_kit(KIT)


def northwind() -> BrandKit:
    """A second, unrelated brand kit: other palette, other type family, other Looks."""
    return BrandKit.model_validate(
        {
            "id": "northwind",
            "name": "Northwind Labs",
            "palette": {
                "swatches": {
                    "ink": "#201010",
                    "surface": "#efe6d8",
                    "accent": "#c2410c",
                    "moss": "#4d7c0f",
                }
            },
            "typography": {"families": {"display": "Fraunces", "body": "Source Serif"}},
            "tone": "Warm and plain-spoken.",
            "looks": [
                {"id": "filmic", "motion_blur": True, "depth_of_field": True},
                {"id": "still", "motion_blur": False, "depth_of_field": False},
            ],
            "default_look_id": "filmic",
            "forbidden_words": ["unbeatable"],
            "claim_sources": [
                {"id": "site", "title": "Northwind site", "ref": "https://example.org"}
            ],
        }
    )


def font(path: str, sha: str, safe: bool = True) -> FontFile:
    licence = LicenseRecord(
        source="foundry", licence="OFL-1.1" if safe else "proprietary-trial", commercial_safe=safe
    )
    return FontFile(path=path, sha256=sha, licence=licence)


FONTS = {
    "Inter": font("fonts/Inter.ttf", SHA),
    "Fraunces": font("fonts/Fraunces.ttf", SHA_B),
}
DEVICE_SLOTS: dict[str, Any] = {
    "screen_frames": [
        {"path": "ui/screen_0001.png", "sha256": SHA},
        {"path": "ui/screen_0002.png", "sha256": SHA},
    ],
    "hdri": {"path": "hdri/studio.hdr", "sha256": SHA_B},
}
SLOTS: dict[str, dict[str, Any]] = {
    LAPTOP.id: DEVICE_SLOTS,
    PHONE.id: DEVICE_SLOTS,
    HERO_OBJECT.id: {"animation": "shatter", "seed": 7},
    END_CARD.id: {"cta": "See every run at a glance", "fonts": FONTS},
    TYPOGRAPHY.id: {"body": "Ship the film", "mode": "cutout", "fonts": FONTS},
}


def tools(recipe: Recipe) -> list[str]:
    return [s.tool for s in recipe.steps]


def args_of(recipe: Recipe, tool: str) -> list[dict[str, Any]]:
    return [dict(s.args) for s in recipe.steps if s.tool == tool]


def colours(recipe: Recipe) -> list[Any]:
    return [a["color"] for a in args_of(recipe, "material.apply_preset")]


# ---- R2/R17: two brand kits, no code change -------------------------------------------


@pytest.mark.parametrize("template_id", sorted(TEMPLATES))
@pytest.mark.parametrize("aspect", ASPECTS)
def test_same_template_renders_two_brand_kits(template_id: str, aspect: Any) -> None:
    template = TEMPLATES[template_id]
    a = template.final(alterlab(), SLOTS[template_id], aspect=aspect)
    b = template.final(northwind(), SLOTS[template_id], aspect=aspect)
    scene_a = template.build(alterlab(), SLOTS[template_id], aspect=aspect)
    scene_b = template.build(northwind(), SLOTS[template_id], aspect=aspect)
    assert tools(scene_a) == tools(scene_b)
    # the final tiers differ only by each kit's Look render rules (clean forbids DoF)
    assert [t for t in tools(a) if t != "camera.set_dof"] == tools(b)
    assert colours(a) != colours(b)
    assert all(
        c in [swatch(alterlab(), r) for r in ("ink", "surface", "accent")] for c in colours(a)
    )
    assert all(
        c in [swatch(northwind(), r) for r in ("ink", "surface", "accent")] for c in colours(b)
    )
    if template_id in (END_CARD.id, TYPOGRAPHY.id):
        assert args_of(a, "text.add")[0]["font"] == {"path": "fonts/Inter.ttf", "sha256": SHA}
        assert args_of(b, "text.add")[0]["font"] == {"path": "fonts/Fraunces.ttf", "sha256": SHA_B}
    assert a.id.startswith("alterlab-") and b.id.startswith("northwind-")


def test_build_is_deterministic() -> None:
    for template_id, template in TEMPLATES.items():
        once = template.final(alterlab(), SLOTS[template_id])
        again = template.final(alterlab(), SLOTS[template_id])
        assert once.content_hash == again.content_hash


def test_a_kit_without_a_template_swatch_is_refused() -> None:
    data = northwind().model_dump(mode="json")
    data["palette"]["swatches"].pop("surface")
    kit = BrandKit.model_validate(data)
    with pytest.raises(TemplateError, match="no 'surface' swatch"):
        LAPTOP.build(kit, DEVICE_SLOTS)


def test_forbidden_words_are_refused_on_screen() -> None:
    with pytest.raises(TemplateError, match="forbidden word"):
        TYPOGRAPHY.build(northwind(), {"body": "An UNBEATABLE studio"})
    with pytest.raises(TemplateError, match="forbidden word"):
        END_CARD.build(alterlab(), {"cta": "Guaranteed results"})


# ---- R3/R10: screen exposure --------------------------------------------------------


@pytest.mark.parametrize(
    "pipeline",
    [
        ColorPipeline(),
        ColorPipeline(view_transform="AgX", display="sRGB"),
        ColorPipeline(view_transform="Standard", display="Rec.1886"),
        ColorPipeline(view_transform="Standard", display="sRGB"),
    ],
)
def test_ui_white_on_the_screen_encodes_near_display_white(pipeline: ColorPipeline) -> None:
    strength = screen_white_strength(pipeline)
    white = view_encode(strength * 1.0, pipeline)
    assert SCREEN_WHITE_TARGET <= white <= 1.0
    # the lowest such strength: a little less no longer reaches display white
    assert view_encode(strength * 0.98, pipeline) < SCREEN_WHITE_TARGET
    for template in (LAPTOP, PHONE):
        recipe = template.build(alterlab(), DEVICE_SLOTS, pipeline=pipeline)
        (device,) = args_of(recipe, "device.add")
        assert device["emission_strength"] == strength
        assert 0 < device["emission_strength"] <= 100


def test_agx_needs_more_exposure_than_standard() -> None:
    standard = screen_white_strength(ColorPipeline(view_transform="Standard"))
    agx = screen_white_strength(ColorPipeline(view_transform="AgX"))
    assert 0.9 < standard <= 1.0
    assert agx > 4 * standard
    # at unit strength AgX greys UI white out: the calibration is what fixes it
    assert view_encode(1.0, ColorPipeline(view_transform="AgX")) < 0.85


def test_aces_has_no_forward_model_and_is_refused() -> None:
    with pytest.raises(ValueError, match="ACES 2.0"):
        screen_white_strength(ColorPipeline(view_transform="ACES 2.0"))


def test_end_card_logo_plate_is_exposed_like_a_screen() -> None:
    kit = alterlab().model_copy(update={"logo": "alterlab-mark"})
    logo = {"path": "logos/alterlab-mark.png", "sha256": SHA}
    pipeline = ColorPipeline()
    recipe = END_CARD.build(kit, {"cta": "Start a run", "logo": logo}, pipeline=pipeline)
    (screen,) = args_of(recipe, "material.apply_screen")
    assert screen["image"] == "logos/alterlab-mark.png" and screen["image_sha256"] == SHA
    assert screen["strength"] == screen_white_strength(pipeline)


# ---- R4/R16: vertical camera ---------------------------------------------------------


def _vertical_tail(recipe: Recipe) -> list[tuple[str, dict[str, Any]]]:
    i = tools(recipe).index("camera.add_aspect")
    return [(s.tool, dict(s.args)) for s in recipe.steps[i : i + 4]]


@pytest.mark.parametrize("template_id", sorted(TEMPLATES))
def test_vertical_uses_its_own_reframed_camera(template_id: str) -> None:
    template = TEMPLATES[template_id]
    for v in template.variants:
        wide = template.build(alterlab(), SLOTS[template_id], variant=v.id, aspect="16:9")
        tall = template.build(alterlab(), SLOTS[template_id], variant=v.id, aspect="9:16")
        assert "camera.add_aspect" not in tools(wide)
        assert args_of(wide, "camera.use") == [{"name": CAMERA, "long_edge": 1920}]
        assert _vertical_tail(tall) == [
            ("camera.add_aspect", {"aspect": "9:16", "name": VERTICAL_CAMERA}),
            ("camera.use", {"name": VERTICAL_CAMERA, "long_edge": 1920}),
            (
                "camera.set_transform",
                {"location": list(v.vertical_location), "rotation": list(v.vertical_rotation)},
            ),
            ("camera.set_lens", {"mm": v.vertical_lens}),
        ]
        # the vertical framing is composed for itself, not the 16:9 view
        assert (v.vertical_location, v.vertical_rotation, v.vertical_lens) != (
            v.location,
            v.rotation,
            v.lens,
        )
        # nothing after the vertical camera re-targets the 16:9 one
        after = tools(tall)[tools(tall).index("camera.add_aspect") :]
        assert "camera.add" not in after
        final = template.final(alterlab(), SLOTS[template_id], variant=v.id, aspect="9:16")
        (res,) = args_of(final, "render.set_resolution")
        assert (res["width"], res["height"]) == (1080, 1920)


# ---- R5/R8: Looks from the kit, passes at the final tier -----------------------------


def test_final_tier_reads_the_kit_look_and_writes_render_passes() -> None:
    kit = northwind()
    pipeline = ColorPipeline(view_transform="AgX", display="sRGB")
    default = HERO_OBJECT.final(kit, {}, pipeline=pipeline)
    still = HERO_OBJECT.final(kit, {}, look_id="still", pipeline=pipeline)

    def prop(recipe: Recipe, name: str) -> Any:
        return [a["value"] for a in args_of(recipe, "render.set_property") if a["property"] == name]

    assert prop(default, "render.use_motion_blur") == [True]  # filmic, the kit default
    assert prop(still, "render.use_motion_blur") == [False]
    assert args_of(default, "render.set_engine") == [{"engine": "cycles"}]
    assert prop(default, "cycles.samples") and prop(default, "cycles.seed")
    assert prop(default, "view_settings.view_transform") == ["AgX"]
    assert prop(default, "view_settings.display_device") == ["sRGB"]
    (passes,) = args_of(default, "render.set_passes")
    assert passes["passes"]
    assert default.steps[-1].tool == "render.set_output"
    assert default.steps[-1].args["format"] == "OPEN_EXR_MULTILAYER"
    assert [s.index for s in default.steps] == list(range(len(default.steps)))
    with pytest.raises(ValueError, match="no look 'cinematic'"):
        HERO_OBJECT.final(kit, {}, look_id="cinematic")


def test_preview_tier_is_eevee_half_resolution_png() -> None:
    preview = LAPTOP.preview(alterlab(), DEVICE_SLOTS, aspect="9:16")
    assert args_of(preview, "render.set_engine") == [{"engine": "eevee"}]
    assert args_of(preview, "render.set_resolution") == [
        {"width": 1080, "height": 1920, "percentage": 50}
    ]
    assert preview.steps[-1].tool == "render.set_output"
    assert preview.steps[-1].args == {"format": "PNG"}
    assert "render.set_passes" not in tools(preview)


# ---- R15/R7: registry and variants ---------------------------------------------------


def test_every_template_has_at_least_two_camera_variants() -> None:
    assert set(TEMPLATES) == {
        "device-laptop",
        "device-phone",
        "hero-object",
        "end-card",
        "typography",
    }
    for template in TEMPLATES.values():
        ids = [v.id for v in template.variants]
        assert len(ids) >= 2 and len(set(ids)) == len(ids)
    cam = LAPTOP.variants[0]
    with pytest.raises(TemplateError, match="at least 2 camera variants"):
        Template(id="solo", slots=DeviceSlots, variants=(cam,), scene=lambda k, s, p: [])
    with pytest.raises(TemplateError, match="no variant 'aerial'"):
        LAPTOP.build(alterlab(), DEVICE_SLOTS, variant="aerial")
    assert isinstance(cam, CameraVariant)


# ---- R21: guardrails ----------------------------------------------------------------


def _check_host_args(recipe: Recipe) -> None:
    for step in recipe.steps:
        if step.tool == "object.animate":
            preset = str(step.args["preset"])
            allowed = {"object", "preset", "frame_start", "frame_end"} | ANIMATE_EXTRA[preset]
        else:
            allowed = HOST_ARGS[step.tool]
        assert set(step.args) <= allowed, (step.tool, set(step.args) - allowed)
        if step.tool == "material.apply_preset":
            assert step.args["preset"] in MATERIAL_PRESETS


@pytest.mark.parametrize("template_id", sorted(TEMPLATES))
def test_templates_emit_only_registered_host_tools(template_id: str) -> None:
    registry = runtime_tool_names(TOOLS_PY)
    template = TEMPLATES[template_id]
    for v in template.variants:
        for aspect in ASPECTS:
            for recipe in (
                template.final(northwind(), SLOTS[template_id], variant=v.id, aspect=aspect),
                template.preview(northwind(), SLOTS[template_id], variant=v.id, aspect=aspect),
            ):
                assert set(tools(recipe)) <= registry
                _check_host_args(recipe)
                assert "secret" not in recipe.model_dump_json().lower()


def test_template_code_never_imports_bpy() -> None:
    assert "bpy" not in sys.modules
    for source in TEMPLATE_SOURCES.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "import bpy" not in text and "bpy_runtime" not in text, source


def test_assets_must_be_relative_and_pinned() -> None:
    with pytest.raises(ValueError, match="relative to the assets dir"):
        DeviceSlots.model_validate({**DEVICE_SLOTS, "hdri": {"path": "../x.hdr", "sha256": SHA}})
    with pytest.raises(ValueError, match="relative to the assets dir"):
        DeviceSlots.model_validate({**DEVICE_SLOTS, "hdri": {"path": "/srv/x.hdr", "sha256": SHA}})
    with pytest.raises(ValueError, match="sha256"):
        DeviceSlots.model_validate({**DEVICE_SLOTS, "hdri": {"path": "x.hdr", "sha256": "nope"}})
    with pytest.raises(ValueError, match="exactly one of screen_frames or screen_movie"):
        DeviceSlots.model_validate(
            {**DEVICE_SLOTS, "screen_movie": {"path": "ui/run.mp4", "sha256": SHA}}
        )
    with pytest.raises(ValueError, match="relative to the assets dir"):
        HERO_OBJECT.build(alterlab(), {"mesh": "../../etc/x.glb"})


def test_referenced_assets_pins_every_template_asset() -> None:
    recipe = LAPTOP.build(
        alterlab(),
        {
            **DEVICE_SLOTS,
            "screen_frames": None,
            "screen_movie": {"path": "ui/run.mp4", "sha256": SHA},
        },
    )
    assert referenced_assets(recipe) == {"ui/run.mp4": SHA, "hdri/studio.hdr": SHA_B}
    frames = LAPTOP.build(alterlab(), DEVICE_SLOTS)
    assert referenced_assets(frames) == {
        "ui/screen_0001.png": SHA,
        "ui/screen_0002.png": SHA,
        "hdri/studio.hdr": SHA_B,
    }
    typed = TYPOGRAPHY.build(northwind(), SLOTS[TYPOGRAPHY.id])
    assert referenced_assets(typed) == {"fonts/Fraunces.ttf": SHA_B}
    kit = alterlab().model_copy(update={"logo": "alterlab-mark"})
    card = END_CARD.build(
        kit,
        {
            "cta": "Start",
            "logo": {"path": "logos/alterlab-mark.png", "sha256": SHA_B},
            "fonts": FONTS,
        },
    )
    assert referenced_assets(card) == {"logos/alterlab-mark.png": SHA_B, "fonts/Inter.ttf": SHA}


# ---- per-template behaviour ---------------------------------------------------------


def test_device_screen_takes_frames_or_movie_and_hdri_lighting() -> None:
    recipe = PHONE.build(alterlab(), {**DEVICE_SLOTS, "frame_offset": 3})
    (device,) = args_of(recipe, "device.add")
    assert device["kind"] == "phone"
    assert device["screen"]["frame_offset"] == 3 and len(device["screen"]["frames"]) == 2
    assert args_of(recipe, "world.set_hdri") == [
        {"path": "hdri/studio.hdr", "sha256": SHA_B, "strength": 1.0, "rotation": 0.0}
    ]
    assert {a["name"] for a in args_of(recipe, "light.add")} == {"key", "rim"}
    assert args_of(LAPTOP.build(alterlab(), DEVICE_SLOTS), "device.add")[0]["kind"] == "laptop"


@pytest.mark.parametrize("animation", sorted(ANIMATE_EXTRA))
def test_hero_object_mesh_and_animation(animation: str) -> None:
    kit = alterlab().model_copy(update={"hero_object_mesh": "alterlab-cube"})
    recipe = HERO_OBJECT.build(kit, {"animation": animation, "frame_end": 96})
    (gltf,) = args_of(recipe, "object.import_gltf")
    assert gltf["path"] == "meshes/alterlab-cube.glb" and gltf["name"] == "hero"
    (anim,) = args_of(recipe, "object.animate")
    assert anim["preset"] == animation and anim["object"] == "hero"
    assert (anim["frame_start"], anim["frame_end"]) == (1, 96)
    slot = HERO_OBJECT.build(kit, {"mesh": "meshes/other.gltf"})
    assert args_of(slot, "object.import_gltf")[0]["path"] == "meshes/other.gltf"
    plain = HERO_OBJECT.build(alterlab(), {"primitive": "uv_sphere"})
    assert "object.import_gltf" not in tools(plain)
    assert {"kind": "uv_sphere"}.items() <= args_of(plain, "object.add_primitive")[-1].items()


def test_typography_depth_and_cutout() -> None:
    depth = TYPOGRAPHY.build(alterlab(), {"body": "Ship it", "fonts": FONTS})
    (text,) = args_of(depth, "text.add")
    assert text["mode"] == "depth" and "plate" not in text
    cut = TYPOGRAPHY.build(alterlab(), {"body": "Ship it", "mode": "cutout"})
    (text,) = args_of(cut, "text.add")
    assert text["mode"] == "cutout" and text["plate"] == "type_plate"
    assert "font" not in text  # no pinned file: Blender's built-in font
    names = [a["name"] for a in args_of(cut, "object.add_primitive")]
    assert names.index("type_plate") < len(names)
    body = TYPOGRAPHY.build(alterlab(), {"body": "Ship it", "role": "body", "fonts": FONTS})
    assert args_of(body, "text.add")[0]["font"]["path"] == "fonts/Inter.ttf"
    unsafe = {"Inter": font("fonts/Inter.ttf", SHA, safe=False)}
    with pytest.raises(TemplateError, match="not commercial-safe"):
        TYPOGRAPHY.build(alterlab(), {"body": "Ship it", "fonts": unsafe})


def test_end_card_logo_comes_only_from_the_kit() -> None:
    plain = END_CARD.build(alterlab(), {"cta": "Start a run"})
    assert "material.apply_screen" not in tools(plain)
    assert args_of(plain, "text.add")[0]["body"] == "Start a run"
    with pytest.raises(TemplateError, match="has no logo"):
        END_CARD.build(
            alterlab(), {"cta": "Start", "logo": {"path": "logos/other.png", "sha256": SHA}}
        )
    kit = alterlab().model_copy(update={"logo": "alterlab-mark"})
    with pytest.raises(TemplateError, match="needs its pinned file"):
        END_CARD.build(kit, {"cta": "Start"})
    with pytest.raises(TemplateError, match="is not the kit's logo"):
        END_CARD.build(kit, {"cta": "Start", "logo": {"path": "logos/other.png", "sha256": SHA}})
