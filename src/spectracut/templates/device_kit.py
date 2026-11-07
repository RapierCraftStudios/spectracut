# SPDX-License-Identifier: AGPL-3.0-or-later
"""Device kit: sha256-pinned glTF device models and their measured screens (#362).

``DEVICE_KIT`` is the asset manifest: a MacBook Pro (``macbook-pro``, space-black
and silver finishes) and an iPhone (``iphone-15-pro``). Each entry pins its source
URL at a commit, the file's sha256, its licence, its real width and the node and
material that carry the display. The binaries are not committed: they are fetched
by :func:`ensure_device_asset` into the shared assets dir
(``<SPECTRACUT_HOME>/assets/devices/<sha256>.glb``) and staged to the host from
there, which re-checks the hash before it imports the file.

The host's ``device.add`` normalises an imported model the same way
:func:`measure_screen` does: a uniform scale so the world-bounds width is the real
width, then a drop so the lowest point sits on the floor (z = 0), in Blender's
Z-up frame (glTF ``(x, y, z)`` is Blender ``(x, -z, y)``). The screen frame each
entry records (``frame``) is :func:`measure_screen` of the pinned file, so recipes
can place the camera and lights from it without reading the file.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from spectracut.spec.asset import LicenseRecord
from spectracut.templates.base import PinnedAsset

Vec3 = tuple[float, float, float]
Fetch = Callable[[str], bytes]

DEVICES_DIR = "devices"
SCREEN_EMISSION = 1.7
FRONT: Vec3 = (0.0, -1.0, 0.0)
"""Devices face the camera side of the set (Blender -Y); screen normals point there."""


class DeviceAssetError(ValueError):
    """A device asset could not be fetched, read or verified."""


@dataclass(frozen=True)
class ScreenSelector:
    """The display faces: the faces of ``node`` that use ``material``."""

    node: str
    material: str

    def arg(self) -> dict[str, str]:
        return {"node": self.node, "material": self.material}


@dataclass(frozen=True)
class ScreenFrame:
    """A normalised model's display: centre, outward normal and up (unit vectors),
    width and height in metres, in Blender world space."""

    centre: Vec3
    normal: Vec3
    up: Vec3
    width: float
    height: float

    @property
    def right(self) -> Vec3:
        """The screen's right edge direction as seen from the front (``up x normal``)."""
        return _cross(self.up, self.normal)


@dataclass(frozen=True)
class DeviceAsset:
    """One pinned device model and how to dress it."""

    id: str
    kind: str
    url: str
    sha256: str
    license: LicenseRecord
    real_width: float
    screen: ScreenSelector
    frame: ScreenFrame
    bounds: tuple[Vec3, Vec3]
    """The normalised model's world bounds (min, max) in metres."""
    needs_uv: bool = False
    body_material: str | None = None
    finishes: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @property
    def path(self) -> str:
        """The content-addressed path under the assets dir."""
        return f"{DEVICES_DIR}/{self.sha256}.glb"

    def pinned(self) -> PinnedAsset:
        return PinnedAsset(path=self.path, sha256=self.sha256)


# Principled overrides on the MacBook body material (``Frame.001``). The host's
# ``device.add`` keeps the same values in ``MODEL_FINISHES``.
MACBOOK_FINISHES: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        "space-black": {
            "Base Color": (0.028, 0.029, 0.032, 1.0),
            "Metallic": 1.0,
            "Roughness": 0.34,
        },
        "silver": {"Base Color": (0.80, 0.80, 0.81, 1.0), "Metallic": 1.0, "Roughness": 0.26},
    }
)

MACBOOK_PRO = DeviceAsset(
    id="macbook-pro",
    kind="laptop",
    url=(
        "https://raw.githubusercontent.com/pmndrs/examples/"
        "541599ac5ad913de85796403c1c6927ada3b303c/examples/"
        "multiple-views-with-uniform-controls/public/mac.glb"
    ),
    sha256="9939bf7a89754ca3d8fab1b9cf5473a99d75f5e266fc4666833c7c0fa8aaad9d",
    license=LicenseRecord(
        source="pmndrs/examples (multiple-views-with-uniform-controls)",
        licence="MIT",
        url="https://github.com/pmndrs/examples",
        attribution="pmndrs/examples contributors",
        commercial_safe=True,
    ),
    real_width=0.3126,
    screen=ScreenSelector(node="Top", material="DisplayGlass"),
    frame=ScreenFrame(
        centre=(1e-05, 0.13993, 0.11274),
        normal=(0.0, -0.96631, 0.25736),
        up=(0.0, 0.25736, 0.96631),
        width=0.29844,
        height=0.18501,
    ),
    bounds=((-0.15626, -0.10994, 0.0), (0.15634, 0.16992, 0.21678)),
    needs_uv=True,
    body_material="Frame.001",
    finishes=MACBOOK_FINISHES,
)

