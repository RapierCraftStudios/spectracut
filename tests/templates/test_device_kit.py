# SPDX-License-Identifier: AGPL-3.0-or-later
"""#362: the glTF device kit's manifest, screen measurement and pinned fetch.

The real glbs are not committed: the manifest pins their commit URLs and sha256,
measurement runs on a synthetic glb, and the real-asset hash/measurement check runs
only when the asset is already in the shared assets cache.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import struct
import subprocess
from pathlib import Path
from typing import Any

import pytest

from spectracut.templates import device_kit as dk
from spectracut.templates.device_kit import (
    IPHONE_15_PRO,
    MACBOOK_PRO,
    DeviceAssetError,
    ensure_device_asset,
    measure,
    measure_screen,
)

ROOT = Path(__file__).resolve().parents[2]
REAL = (MACBOOK_PRO, IPHONE_15_PRO)


def glb(doc: dict[str, Any]) -> bytes:
    """A binary glTF holding only ``doc`` as its JSON chunk."""
    body = json.dumps(doc).encode()
    body += b" " * (-len(body) % 4)
    header = struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(body))
    return header + struct.pack("<I4s", len(body), b"JSON") + body


def _accessor(lo: list[float], hi: list[float]) -> dict[str, Any]:
    return {"componentType": 5126, "count": 8, "type": "VEC3", "min": lo, "max": hi}


# A MacBook-like glb (glTF Y-up): the body and a thin display facing glTF +Z (Blender
# -Y), both on child "Top", which carries a 5.8x scale like the source asset.
LAPTOP_DOC: dict[str, Any] = {
    "asset": {"version": "2.0"},
    "scene": 0,
    "scenes": [{"nodes": [0]}],
    "nodes": [
        {"name": "Macbook", "children": [1]},
        {"name": "Top", "mesh": 0, "scale": [5.8, 5.8, 5.8]},
    ],
    "meshes": [
        {
            "primitives": [
                {"attributes": {"POSITION": 0}, "material": 0},
                {"attributes": {"POSITION": 1}, "material": 1},
            ]
        }
    ],
    "accessors": [
        _accessor([-0.5, -0.1, -0.3], [0.5, 0.1, 0.3]),
        _accessor([-0.4, -0.05, 0.3], [0.4, 0.2, 0.3001]),
    ],
    "materials": [{"name": "Frame.001"}, {"name": "DisplayGlass"}],
}


# ---- manifest -------------------------------------------------------------------------------


def test_manifest_pins_two_commit_urls_by_sha256() -> None:
    assert set(dk.KIND_MODEL.values()) == {"macbook-pro", "iphone-15-pro"}
    assert MACBOOK_PRO.url == (
        "https://raw.githubusercontent.com/pmndrs/examples/"
        "541599ac5ad913de85796403c1c6927ada3b303c/examples/"
        "multiple-views-with-uniform-controls/public/mac.glb"
    )
    assert MACBOOK_PRO.sha256 == "9939bf7a89754ca3d8fab1b9cf5473a99d75f5e266fc4666833c7c0fa8aaad9d"
    assert IPHONE_15_PRO.url == (
        "https://raw.githubusercontent.com/adrianhajdin/iphone/"
        "16ec9fda2f4f658fd662c5818c5a10b1a9545e1b/public/models/scene.glb"
    )
    assert IPHONE_15_PRO.sha256 == (
        "a628339ba9960424b1e6285d31423be0dedf02a71b02e4e61f706a2e4bb8770e"
    )
    for entry in REAL:
        assert entry.path == f"devices/{entry.sha256}.glb"
        assert entry.pinned().sha256 == entry.sha256
        assert entry.license.commercial_safe


def test_manifest_dressing_matches_the_issue() -> None:
    assert MACBOOK_PRO.real_width == 0.3126 and MACBOOK_PRO.needs_uv
    assert MACBOOK_PRO.screen.arg() == {"node": "Top", "material": "DisplayGlass"}
    assert MACBOOK_PRO.body_material == "Frame.001"
    assert set(MACBOOK_PRO.finishes) == {"space-black", "silver"}
    assert MACBOOK_PRO.license.licence == "MIT"
    assert IPHONE_15_PRO.real_width == 0.0767
    assert IPHONE_15_PRO.screen.arg() == {"node": "xXDHkMplTIDAXLN", "material": "pIJKfZsazmcpEiU"}
    assert IPHONE_15_PRO.license.licence == "CC-BY-4.0"
    assert "polyman" in IPHONE_15_PRO.license.attribution
    for entry in REAL:
        lo, hi = entry.bounds
        assert lo[2] == 0.0  # lowest point on the floor
        assert hi[0] - lo[0] == pytest.approx(entry.real_width, abs=1e-4)
        assert entry.frame.normal[1] < 0  # facing the camera side (-Y)


def test_kit_binaries_are_not_committed() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "*.glb"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    names = {Path(t).name for t in tracked}
    assert not names & {"mac.glb", "scene.glb"}
    assert not any(e.sha256 in t for e in REAL for t in tracked)


# ---- measure_screen ------------------------------------------------------------------------


def test_measure_screen_normalises_and_frames_the_display() -> None:
    entry = dataclasses.replace(MACBOOK_PRO)
    frame, (lo, hi) = measure(glb(LAPTOP_DOC), entry)
    # Width scaled to the real 312.6 mm despite the 5.8x child scale; floor at z = 0.
    assert hi[0] - lo[0] == pytest.approx(0.3126, abs=1e-5)
    assert lo[2] == 0.0
    assert frame.normal == (0.0, -1.0, 0.0) and frame.up == (0.0, 0.0, 1.0)
    assert frame.width == pytest.approx(0.8 * 0.3126, abs=1e-5)
    assert frame.height == pytest.approx(0.25 * 0.3126, abs=1e-5)
    assert frame.centre[0] == pytest.approx(0.0, abs=1e-6)
    assert frame.centre[1] == pytest.approx(-0.30005 * 0.3126, abs=1e-5)  # glTF +Z -> -Y
    assert frame.centre[2] == pytest.approx(0.175 * 0.3126, abs=1e-5)
    assert frame.right == (1.0, 0.0, 0.0)  # unmirrored: viewer's right is +X
    assert measure_screen(glb(LAPTOP_DOC), entry) == frame


def test_measure_screen_refuses_a_missing_screen_or_a_non_glb() -> None:
    doc = {**LAPTOP_DOC, "materials": [{"name": "Frame.001"}, {"name": "Other"}]}
    with pytest.raises(DeviceAssetError, match="DisplayGlass"):
        measure_screen(glb(doc), MACBOOK_PRO)
    with pytest.raises(DeviceAssetError, match="binary glTF"):
        measure_screen(b"not a glb at all, really", MACBOOK_PRO)


def test_kit_camera_frames_the_whole_device_at_70mm() -> None:
    for entry in REAL:
        for aspect in (16 / 9, 9 / 16):
            cam = dk.kit_camera(entry, yaw=35, elevation=12, aspect=aspect)
            assert cam.lens == 70.0
            lo, hi = entry.bounds
            corners = [
                (x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])
            ]
            assert dk.in_frame(cam.location, entry.frame.centre, 70.0, aspect, corners)
    wide = dk.kit_camera(MACBOOK_PRO, yaw=35, elevation=12, aspect=16 / 9)
    # Starts at about 1.2 m and backs off only as far as keeping the device whole needs.
    assert dk.KIT_DISTANCE == 1.2
    assert dk.KIT_DISTANCE <= wide.distance <= 1.35 * dk.KIT_DISTANCE


def _bounds_centre(entry: dk.DeviceAsset) -> tuple[float, float, float]:
    lo, hi = entry.bounds
    return ((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2)


def _forward(rotation: tuple[float, float, float]) -> tuple[float, float, float]:
    """The camera's view axis (-Z) for XYZ Euler ``rotation`` (Blender)."""
    import math

    rx, ry, rz = rotation
    x = -(math.cos(rx) * math.sin(ry) * math.cos(rz) + math.sin(rx) * math.sin(rz))
    y = -(math.cos(rx) * math.sin(ry) * math.sin(rz) - math.sin(rx) * math.cos(rz))
    z = -(math.cos(rx) * math.cos(ry))
    return (x, y, z)


