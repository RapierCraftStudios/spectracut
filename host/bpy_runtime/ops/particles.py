# SPDX-License-Identifier: AGPL-3.0-or-later
"""Particle op: ``particles.add`` puts a seeded dust/emitter system on a mesh.

The system is a Particle System modifier on ``emitter``; its emission is a
function of the settings and ``seed`` only (the op draws no random numbers
itself), so a recipe yields the same particles on every host. Particles render
as copies of ``instance`` when given, else as halos. Every argument is checked
before the scene is changed.
"""

from __future__ import annotations

from typing import Any

from bpy_runtime.ops import (
    Report,
    ToolError,
    as_int,
    as_name,
    as_num,
    check_only,
    find_object,
)
from bpy_runtime.ops.camera import MAX_FRAME

TOOL = "particles.add"
SEED_MAX = 2**31 - 1
_ALLOWED = {
    "emitter",
    "name",
    "count",
    "seed",
    "lifetime",
    "size",
    "velocity",
    "frame_start",
    "frame_end",
    "instance",
}


def _parse(args: dict[str, Any]) -> dict[str, Any]:
    check_only(args, _ALLOWED, TOOL)
    p: dict[str, Any] = {
        "name": as_name(args.get("name", "Dust"), "name"),
        "count": as_int(args.get("count", 1000), "count", 1, 100_000),
        "seed": as_int(args.get("seed", 0), "seed", 0, SEED_MAX),
        "lifetime": as_num(args.get("lifetime", 50), "lifetime", 1, 10_000),
        "size": as_num(args.get("size", 0.01), "size", 0, 100),
        "velocity": as_num(args.get("velocity", 0), "velocity", 0, 100),
        "frame_start": as_int(args.get("frame_start", 1), "frame_start", 0, MAX_FRAME),
        "frame_end": as_int(args.get("frame_end", 1), "frame_end", 0, MAX_FRAME),
    }
    if p["size"] <= 0:
        raise ToolError("size must be within (0, 100]")
    if p["frame_end"] < p["frame_start"]:
        raise ToolError(f"{TOOL}: frame_end must not be before frame_start")
    return p


def particles_add(bpy: Any, args: dict[str, Any]) -> Report:
    p = _parse(args)
    emitter = find_object(bpy, args.get("emitter"), TOOL, "emitter")
    if emitter.type != "MESH":
        raise ToolError(f"{TOOL}: emitter {emitter.name!r} is not a mesh object")
    instance = None
    if "instance" in args:
        instance = find_object(bpy, args["instance"], TOOL, "instance")
        if instance is emitter:
            raise ToolError(f"{TOOL}: an emitter cannot instance itself")
    if any(m.name == p["name"] for m in emitter.modifiers):
        raise ToolError(f"{TOOL}: {emitter.name!r} already has a modifier named {p['name']!r}")
    mod = emitter.modifiers.new(p["name"], type="PARTICLE_SYSTEM")
    psys = mod.particle_system
    psys.seed = p["seed"]
    settings = psys.settings
    settings.name = p["name"]
    settings.type = "EMITTER"
    settings.count = p["count"]
    # Start before end: Blender pushes frame_end up to frame_start, never down.
    settings.frame_start = p["frame_start"]
    settings.frame_end = p["frame_end"]
    settings.lifetime = p["lifetime"]
    settings.particle_size = p["size"]
    settings.normal_factor = p["velocity"]
    if instance is not None:
        settings.render_type = "OBJECT"
        settings.instance_object = instance
    else:
        settings.render_type = "HALO"
    return {
        "emitter": str(emitter.name),
        "system": p["name"],
        "seed": p["seed"],
        "count": p["count"],
        "lifetime": p["lifetime"],
        "size": p["size"],
        "velocity": p["velocity"],
        "frame_start": p["frame_start"],
        "frame_end": p["frame_end"],
        "instance": str(instance.name) if instance is not None else None,
    }