IPHONE_15_PRO = DeviceAsset(
    id="iphone-15-pro",
    kind="phone",
    url=(
        "https://raw.githubusercontent.com/adrianhajdin/iphone/"
        "16ec9fda2f4f658fd662c5818c5a10b1a9545e1b/public/models/scene.glb"
    ),
    sha256="a628339ba9960424b1e6285d31423be0dedf02a71b02e4e61f706a2e4bb8770e",
    license=LicenseRecord(
        source="Sketchfab via adrianhajdin/iphone",
        licence="CC-BY-4.0",
        url="https://github.com/adrianhajdin/iphone",
        attribution="Apple iPhone 15 Pro Max Black by polyman (Sketchfab)",
        commercial_safe=True,
    ),
    real_width=0.0767,
    screen=ScreenSelector(node="xXDHkMplTIDAXLN", material="pIJKfZsazmcpEiU"),
    frame=ScreenFrame(
        centre=(0.0, 0.00608, 0.07874),
        normal=(0.0, -1.0, 0.0),
        up=(0.0, 0.0, 1.0),
        width=0.07035,
        height=0.15203,
    ),
    bounds=((-0.03835, -0.00608, 0.0), (0.03835, 0.00608, 0.15747)),
)

DEVICE_KIT: Mapping[str, DeviceAsset] = MappingProxyType(
    {a.id: a for a in (MACBOOK_PRO, IPHONE_15_PRO)}
)
KIND_MODEL: Mapping[str, str] = MappingProxyType(
    {"laptop": MACBOOK_PRO.id, "phone": IPHONE_15_PRO.id}
)


def kit_for(kind: str) -> DeviceAsset:
    """The kit model a device template of ``kind`` (laptop, phone) shows."""
    return DEVICE_KIT[KIND_MODEL[kind]]


def by_sha256(sha256: str) -> DeviceAsset | None:
    return next((a for a in DEVICE_KIT.values() if a.sha256 == sha256), None)


# ---- camera from the screen frame ----------------------------------------------------------

KIT_LENS = 70.0
"""Lens (mm) of the kit device cameras."""
KIT_DISTANCE = 1.2
"""Camera distance (m) to the MacBook screen centre; other models scale it by screen width."""
SENSOR = 36.0
"""Sensor width (mm) on the frame's long edge (Blender's AUTO sensor fit)."""
FRAME_MARGIN = 0.95
"""The device bounds must sit inside this fraction of the half-frame."""


@dataclass(frozen=True)
class KitCamera:
    """A camera aimed at the screen centre (Blender XYZ Euler radians, level horizon)."""

    location: Vec3
    rotation: Vec3
    lens: float
    distance: float


def look_rotation(location: Vec3, target: Vec3) -> Vec3:
    """The XYZ Euler that points a camera's (or area light's) -Z axis from
    ``location`` at ``target`` with no roll."""
    d = (target[0] - location[0], target[1] - location[1], target[2] - location[2])
    horizontal = math.hypot(d[0], d[1])
    return (math.atan2(horizontal, -d[2]), 0.0, math.atan2(-d[0], d[1]))


def in_frame(location: Vec3, target: Vec3, lens: float, aspect: float, points: list[Vec3]) -> bool:
    """True when every point projects inside ``FRAME_MARGIN`` of a camera at
    ``location`` aimed at ``target`` (``aspect`` is width / height)."""
    f = _unit((target[0] - location[0], target[1] - location[1], target[2] - location[2]))
    right = _unit(_cross(f, (0.0, 0.0, 1.0)))
    up = _cross(right, f)
    long_half = SENSOR / 2 / lens
    half_w = long_half if aspect >= 1 else long_half * aspect
    half_h = long_half / aspect if aspect >= 1 else long_half
    for p in points:
        rel = (p[0] - location[0], p[1] - location[1], p[2] - location[2])
        depth = _dot(rel, f)
        if depth <= 0:
            return False
        x, y = _dot(rel, right) / depth, _dot(rel, up) / depth
        if abs(x) > half_w * FRAME_MARGIN or abs(y) > half_h * FRAME_MARGIN:
            return False
    return True