def _aim_point(cam: dk.KitCamera, centre: tuple[float, float, float]) -> float:
    """Distance from ``centre`` to the camera's view ray."""
    import math

    f = _forward(cam.rotation)
    v = [c - p for c, p in zip(centre, cam.location, strict=True)]
    t = sum(a * b for a, b in zip(v, f, strict=True))
    return math.dist(v, [t * a for a in f])


def test_wide_aims_at_bounds_centre() -> None:
    for entry in REAL:
        cam = dk.kit_camera(entry, yaw=0, elevation=8, aspect=16 / 9, target="bounds")
        centre = _bounds_centre(entry)
        assert _aim_point(cam, centre) < 1e-3
        assert _aim_point(cam, entry.frame.centre) > 1e-4 or centre == entry.frame.centre
        lo, hi = entry.bounds
        corners = [
            (x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])
        ]
        assert dk.in_frame(cam.location, centre, 70.0, 16 / 9, corners)


def test_close_is_0_62_of_fitted_distance_at_screen_centre() -> None:
    import math

    for entry in REAL:
        fitted = dk.kit_camera(entry, yaw=0, elevation=8, aspect=16 / 9)
        close = dk.kit_camera(entry, yaw=0, elevation=8, aspect=16 / 9, scale=0.62)
        assert close.distance == pytest.approx(0.62 * fitted.distance, abs=1e-3)
        assert math.dist(close.location, entry.frame.centre) == pytest.approx(
            close.distance, abs=1e-3
        )
        assert _aim_point(close, entry.frame.centre) < 1e-3


