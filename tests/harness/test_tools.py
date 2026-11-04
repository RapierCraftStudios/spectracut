# SPDX-License-Identifier: AGPL-3.0-or-later
"""Harness tools, recorder, dispatch and taxonomy against the real host agent + fake Blender."""

from __future__ import annotations

import ast
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest
from spectracut_host.guard import runtime_tool_names

from spectracut.harness.dispatch import PreviewDispatcher, _host_env
from spectracut.harness.errors import (
    ERROR_CODES,
    HARNESS_CODES,
    RUNTIME_CODES,
    HarnessError,
    code_from_host_output,
)
from spectracut.harness.imagecmp import decode_png, frames_match, mean_abs_diff
from spectracut.harness.recorder import Recorder, load_recipe
from spectracut.harness.server import (
    CALL_TOOL,
    FIXED_CONTEXT_BUDGET_BYTES,
    FIXED_TOOLS,
    all_tools_bytes,
    call,
    fixed_context_bytes,
    fixed_tools,
)
from spectracut.harness.tools import Output, Session, build_tools, registry, schema_for
from spectracut.harness.tools.light import RIGS
from spectracut.queue.signing import SIGNING_KEY_ENV, load_public_key, parse_signed_job, verify_job

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_NAMES = runtime_tool_names(ROOT / "host" / "bpy_runtime" / "tools.py")
HARNESS_SRC = ROOT / "src" / "spectracut" / "harness"

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
]


def build(session: Session, steps: list[tuple[str, dict[str, Any]]] = PRODUCT_SHOT) -> None:
    for name, args in steps:
        call(session, name, args)


def error_of(session: Session, name: str, args: dict[str, Any]) -> HarnessError:
    with pytest.raises(HarnessError) as info:
        call(session, name, args)
    return info.value


# --- R2 recipe recording ----------------------------------------------------------------


def test_build_calls_become_contiguous_recipe_steps_queries_only_log(session: Session) -> None:
    build(session)
    call(session, "inspect.framing", {"object": "Product"})
    recipe = session.recorder.recipe()
    tools = [s.tool for s in recipe.steps]
    assert tools[:4] == [
        "scene.clear",
        "object.add_primitive",
        "object.add_primitive",
        "material.apply_preset",
    ]
    assert tools[4:7] == ["light.add"] * 3 and tools[-1] == "camera.add"
    assert [s.index for s in recipe.steps] == list(range(len(tools)))
    assert "inspect.framing" not in tools
    assert [c.tool for c in session.recorder.calls] == [n for n, _ in PRODUCT_SHOT] + [
        "inspect.framing"
    ]
    assert session.recipe_path is not None
    assert load_recipe(session.recipe_path) == recipe


# --- R7 replay -----------------------------------------------------------------------------


def test_replaying_saved_recipe_reproduces_frame(
    session: Session, make_dispatcher: Any, tmp_path: Path
) -> None:
    build(session)
    first = call(session, "preview.render", {}).images[0][1]
    assert session.recipe_path is not None
    saved = load_recipe(session.recipe_path)

    replayer: PreviewDispatcher = make_dispatcher(work_dir=tmp_path / "replay")
    replay = replayer.render(saved)
    assert replay.recipe_hash == saved.content_hash
    assert frames_match(first, replay.frame, tolerance=0.01)

    # A different recipe (another colour, no lights) gives a different frame.
    bad = Recorder(steps=[s for s in saved.steps if s.tool != "light.add"])
    bad.steps = [s.model_copy(update={"index": i}) for i, s in enumerate(bad.steps)]
    other = replayer.render(bad.recipe())
    assert not frames_match(first, other.frame, tolerance=0.01)


def _encode(width: int, height: int, channels: int, pixels: bytes, ftype: int) -> bytes:
    stride = width * channels
    raw = bytearray()
    prev = bytes(stride)
    for y in range(height):
        row = pixels[y * stride : (y + 1) * stride]
        out = bytearray()
        for x in range(stride):
            a = row[x - channels] if x >= channels else 0
            b = prev[x]
            c = prev[x - channels] if x >= channels else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            paeth = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
            pred = [0, a, b, (a + b) // 2, paeth][ftype]
            out.append((row[x] - pred) & 0xFF)
        raw += bytes([ftype]) + out
        prev = row
    color = {3: 2, 4: 6}[channels]

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + body))
        return struct.pack(">I", len(body)) + kind + body + crc

    header = struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw)))
        + chunk(b"IEND", b"")
    )


