# SPDX-License-Identifier: AGPL-3.0-or-later
"""``render.set_property`` (a fixed allowlist of render and colour settings) and ``render.set_output``.

Each allowed key maps to a typed validator and a setter written out in code;
the user-supplied key only selects a row in the table. Nothing here evaluates
or looks up attributes by user strings.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bpy_runtime.ops import Report, ToolError, as_bool, as_int, as_num, check_only

Setter = Callable[[Any, Any], None]
Validator = Callable[[Any, str], Any]


def _int_in(lo: int, hi: int) -> Validator:
    return lambda v, what: as_int(v, what, lo, hi)


def _float_in(lo: float, hi: float) -> Validator:
    return lambda v, what: as_num(v, what, lo, hi)


def _one_of(*choices: str) -> Validator:
    def check(v: Any, what: str) -> str:
        if not isinstance(v, str) or v not in choices:
            raise ToolError(f"{what} must be one of {sorted(choices)}")
        return v

    return check


def _set_film_transparent(s: Any, v: Any) -> None:
    s.render.film_transparent = v


def _set_fps(s: Any, v: Any) -> None:
    s.render.fps = v


def _set_motion_blur(s: Any, v: Any) -> None:
    s.render.use_motion_blur = v


def _set_motion_blur_shutter(s: Any, v: Any) -> None:
    s.render.motion_blur_shutter = v


def _set_cycles_samples(s: Any, v: Any) -> None:
    s.cycles.samples = v


def _set_cycles_seed(s: Any, v: Any) -> None:
    s.cycles.seed = v


def _set_cycles_use_denoising(s: Any, v: Any) -> None:
    s.cycles.use_denoising = v


def _set_cycles_denoiser(s: Any, v: Any) -> None:
    s.cycles.denoiser = v


def _set_eevee_samples(s: Any, v: Any) -> None:
    s.eevee.taa_render_samples = v


def _set_view_transform(s: Any, v: Any) -> None:
    s.view_settings.view_transform = v


def _set_look(s: Any, v: Any) -> None:
    s.view_settings.look = v


def _set_display_device(s: Any, v: Any) -> None:
    s.display_settings.display_device = v


def _set_exposure(s: Any, v: Any) -> None:
    s.view_settings.exposure = v


def _set_gamma(s: Any, v: Any) -> None:
    s.view_settings.gamma = v


PROPERTIES: dict[str, tuple[Validator, Setter]] = {
    "render.film_transparent": (as_bool, _set_film_transparent),
    "render.fps": (_int_in(1, 240), _set_fps),
    "render.use_motion_blur": (as_bool, _set_motion_blur),
    "render.motion_blur_shutter": (_float_in(0.0, 2.0), _set_motion_blur_shutter),
    "cycles.samples": (_int_in(1, 65536), _set_cycles_samples),
    "cycles.seed": (_int_in(0, 2**31 - 1), _set_cycles_seed),
    "cycles.use_denoising": (as_bool, _set_cycles_use_denoising),
    "cycles.denoiser": (_one_of("OPENIMAGEDENOISE", "OPTIX"), _set_cycles_denoiser),
    "eevee.taa_render_samples": (_int_in(1, 65536), _set_eevee_samples),
    "view_settings.view_transform": (
        _one_of("AgX", "Standard", "Filmic", "Khronos PBR Neutral", "ACES 2.0"),
        _set_view_transform,
    ),
    "view_settings.look": (
        _one_of(
            "None",
            "AgX - Punchy",
            "AgX - Greyscale",
            "AgX - Very High Contrast",
            "AgX - High Contrast",
            "AgX - Medium High Contrast",
            "AgX - Base Contrast",
            "AgX - Medium Low Contrast",
            "AgX - Low Contrast",
            "AgX - Very Low Contrast",
        ),
        _set_look,
    ),
    "view_settings.display_device": (_one_of("sRGB", "Rec.1886"), _set_display_device),
    "view_settings.exposure": (_float_in(-10.0, 10.0), _set_exposure),
    "view_settings.gamma": (_float_in(0.0, 5.0), _set_gamma),
}


def render_set_property(bpy: Any, args: dict[str, Any]) -> Report:
    check_only(args, {"property", "value"}, "render.set_property")
    key = args.get("property")
    row = PROPERTIES.get(key) if isinstance(key, str) else None
    if row is None:
        raise ToolError(f"property must be one of {sorted(PROPERTIES)}")
    if "value" not in args:
        raise ToolError("render.set_property: value is required")
    validate, setter = row
    value = validate(args["value"], "value")
    setter(bpy.context.scene, value)
    return {"property": key, "value": value}


# Output formats: an 8-bit PNG master, or scene-linear OpenEXR (no view transform baked).
OUTPUT_DEPTHS = {"PNG": ("8", "16"), "OPEN_EXR": ("16", "32")}
EXR_CODEC = "ZIP"


def render_set_output(bpy: Any, args: dict[str, Any]) -> Report:
    check_only(args, {"format", "color_depth"}, "render.set_output")
    fmt = _one_of(*OUTPUT_DEPTHS)(args.get("format"), "format")
    depth = _one_of(*OUTPUT_DEPTHS[fmt])(
        args.get("color_depth", OUTPUT_DEPTHS[fmt][0]), "color_depth"
    )
    image = bpy.context.scene.render.image_settings
    image.file_format = fmt
    image.color_depth = depth
    # FOLLOW_SCENE: PNG is encoded with the scene's view/display; EXR stays scene-linear.
    image.color_management = "FOLLOW_SCENE"
    report: dict[str, Any] = {"format": fmt, "color_depth": depth}
    if fmt == "OPEN_EXR":
        image.exr_codec = EXR_CODEC
        report["exr_codec"] = EXR_CODEC
    return report