def test_default_kit_camera_unchanged() -> None:
    for entry in REAL:
        for yaw, el in ((0, 8), (35, 12)):
            a = dk.kit_camera(entry, yaw=yaw, elevation=el, aspect=16 / 9)
            b = dk.kit_camera(
                entry, yaw=yaw, elevation=el, aspect=16 / 9, target="screen", scale=1.0
            )
            assert a == b
    # Values recorded before the target and scale options existed.
    front = dk.kit_camera(MACBOOK_PRO, yaw=0, elevation=8, aspect=16 / 9)
    assert (front.location, front.rotation, front.distance) == (
        (0.0, -1.1454, 0.2934),
        (1.43117, 0.0, 0.0),
        1.2979,
    )
    quarter = dk.kit_camera(MACBOOK_PRO, yaw=35, elevation=12, aspect=16 / 9)
    assert (quarter.location, quarter.rotation, quarter.distance) == (
        (0.8519, -1.0767, 0.4284),
        (1.361357, 0.0, 0.610865),
        1.5184,
    )


# ---- ensure_device_asset -------------------------------------------------------------------


def _entry(data: bytes) -> dk.DeviceAsset:
    return dataclasses.replace(
        MACBOOK_PRO, url="https://example.invalid/mac.glb", sha256=hashlib.sha256(data).hexdigest()
    )


def test_ensure_device_asset_fetches_verifies_and_caches(tmp_path: Path) -> None:
    data = glb(LAPTOP_DOC)
    entry = _entry(data)
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return data

    path = ensure_device_asset(entry, tmp_path, fetch=fetch)
    assert path == tmp_path / "devices" / f"{entry.sha256}.glb"
    assert path.read_bytes() == data and fetched == [entry.url]
    # A verified cached copy is reused without fetching again.
    assert ensure_device_asset(entry, tmp_path, fetch=fetch) == path and len(fetched) == 1
    # A corrupt cached copy is replaced.
    path.write_bytes(b"corrupt")
    assert ensure_device_asset(entry, tmp_path, fetch=fetch).read_bytes() == data


def test_ensure_device_asset_refuses_a_hash_mismatch(tmp_path: Path) -> None:
    entry = _entry(b"the pinned bytes")
    with pytest.raises(DeviceAssetError, match="manifest pins"):
        ensure_device_asset(entry, tmp_path, fetch=lambda url: b"other bytes")
    assert not (tmp_path / entry.path).exists()

    def offline(url: str) -> bytes:
        raise OSError("offline")

    with pytest.raises(DeviceAssetError, match="cannot fetch"):
        ensure_device_asset(entry, tmp_path, fetch=offline)


@pytest.mark.parametrize("entry", REAL, ids=lambda e: e.id)
def test_cached_real_asset_matches_its_pin_and_recorded_frame(entry: dk.DeviceAsset) -> None:
    path = dk.default_cache_dir() / entry.path
    if not path.is_file():
        pytest.skip(f"{entry.id} is not in the shared assets cache")
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == entry.sha256
    frame, bounds = measure(data, entry)
    assert frame == entry.frame and bounds == entry.bounds