@pytest.mark.parametrize("ftype", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("channels", [3, 4])
def test_png_decoder_handles_every_filter(ftype: int, channels: int) -> None:
    w, h = 5, 4
    pixels = bytes(
        (x * 37 + y * 11 + c * 101) % 256
        for y in range(h)
        for x in range(w)
        for c in range(channels)
    )
    img = decode_png(_encode(w, h, channels, pixels, ftype))
    assert (img.width, img.height, img.channels, img.pixels) == (w, h, channels, pixels)


def test_frame_compare_tolerance_and_size() -> None:
    a = _encode(2, 1, 3, bytes([0, 0, 0, 255, 255, 255]), 0)
    b = _encode(2, 1, 3, bytes([0, 0, 0, 255, 255, 253]), 0)
    assert mean_abs_diff(a, a) == 0.0
    assert frames_match(a, b) and not frames_match(a, b, tolerance=0.0)
    assert not frames_match(a, _encode(1, 2, 3, bytes(6), 0))


# --- R8 error taxonomy ---------------------------------------------------------------------


def test_taxonomy_is_closed_and_stable() -> None:
    assert {
        "framing.subject_too_small",
        "render.timeout",
        "render.failed",
        "host.refused",
    } <= HARNESS_CODES
    assert {"args.invalid", "scene.object_not_found", "scene.no_camera"} <= RUNTIME_CODES
    assert ERROR_CODES == RUNTIME_CODES | HARNESS_CODES
    with pytest.raises(ValueError):
        HarnessError("made.up", "nope")
    assert HarnessError("render.timeout", "slow").to_payload() == {
        "error": {"code": "render.timeout", "message": "slow"}
    }
    tail = "spectracut runner: refused: step 3 (inspect.framing) [scene.object_not_found]: no"
    assert code_from_host_output(tail) == "scene.object_not_found"
    assert code_from_host_output("[not.a_code] boom") == "render.failed"


def test_runtime_codes_pass_through_from_host(session: Session) -> None:
    build(session)
    assert (
        error_of(session, "inspect.framing", {"object": "Ghost"}).code == "scene.object_not_found"
    )


def test_args_and_unknown_tool_codes(session: Session) -> None:
    assert error_of(session, "camera.set_lens", {"mm": -1}).code == "args.invalid"
    assert (
        error_of(session, "light.add", {"rig": "three_point", "energy": 5}).code == "args.invalid"
    )
    assert (
        error_of(session, "object.add_primitive", {"kind": "cube", "x": 1}).code == "args.invalid"
    )
    assert error_of(session, "nope.tool", {}).code == "tool.unknown"
    assert error_of(session, "preview.render", {}).code == "recipe.empty"
    assert session.recorder.steps == []


def test_render_timeout(make_dispatcher: Any) -> None:
    sleeper = (sys.executable, "-c", "import time; time.sleep(30)")
    slow: PreviewDispatcher = make_dispatcher(host_cmd=sleeper, timeout_s=0.5)
    session = Session(Recorder(), slow)
    call(session, "scene.clear", {})
    assert error_of(session, "preview.render", {}).code == "render.timeout"


def test_framing_subject_too_small(session: Session) -> None:
    build(session)
    err = error_of(session, "inspect.framing", {"object": "Product", "min_coverage_pct": 90})
    assert err.code == "framing.subject_too_small"
    assert 0 < err.details["coverage_pct"] < 90


def test_host_refusal_is_host_refused(make_dispatcher: Any) -> None:
    from spectracut.queue.signing import generate_keypair

    stranger: PreviewDispatcher = make_dispatcher(key=generate_keypair()[0])
    session = Session(Recorder(), stranger)
    call(session, "scene.clear", {})
    assert error_of(session, "preview.render", {}).code == "host.refused"


# --- R9 framing coverage -------------------------------------------------------------------


def test_inspect_framing_coverage_is_percent_of_frame(session: Session) -> None:
    build(session)
    report = call(session, "inspect.framing", {"object": "Product", "min_coverage_pct": 1}).data
    assert report["unit"] == "percent_of_frame"
    x0, y0, x1, y1 = report["bbox_ndc"]
    assert report["coverage_pct"] == pytest.approx((x1 - x0) * (y1 - y0) * 100)
    assert 1 < report["coverage_pct"] < 100 and report["meets_min"] is True
    assert report["in_frame_pct"] == pytest.approx(100.0)


# --- R10 fixed context budget via discover -------------------------------------------------


def test_fixed_context_under_budget_and_discover_gives_schemas(session: Session) -> None:
    assert [t.name for t in fixed_tools()] == [FIXED_TOOLS[0], CALL_TOOL, *FIXED_TOOLS[1:]]
    assert fixed_context_bytes() < FIXED_CONTEXT_BUDGET_BYTES
    assert fixed_context_bytes() * 2 < all_tools_bytes()
    listed = call(session, "discover", {}).data["tools"]
    assert [t["name"] for t in listed] == sorted(registry())
    cams = call(session, "discover", {"prefix": "camera."}).data["tools"]
    assert cams and all(t["name"].startswith("camera.") for t in cams)
    docs = call(session, "discover", {"prefix": "docs."}).data["tools"]
    assert [t["name"] for t in docs] == ["docs.search_api", "docs.search_manual"]
    schemas = call(session, "discover", {"names": ["camera.move", "light.add"]}).data["schemas"]
    assert schemas["camera.move"] == schema_for("camera.move")
    assert "keys" in schemas["camera.move"]["properties"]
    assert session.recorder.steps == []


# --- R11 look ------------------------------------------------------------------------------


def test_look_returns_three_auto_framed_views(session: Session) -> None:
    build(session)
    steps_before = list(session.recorder.steps)
    out: Output = call(session, "look", {"angle": "three_quarter"})
    assert [label for label, _ in out.images] == ["solid", "material", "rendered"]
    assert all(decode_png(png).width > 0 for _, png in out.images)
    views = out.data["views"]
    assert [v["shading"] for v in views] == ["solid", "material", "rendered"]
    assert [v["engine"] for v in views][:2] == ["BLENDER_WORKBENCH"] * 2
    for v in views:
        assert v["angle"] == "three_quarter"
        loc = v["camera"]["location"]
        assert loc[0] > 0 and loc[1] < 0 and loc[2] > 1
    solid, material, rendered = (png for _, png in out.images)
    assert not frames_match(solid, material, tolerance=0.001)
    assert session.recorder.steps == steps_before


# --- R12 runtime allowlist -----------------------------------------------------------------


def test_every_tool_step_is_a_runtime_tool() -> None:
    for tool in registry().values():
        assert tool.steps <= RUNTIME_NAMES, tool.name
    for tool in build_tools():
        assert tool.name in RUNTIME_NAMES
        assert tool.steps == frozenset({tool.name})


def test_light_rig_expands_to_several_light_adds(session: Session) -> None:
    out = call(session, "light.add", {"rig": "product_softbox", "target": [0, 0, 1], "power": 2})
    steps = session.recorder.steps
    assert len(out.data["recorded"]) == len(RIGS["product_softbox"]) == len(steps) == 3
    assert {s.tool for s in steps} == {"light.add"}
    assert [s.args["energy"] for s in steps] == [1000.0, 1000.0, 700.0]
    assert [s.args["name"] for s in steps] == ["RigLeft", "RigRight", "RigTop"]


def test_session_recipe_and_signed_job_only_hold_runtime_steps(
    session: Session, keypair: tuple[str, str]
) -> None:
    build(session)
    call(session, "look", {"modes": ["solid"]})
    assert {s.tool for s in session.recorder.steps} <= RUNTIME_NAMES
    # R16: what reaches the host is a signed, declarative recipe of runtime tools only.
    for job in (session.dispatcher.config.work_dir / "jobs").glob("*/job.json"):
        signed = parse_signed_job(job.read_bytes())
        envelope = verify_job(signed, load_public_key(keypair[1]))
        assert {s.tool for s in envelope.recipe.steps} <= RUNTIME_NAMES


# --- R13 / R15 no bpy in the control plane -------------------------------------------------


def test_harness_never_imports_bpy_or_host() -> None:
    modules = sorted(
        "spectracut."
        + ".".join(p.relative_to(HARNESS_SRC.parent).with_suffix("").parts).replace(".__init__", "")
        for p in HARNESS_SRC.rglob("*.py")
    )
    code = (
        "import importlib, json, sys\n"
        f"for m in {modules!r}: importlib.import_module(m)\n"
        "print(json.dumps(sorted(m for m in sys.modules "
        "if m.split('.')[0] in ('bpy', 'bpy_runtime', 'spectracut_host'))))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == []
    for path in HARNESS_SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                assert name.split(".")[0] not in {"bpy", "bpy_runtime", "spectracut_host"}, path


# --- R17 secrets ---------------------------------------------------------------------------


def test_signing_key_never_reaches_recipes_logs_or_host(
    session: Session, keypair: tuple[str, str]
) -> None:
    build(session)
    call(session, "preview.render", {})
    secret = keypair[0]
    assert session.recipe_path is not None
    assert secret not in session.recipe_path.read_text()
    assert secret not in repr(session.recorder.calls)
    for path in session.dispatcher.config.work_dir.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".png"}:
            assert secret.encode() not in path.read_bytes(), path
    assert SIGNING_KEY_ENV not in _host_env({SIGNING_KEY_ENV: secret, "PATH": "/bin"})