def kit_camera(
    entry: DeviceAsset,
    *,
    yaw: float,
    elevation: float,
    aspect: float,
    target: Literal["screen", "bounds"] = "screen",
    scale: float = 1.0,
) -> KitCamera:
    """The camera on ``entry``'s screen: from the screen centre along the screen
    normal turned ``yaw`` degrees about world Z (three-quarter at about 35) and
    raised to ``elevation`` degrees, ``KIT_LENS`` mm, aimed at the screen centre.

    The distance starts at ``KIT_DISTANCE`` scaled by the screen width relative to
    the MacBook's and grows until the whole device (its normalised bounds) is in
    frame at ``aspect``.

    ``target="bounds"`` aims at the device bounds centre instead. ``scale != 1``
    multiplies the fitted distance (no re-fit), still aimed at the screen centre.
    """
    frame = entry.frame
    n = _unit((frame.normal[0], frame.normal[1], 0.0))
    a, e = math.radians(yaw), math.radians(elevation)
    h = (n[0] * math.cos(a) - n[1] * math.sin(a), n[0] * math.sin(a) + n[1] * math.cos(a))
    direction = (h[0] * math.cos(e), h[1] * math.cos(e), math.sin(e))
    lo, hi = entry.bounds
    corners = _corners(lo, hi)
    c = frame.centre
    if target == "bounds":
        c = ((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2)
    distance = KIT_DISTANCE * frame.width / MACBOOK_PRO.frame.width
    for _ in range(80):
        loc = (
            c[0] + direction[0] * distance,
            c[1] + direction[1] * distance,
            c[2] + direction[2] * distance,
        )
        if in_frame(loc, c, KIT_LENS, aspect, corners):
            if scale != 1.0:
                c = frame.centre
                distance *= scale
                loc = (
                    c[0] + direction[0] * distance,
                    c[1] + direction[1] * distance,
                    c[2] + direction[2] * distance,
                )
            return KitCamera(
                location=_round(loc, 4),
                rotation=_round(look_rotation(loc, c), 6),
                lens=KIT_LENS,
                distance=round(distance, 4),
            )
        distance *= 1.04
    raise DeviceAssetError(f"{entry.id}: no camera distance frames the whole device")


# ---- vector and matrix helpers -------------------------------------------------------------

Mat4 = list[list[float]]


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _unit(v: Vec3) -> Vec3:
    n = math.sqrt(_dot(v, v))
    if n == 0:
        raise DeviceAssetError("degenerate screen axis")
    return (v[0] / n, v[1] / n, v[2] / n)


def _matmul(a: Mat4, b: Mat4) -> Mat4:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _apply(m: Mat4, p: Vec3, w: float = 1.0) -> Vec3:
    x, y, z = (m[i][0] * p[0] + m[i][1] * p[1] + m[i][2] * p[2] + m[i][3] * w for i in range(3))
    return (x, y, z)


def _trs(node: Mapping[str, Any]) -> Mat4:
    if "matrix" in node:
        c = [float(v) for v in node["matrix"]]  # column-major
        return [[c[j * 4 + i] for j in range(4)] for i in range(4)]
    tx, ty, tz = (float(v) for v in node.get("translation", (0, 0, 0)))
    qx, qy, qz, qw = (float(v) for v in node.get("rotation", (0, 0, 0, 1)))
    sx, sy, sz = (float(v) for v in node.get("scale", (1, 1, 1)))
    r = [
        [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
        [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
        [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)],
    ]
    return [
        [r[0][0] * sx, r[0][1] * sy, r[0][2] * sz, tx],
        [r[1][0] * sx, r[1][1] * sy, r[1][2] * sz, ty],
        [r[2][0] * sx, r[2][1] * sy, r[2][2] * sz, tz],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _z_up(p: Vec3) -> Vec3:
    """glTF Y-up to Blender Z-up, as Blender's importer converts."""
    return (p[0], -p[2], p[1])


def _corners(lo: Vec3, hi: Vec3) -> list[Vec3]:
    return [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]


# ---- glTF reading --------------------------------------------------------------------------


def gltf_json(glb: bytes) -> dict[str, Any]:
    """The JSON chunk of a binary glTF (``.glb``) file."""
    if len(glb) < 20 or glb[:4] != b"glTF":
        raise DeviceAssetError("not a binary glTF file")
    _magic, version, _length = struct.unpack_from("<4sII", glb, 0)
    length, kind = struct.unpack_from("<I4s", glb, 12)
    if version != 2 or kind != b"JSON":
        raise DeviceAssetError("glb must be glTF 2.0 with a leading JSON chunk")
    doc: dict[str, Any] = json.loads(glb[20 : 20 + length])
    return doc


def _world_matrices(doc: Mapping[str, Any]) -> dict[int, Mat4]:
    nodes = doc.get("nodes", [])
    scene = doc.get("scenes", [{}])[doc.get("scene", 0)]
    roots = scene.get("nodes", range(len(nodes)))
    out: dict[int, Mat4] = {}

    def walk(i: int, parent: Mat4 | None) -> None:
        local = _trs(nodes[i])
        world = local if parent is None else _matmul(parent, local)
        out[i] = world
        for child in nodes[i].get("children", []):
            walk(child, world)

    for root in roots:
        walk(root, None)
    return out


def _bounds(doc: Mapping[str, Any], prim: Mapping[str, Any]) -> tuple[Vec3, Vec3]:
    acc = doc["accessors"][prim["attributes"]["POSITION"]]
    lo, hi = acc.get("min"), acc.get("max")
    if lo is None or hi is None:
        raise DeviceAssetError("POSITION accessor has no min/max bounds")
    return (lo[0], lo[1], lo[2]), (hi[0], hi[1], hi[2])


@dataclass(frozen=True)
class _Normalise:
    """Imported (glTF world, converted to Z-up) to normalised: ``p * scale + offset``."""

    scale: float
    offset: Vec3

    def __call__(self, p: Vec3) -> Vec3:
        return (
            p[0] * self.scale + self.offset[0],
            p[1] * self.scale + self.offset[1],
            p[2] * self.scale + self.offset[2],
        )


def _world_points(doc: Mapping[str, Any], mats: Mapping[int, Mat4]) -> list[Vec3]:
    nodes = doc.get("nodes", [])
    points: list[Vec3] = []
    for i, world in mats.items():
        mesh = nodes[i].get("mesh")
        if mesh is None:
            continue
        for prim in doc["meshes"][mesh]["primitives"]:
            lo, hi = _bounds(doc, prim)
            points += [_z_up(_apply(world, c)) for c in _corners(lo, hi)]
    if not points:
        raise DeviceAssetError("glb has no meshes")
    return points


def _normaliser(points: list[Vec3], real_width: float) -> tuple[_Normalise, tuple[Vec3, Vec3]]:
    xs, zs = [p[0] for p in points], [p[2] for p in points]
    width = max(xs) - min(xs)
    if width <= 0:
        raise DeviceAssetError("glb has zero width")
    s = real_width / width
    norm = _Normalise(scale=s, offset=(0.0, 0.0, -min(zs) * s))
    moved = [norm(p) for p in points]
    lo = (min(p[0] for p in moved), min(p[1] for p in moved), min(p[2] for p in moved))
    hi = (max(p[0] for p in moved), max(p[1] for p in moved), max(p[2] for p in moved))
    return norm, (lo, hi)


def _round(v: Vec3, n: int = 5) -> Vec3:
    x, y, z = (round(c, n) + 0.0 for c in v)
    return (x, y, z)


def screen_frame(corners: list[Vec3]) -> ScreenFrame:
    """The frame of a planar screen from its (world, normalised) bounding-box corners.

    The plane's normal is the box's thin axis, turned to face the front (-Y). Up is
    the in-plane axis closest to world +Z; width and height are the extents along
    the screen's right and up directions.
    """
    origin = corners[0]
    # The box's three edge directions from corner 0 (corners vary z fastest, then y, x).
    edges = [corners[1], corners[2], corners[4]]
    axes = [tuple(e[i] - origin[i] for i in range(3)) for e in edges]
    lengths = [math.sqrt(sum(c * c for c in a)) for a in axes]
    thin = min(range(3), key=lambda i: lengths[i])
    flat = [i for i in range(3) if i != thin]
    a, b = (_unit(axes[i]) for i in flat)  # type: ignore[arg-type]
    normal = _unit(_cross(a, b))
    if _dot(normal, FRONT) < 0:
        normal = (-normal[0], -normal[1], -normal[2])
    up = a if abs(a[2]) >= abs(b[2]) else b
    if up[2] < 0:
        up = (-up[0], -up[1], -up[2])
    right = _cross(up, normal)
    centre_t = [sum(p[i] for p in corners) / len(corners) for i in range(3)]
    centre: Vec3 = (centre_t[0], centre_t[1], centre_t[2])
    rel = [(p[0] - centre[0], p[1] - centre[1], p[2] - centre[2]) for p in corners]
    width = max(_dot(r, right) for r in rel) - min(_dot(r, right) for r in rel)
    height = max(_dot(r, up) for r in rel) - min(_dot(r, up) for r in rel)
    return ScreenFrame(
        centre=_round(centre),
        normal=_round(normal),
        up=_round(up),
        width=round(width, 5),
        height=round(height, 5),
    )


def _screen_corners(doc: Mapping[str, Any], mats: Mapping[int, Mat4], entry: DeviceAsset) -> Any:
    nodes = doc.get("nodes", [])
    materials = doc.get("materials", [])
    for i, world in mats.items():
        if nodes[i].get("name") != entry.screen.node or nodes[i].get("mesh") is None:
            continue
        for prim in doc["meshes"][nodes[i]["mesh"]]["primitives"]:
            mat = prim.get("material")
            if mat is not None and materials[mat].get("name") == entry.screen.material:
                lo, hi = _bounds(doc, prim)
                return [_z_up(_apply(world, c)) for c in _corners(lo, hi)]
    raise DeviceAssetError(
        f"{entry.id}: no node {entry.screen.node!r} with material {entry.screen.material!r}"
    )


def measure(glb: bytes, entry: DeviceAsset) -> tuple[ScreenFrame, tuple[Vec3, Vec3]]:
    """The normalised screen frame and model bounds of ``glb`` dressed as ``entry``."""
    doc = gltf_json(glb)
    mats = _world_matrices(doc)
    norm, bounds = _normaliser(_world_points(doc, mats), entry.real_width)
    frame = screen_frame([norm(p) for p in _screen_corners(doc, mats, entry)])
    lo, hi = bounds
    return frame, (_round(lo), _round(hi))


def measure_screen(glb: bytes, entry: DeviceAsset) -> ScreenFrame:
    """The display's centre, normal, up, width and height in metres once ``glb`` is
    normalised (real width, lowest point on the floor, Blender Z-up)."""
    return measure(glb, entry)[0]


# ---- fetch and verify ----------------------------------------------------------------------


def _urlopen(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - pinned https URL
        data: bytes = resp.read()
    return data


def default_cache_dir() -> Path:
    """The shared assets dir: ``<SPECTRACUT_HOME>/assets``."""
    from spectracut.studio.store import spectracut_home

    return spectracut_home() / "assets"


def ensure_device_asset(
    entry: DeviceAsset, cache_dir: Path | None = None, fetch: Fetch = _urlopen
) -> Path:
    """The verified local copy of ``entry``: ``<cache_dir>/devices/<sha256>.glb``.

    A cached file is reused when its sha256 matches; otherwise the pinned URL is
    fetched, verified and written atomically. A download whose sha256 differs from
    the pin is refused (and nothing is written).
    """
    from spectracut.studio.store import write_atomic

    root = cache_dir if cache_dir is not None else default_cache_dir()
    path = root / entry.path
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == entry.sha256:
        return path
    try:
        data = fetch(entry.url)
    except OSError as exc:
        raise DeviceAssetError(f"{entry.id}: cannot fetch {entry.url}: {exc}") from exc
    got = hashlib.sha256(data).hexdigest()
    if got != entry.sha256:
        raise DeviceAssetError(
            f"{entry.id}: {entry.url} has sha256 {got}, the manifest pins {entry.sha256}"
        )
    write_atomic(path, data)
    return path
