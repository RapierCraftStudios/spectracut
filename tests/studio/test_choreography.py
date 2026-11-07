# SPDX-License-Identifier: AGPL-3.0-or-later
"""Device shot choreography (#369): ShotPath, anchors, keys, screen timeline, twitch."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from spectracut.providers.mock_image import solid_png
from spectracut.qa.common import encode_png
from spectracut.studio.departments import choreography as ch
from spectracut.studio.departments.capture import ScreenCapture
from spectracut.studio.departments.screens import CHROME_SIZES, CONTENT_RECTS
from spectracut.templates.device_kit import kit_for, look_rotation

LAPTOP = kit_for("laptop")
ANCHORS = {
    "SC": (0.5, 0.5),
    "#url": (0.197, 0.46),
    "text=Run": (0.135, 0.525),
    ".result-card": (0.66, 0.2),
    "text=Status": (0.45, 0.205),
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _capture(
    tmp_path: Path,
    colours: list[int] | None = None,
    *,
    anchors: dict[str, tuple[float, float]] | None = None,
    screen: str | None = None,
    fps: int = 24,
) -> ScreenCapture:
    root = tmp_path / "clip"
    root.mkdir(parents=True, exist_ok=True)
    clip = []
    for n, c in enumerate(colours or [0, 1, 2]):
        data = solid_png(4, 4, (c, 20, 30))
        path = root / f"f{n:04d}.png"
        path.write_bytes(data)
        clip.append({"path": str(path), "sha256": _sha(data)})
    raw: dict[str, Any] = {
        "key": "beat:run",
        "route": "/",
        "url": "http://127.0.0.1:9/",
        "device": "laptop",
        "fps": fps,
        "clip": clip,
        "still": clip[-1],
        "manifest": str(root / "manifest.json"),
        "events": str(root / "events.json"),
        "text": str(root / "text.json"),
        "provenance": {
            "product_id": "alterlab",
            "url": "http://127.0.0.1:9/",
            "steps": [],
            "device": "laptop",
            "spec_sha256": _sha(b"spec"),
            "frames_sha256": _sha(b"frames"),
            "rig": "@spectracut/capture@0",
            "demo_check": "passed",
            "deterministic": True,
        },
        "anchors": anchors if anchors is not None else ANCHORS,
    }
    if screen is not None:
        size = CHROME_SIZES[screen]
        raw["screen"] = {
            "state": "beat:run",
            "device": "laptop",
            "chrome": screen,
            "size": size.model_dump(),
            "content_rect": CONTENT_RECTS[screen].model_dump(),
            "clip": clip,
            "raw_still_sha256": clip[-1]["sha256"],
        }
    return ScreenCapture.model_validate(raw)


def _path(**over: Any) -> dict[str, Any]:
    raw = ch.UI_RUN_3CUT.model_dump(mode="json")
    raw.update(over)
    return raw


# ---- model -------------------------------------------------------------------------------


def test_ui_run_3cut_round_trips_and_is_registered() -> None:
    dumped = ch.UI_RUN_3CUT.model_dump(mode="json")
    again = ch.ShotPath.model_validate(json.loads(json.dumps(dumped)))
    assert again == ch.UI_RUN_3CUT and again.model_dump(mode="json") == dumped
    assert ch.SHOT_PATHS == {"ui-run-3cut": ch.UI_RUN_3CUT}
    p = ch.UI_RUN_3CUT
    assert (p.frames, p.fps) == (132, 24)
    assert [(c.name, c.start, c.end) for c in p.cuts] == [
        ("A", 1, 40),
        ("B", 41, 72),
        ("C", 73, 132),
    ]
    assert p.timing() == [
        {"name": "A", "start": 1, "end": 40},
        {"name": "B", "start": 41, "end": 72},
        {"name": "C", "start": 73, "end": 132},
    ]


def test_ui_run_3cut_keys_follow_the_approved_shot() -> None:
    a, b, c = ch.UI_RUN_3CUT.cuts
    # Cut A: macro rise from the deck edge at f/2.8, 50 -> 60 mm, focus pull onto the URL.
    assert (a.keys[0].fstop, a.keys[0].lens, a.keys[-1].lens) == (2.8, 50, 60)
    assert a.keys[0].loc[0] == "SC" and a.keys[0].loc[1][2] < 0
    assert a.keys[-1].aim[0] == "URL" and a.keys[-1].focus is None
    # Cut B: 85 mm push on Run with -4 deg roll.
    assert {k.lens for k in b.keys} == {85} and b.keys[0].roll == -4
    assert all(k.aim[0] == "RUN" for k in b.keys)
    # Cut C: orbit from the side to the front three-quarter onto the cards, focus on Status.
    assert c.keys[0].loc[1][1] > c.keys[-1].loc[1][1]
    assert all(k.aim[0] == "CARDS" and k.focus is not None for k in c.keys)
    assert all(k.focus is not None and k.focus[0] == "STATUS" for k in c.keys)
    assert c.keys[-1].fstop == 3.2


def test_template_carries_no_anchor_coordinates() -> None:
    raw = json.dumps(ch.UI_RUN_3CUT.model_dump(mode="json"))
    assert "anchors" not in raw and "screen_wh" not in raw
    for patterns in ch.UI_RUN_3CUT.roles.values():
        assert all(isinstance(p, str) for p in patterns)
    assert set(ch.UI_RUN_3CUT.roles) == {"URL", "RUN", "CARDS", "STATUS"}


def test_shot_path_validator_rejects_gaps_stray_keys_and_unknown_anchors() -> None:
    gap = _path()
    gap["cuts"][1]["start"] = 42
    gap["cuts"][1]["keys"][0]["f"] = 42
    with pytest.raises(ValidationError, match="starts at 42, expected 41"):
        ch.ShotPath.model_validate(gap)
    stray = _path()
    stray["cuts"][0]["keys"][-1]["f"] = 39
    with pytest.raises(ValidationError, match="keys must start at f1 and end at f40"):
        ch.ShotPath.model_validate(stray)
    unknown = _path()
    unknown["cuts"][2]["keys"][0]["aim"] = ["LOGO", [0, 0, 0]]
    with pytest.raises(ValidationError, match="unknown anchor 'LOGO'"):
        ch.ShotPath.model_validate(unknown)
    short = _path(frames=140)
    with pytest.raises(ValidationError, match="not 1..140"):
        ch.ShotPath.model_validate(short)


# ---- anchors -----------------------------------------------------------------------------


def test_anchors_resolve_from_the_capture_dom_anchors(tmp_path: Path) -> None:
    resolved = ch.resolve_anchors(ch.UI_RUN_3CUT, _capture(tmp_path))
    assert resolved == {
        "URL": ("#url", (0.197, 0.46)),
        "RUN": ("text=Run", (0.135, 0.525)),
        "CARDS": (".result-card", (0.66, 0.2)),
        "STATUS": ("text=Status", (0.45, 0.205)),
    }


def test_missing_or_ambiguous_role_fails_closed(tmp_path: Path) -> None:
    missing = {k: v for k, v in ANCHORS.items() if k != "text=Status"}
    with pytest.raises(ValueError, match=r"role 'STATUS'.*matches no anchor.*#url"):
        ch.resolve_anchors(ch.UI_RUN_3CUT, _capture(tmp_path, anchors=missing))
    ambiguous = {**ANCHORS, ".result-card:nth(2)": (0.7, 0.3)}
    with pytest.raises(ValueError, match=r"role 'CARDS'.*matches 2 anchors"):
        ch.resolve_anchors(ch.UI_RUN_3CUT, _capture(tmp_path, anchors=ambiguous))


def test_anchor_world_uses_the_kit_screen_basis(tmp_path: Path) -> None:
    capture = _capture(tmp_path)
    at = (0.1, -0.2, 0.0)
    got = ch.anchor_world((0.25, 0.75), capture, LAPTOP, at)
    frame = LAPTOP.frame
    n = frame.normal
    h = math.hypot(n[0], n[1])
    fwd = (n[0] / h, n[1] / h, 0.0)
    side = (-fwd[1], fwd[0], 0.0)
    up = (n[1] * side[2] - n[2] * side[1], n[2] * side[0] - n[0] * side[2], 0.0)
    up = (up[0], up[1], n[0] * side[1] - n[1] * side[0])
    length = math.sqrt(sum(c * c for c in up))
    up = tuple(c / length * (1 if up[2] >= 0 else -1) for c in up)  # type: ignore[assignment]
    sc = tuple(c + a for c, a in zip(frame.centre, at, strict=True))
    want = tuple(
        sc[i] + side[i] * (0.25 - 0.5) * frame.width + up[i] * (0.5 - 0.75) * frame.height
        for i in range(3)
    )
    assert got == pytest.approx(want)
    # The screen centre UV is the screen centre.
    assert ch.anchor_world((0.5, 0.5), capture, LAPTOP, at) == pytest.approx(sc)


def test_anchor_uv_maps_through_the_chrome_content_rect(tmp_path: Path) -> None:
    capture = _capture(tmp_path, screen="windowed")
    rect, size = CONTENT_RECTS["windowed"], CHROME_SIZES["windowed"]
    u, v = ch.display_uv((0.0, 1.0), capture)
    assert (u, v) == pytest.approx((rect.x / size.width, (rect.y + rect.height) / size.height))
    plain = _capture(tmp_path / "p", anchors=ANCHORS)
    assert ch.display_uv((0.2, 0.3), plain) == (0.2, 0.3)


# ---- keys --------------------------------------------------------------------------------


def _world(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    capture = _capture(tmp_path)
    basis = ch.screen_basis(LAPTOP, (0.0, 0.0, 0.0))
    return ch.anchors_world(ch.UI_RUN_3CUT, capture, LAPTOP, (0.0, 0.0, 0.0)), basis


def test_path_keys_hard_cut_at_the_cut_boundaries(tmp_path: Path) -> None:
    world, basis = _world(tmp_path)
    keys: list[Any] = ch.path_keys(ch.UI_RUN_3CUT, world, basis)
    frames = [k["frame"] for k in keys]
    assert frames == [1, 40, 41, 72, 73, 132]
    # Consecutive cut keys are one frame apart: the host holds them (CONSTANT).
    assert (frames[2] - frames[1], frames[4] - frames[3]) == (1, 1)
    for k in keys:
        assert set(k) == {"frame", "location", "rotation", "lens", "focus_distance", "fstop"}
    first_b = keys[2]
    run = ch.point_world(("RUN", (0.2, -0.07, 0.02)), world, basis)
    assert first_b["location"] == pytest.approx(list(run), abs=1e-5)
    rx, ry, rz = look_rotation(run, world["RUN"])
    assert first_b["rotation"] == pytest.approx([rx, ry + math.radians(-4), rz], abs=1e-5)
    assert first_b["lens"] == 85 and first_b["fstop"] == 2.8
    assert first_b["focus_distance"] == pytest.approx(math.dist(run, world["RUN"]), abs=1e-5)
    # Cut C focuses on Status, not on the cards it aims at.
    last = keys[-1]
    loc = ch.point_world(("CARDS", (0.34, 0.1, 0.04)), world, basis)
    assert last["focus_distance"] == pytest.approx(math.dist(loc, world["STATUS"]), abs=1e-5)


def test_ui_run_3cut_frames_its_anchors_on_the_laptop(tmp_path: Path) -> None:
    world, basis = _world(tmp_path)
    assert ch.framing_problems(ch.UI_RUN_3CUT, world, basis, 16 / 9) == []
    # A focus point far off axis is a framing problem.
    raw = _path()
    raw["cuts"][2]["keys"][0]["focus"] = ["SC", [0, 3.0, 0]]
    bad = ch.ShotPath.model_validate(raw)
    problems = ch.framing_problems(bad, world, basis, 16 / 9)
    assert problems and problems[0].startswith("cut C f73")


# ---- screen timeline ---------------------------------------------------------------------


def test_cut_b_ends_on_running_not_the_reset_run(tmp_path: Path) -> None:
    # 0..9 idle/typing, 10 pasted URL, 11 still "Run" (clicked at 12), 12..19 "Running...",
    # 20.. the button resets to the idle "Run" frame (sha of frame 11).
    colours = list(range(12)) + [100] * 8 + [11] * 40 + [200] * 20
    capture = _capture(tmp_path, colours)
    events = [
        {"type": "keystroke", "key": "x", "frame": 3},
        {"type": "keystroke", "key": "⌘V", "frame": 10},
        {"type": "click", "frame": 2},
        {"type": "click", "frame": 12},
    ]
    frames = ch.screen_timeline(ch.UI_RUN_3CUT, capture, events)
    assert len(frames) == 132
    clip = capture.clip
    running = clip[12].sha256
    cut_b = frames[40:72]
    # Cut B: the click (after the paste) lands 8 frames in; the cut ends on Running.
    assert cut_b[8].sha256 == running
    assert cut_b[-1].sha256 == running
    assert all(f.sha256 != clip[11].sha256 for f in cut_b[9:])
    # Cut A lands the paste on its frame 30; cut C ends on the clip's last frame.
    assert frames[30].sha256 == clip[10].sha256
    assert frames[-1].sha256 == clip[-1].sha256
    # Without the guard (no click recorded) the click cut cannot be built.
    with pytest.raises(ValueError, match="syncs to the click"):
        ch.screen_timeline(ch.UI_RUN_3CUT, capture, events[:2])


# ---- twitch ------------------------------------------------------------------------------


def _gradient_png(offset: int) -> bytes:
    rows = []
    for _y in range(8):
        rows.append(bytes(x * 4 + offset for x in range(32) for _ in range(3)))
    return encode_png(32, 8, 3, b"".join(rows))


def test_twitch_frames_flags_an_isolated_drop_but_not_a_cut(tmp_path: Path) -> None:
    offsets = [0, 1, 2, 3, 4, 60, 61, 62, 63, 64, 65, 120, 121, 122]
    paths = []
    for n, off in enumerate(offsets, start=1):
        p = tmp_path / f"frame_{n:04d}.png"
        p.write_bytes(_gradient_png(off))
        paths.append(p)
    # Frame 6 jumps mid-cut (a twitch); frame 12 jumps at a cut start (a hard cut).
    assert ch.twitch_frames(paths, [1, 12]) == [6]
    assert ch.twitch_frames(paths, [1, 6, 12]) == []
    # Undecodable frames break the series instead of failing.
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"not a png")
    assert ch.twitch_frames([junk, *paths[:3]], [1]) == []


def test_choreography_imports_no_producer_or_queue() -> None:
    import ast

    tree = ast.parse(Path(ch.__file__).read_text(encoding="utf-8"))
    names = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)] + [
        a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names
    ]
    assert not [
        n for n in names if n.startswith(("spectracut.queue", "spectracut.studio.producer"))
    ]
