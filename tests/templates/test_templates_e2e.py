# SPDX-License-Identifier: AGPL-3.0-or-later
"""E2E (#199): shot templates rendered by real Blender on the Flamenco farm.

- ``test_e2e_each_template_previews_in_under_five_minutes``: every template's
  preview (Eevee, half resolution) from its spec finishes in under 300 s wall
  time on the RTX 3070-class worker (post-deploy check ``templates-preview-farm-e2e``).
- ``test_e2e_device_screen_dolly_has_no_moire``: a laptop dolly renders the
  screen at several camera distances; the high-frequency energy in the screen
  region stays bounded and steady across distances, which moire would break
  (post-deploy check ``device-screen-moire-dolly-farm-e2e``).
- ``test_e2e_device_hero_shot_has_dof_camera_motion_and_legible_ui`` (#339): a device
  shot built from a UI capture by the shot runner's recipe renders at final tier on
  legion; the camera moves between the first and last frame, the focus pull sharpens
  the screen and the UI reads by OCR at delivery size (post-deploy check
  ``device-hero-shot-legion-e2e``).

Runs only with ``SPECTRACUT_E2E=1`` and ``SPECTRACUT_FLAMENCO_URL`` (plus
``SPECTRACUT_FLAMENCO_SHARED`` / ``SPECTRACUT_FLAMENCO_TAG``), like
``tests/finishing/test_looks_e2e.py``; the unit criteria are in ``test_templates.py``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from spectracut.harness.imagecmp import decode_png
from spectracut.spec.brand import BrandKit, load_brand_kit
from spectracut.spec.recipe import Recipe
from spectracut.studio import device_shot as ds
from spectracut.studio.node import Tier
from spectracut.templates import LAPTOP, TEMPLATES
from spectracut.templates.base import CameraVariant

E2E = os.environ.get("SPECTRACUT_E2E") == "1" and bool(os.environ.get("SPECTRACUT_FLAMENCO_URL"))
skip_unless_e2e = pytest.mark.skipif(
    not E2E, reason="set SPECTRACUT_E2E=1 and SPECTRACUT_FLAMENCO_URL"
)

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "brands" / "alterlab" / "brand-kit.yaml"
SCREEN = ROOT / "brands" / "alterlab" / "assets" / "ui" / "dashboard.png"
PREVIEW_BUDGET_S = 300.0
DOLLY_DISTANCES = (0.6, 0.95, 1.6, 2.6, 4.0)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _flat_hdri(path: Path) -> None:
    """A tiny uncompressed Radiance HDR (soft grey sky over a darker floor)."""
    width, height = 4, 2
    rows = [b"\x80\x80\x80\x81" * width, b"\x40\x40\x40\x80" * width]
    header = f"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y {height} +X {width}\n".encode()
    path.write_bytes(header + b"".join(rows))


def _assets(tmp: Path) -> tuple[Path, dict[str, Any]]:
    """An assets dir with the AlterLab dashboard as the screen and a generated HDRI."""
    root = tmp / "assets"
    (root / "ui").mkdir(parents=True)
    (root / "hdri").mkdir()
    frame = root / "ui" / "screen_0001.png"
    shutil.copyfile(SCREEN, frame)
    licence = SCREEN.with_name(SCREEN.name + ".license.json")
    if licence.exists():
        shutil.copyfile(licence, frame.with_name(frame.name + ".license.json"))
    hdri = root / "hdri" / "studio.hdr"
    _flat_hdri(hdri)
    slots = {
        "screen_frames": [{"path": "ui/screen_0001.png", "sha256": _sha(frame)}],
        "hdri": {"path": "hdri/studio.hdr", "sha256": _sha(hdri)},
    }
    return root, slots


def _slots(device: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "device-laptop": device,
        "device-phone": device,
        "hero-object": {"animation": "turntable", "frame_end": 24},
        "end-card": {"cta": "See every run at a glance"},
        "typography": {"body": "Ship the film", "mode": "cutout"},
    }


def _render(recipe: Recipe, assets: Path) -> tuple[Path, float]:
    """Submit ``recipe`` (one frame) to the farm; return its output dir and wall seconds."""
    from spectracut.queue.flamenco import FlamencoClient
    from spectracut.queue.signing import load_private_key

    config = ds.DeviceShotConfig.from_env()
    home = Path(os.environ.get("SPECTRACUT_HOME") or Path.home() / ".local/share/spectracut")
    key = load_private_key(home=home)
    client = FlamencoClient(config.flamenco_url)
    assets_dir, _sha = ds._stage_for(config, str(assets), recipe)
    opts = ds.ExecutorOptions()
    job_id = ds._fresh_job_id(config, f"e2e-{recipe.shot_id}"[:60])
    out = config.out_dir(job_id)
    started = time.monotonic()
    envelope = ds._envelope(job_id, recipe, config, opts.now())
    job_file = ds.sign_to_shared(envelope, key, config.shared)
    job = ds._blender_job(envelope, job_file, out, config, Tier.PREVIEW, assets_dir)
    ds._executor(client, out, config, opts, assets_dir).run(job)
    return out, time.monotonic() - started


def _kit() -> BrandKit:
    return load_brand_kit(KIT)


def _render_frames(recipe: Recipe, assets: Path, frame_end: int, extra: Any) -> Path:
    """Submit ``recipe`` (frames 1..frame_end) at final tier with ``extra`` staged."""
    from spectracut.queue.flamenco import FlamencoClient
    from spectracut.queue.signing import load_private_key

    config = dataclasses.replace(ds.DeviceShotConfig.from_env(), assets_dir=None)
    home = Path(os.environ.get("SPECTRACUT_HOME") or Path.home() / ".local/share/spectracut")
    key = load_private_key(home=home)
    client = FlamencoClient(config.flamenco_url)
    assets_dir, _sha = ds.stage_for(config, str(assets), recipe, extra)
    opts = ds.ExecutorOptions()
    job_id = ds.fresh_job_id(config, f"e2e-{recipe.shot_id}"[:60])
    out = config.out_dir(job_id)
    envelope = ds.envelope(job_id, recipe, config, opts.now(), frame_end=frame_end)
    job_file = ds.sign_to_shared(envelope, key, config.shared)
    job = ds.blender_job(envelope, job_file, out, config, Tier.FINAL, assets_dir)
    ds.executor(client, out, config, opts, assets_dir).run(job)
    return out


@pytest.mark.e2e
@skip_unless_e2e
@pytest.mark.parametrize("template_id", sorted(TEMPLATES))
def test_e2e_each_template_previews_in_under_five_minutes(template_id: str, tmp_path: Path) -> None:
    assets, device = _assets(tmp_path)
    recipe = TEMPLATES[template_id].preview(_kit(), _slots(device)[template_id])
    out, seconds = _render(recipe, assets)
    print(f"{template_id}: preview in {seconds:.1f}s -> {out}")
    frames = sorted(out.glob("frame_*.png"))
    assert frames, f"no preview frame in {out}"
    image = decode_png(frames[0].read_bytes())
    assert image.width == 960 and image.height == 540
    assert seconds < PREVIEW_BUDGET_S


def _luma(image: Any) -> list[list[float]]:
    c, w = image.channels, image.width
    px = image.pixels
    return [
        [
            (
                0.2126 * px[(y * w + x) * c]
                + 0.7152 * px[(y * w + x) * c + 1]
                + 0.0722 * px[(y * w + x) * c + 2]
            )
            / 255.0
            for x in range(w)
        ]
        for y in range(image.height)
    ]


def high_frequency_energy(luma: list[list[float]], box: tuple[int, int, int, int]) -> float:
    """Mean absolute 4-neighbour Laplacian over ``box`` (x0, y0, x1, y1), per unit luminance."""
    x0, y0, x1, y1 = box
    total, mean, n = 0.0, 0.0, 0
    for y in range(max(y0, 1), min(y1, len(luma) - 1)):
        row = luma[y]
        for x in range(max(x0, 1), min(x1, len(row) - 1)):
            lap = 4 * row[x] - row[x - 1] - row[x + 1] - luma[y - 1][x] - luma[y + 1][x]
            total += abs(lap)
            mean += row[x]
            n += 1
    return total / max(mean, 1e-6) if n else 0.0


def _dolly(distance: float) -> Any:
    base = LAPTOP.variants[0]
    near = CameraVariant(
        id="dolly",
        location=(0, -distance, base.location[2]),
        rotation=base.rotation,
        lens=base.lens,
        vertical_location=(0, -distance * 1.6, base.location[2]),
        vertical_rotation=base.rotation,
        vertical_lens=base.lens,
    )
    return dataclasses.replace(LAPTOP, variants=(near, LAPTOP.variants[1]))


def test_high_frequency_energy_flags_a_moire_pattern() -> None:
    """The dolly metric itself: a fine checker scores far above a smooth gradient."""
    flat = [[0.5 + 0.001 * x for x in range(32)] for _ in range(32)]
    checker = [[0.2 + 0.6 * ((x + y) % 2) for x in range(32)] for y in range(32)]
    box = (0, 0, 32, 32)
    assert high_frequency_energy(flat, box) < 0.01 < 1.0 < high_frequency_energy(checker, box)


@pytest.mark.e2e
@skip_unless_e2e
def test_e2e_device_screen_dolly_has_no_moire(tmp_path: Path) -> None:
    assets, device = _assets(tmp_path)
    energies = []
    for distance in DOLLY_DISTANCES:
        recipe = _dolly(distance).preview(_kit(), device)
        out, _seconds = _render(recipe, assets)
        image = decode_png(sorted(out.glob("frame_*.png"))[0].read_bytes())
        # the screen sits at the frame centre; shrink the box as the camera backs off
        half_w = max(int(image.width * 0.3 * min(1.0, 0.95 / distance)), 8)
        half_h = max(int(image.height * 0.3 * min(1.0, 0.95 / distance)), 8)
        cx, cy = image.width // 2, image.height // 2
        box = (cx - half_w, cy - half_h, cx + half_w, cy + half_h)
        energies.append(high_frequency_energy(_luma(image), box))
    print(dict(zip(DOLLY_DISTANCES, energies, strict=True)))
    assert max(energies) < 0.5, energies
    assert max(energies) <= 3 * max(min(energies), 1e-3), energies


@pytest.mark.e2e
@skip_unless_e2e
def test_e2e_device_hero_shot_has_dof_camera_motion_and_legible_ui(tmp_path: Path) -> None:
    from spectracut.qa.copy_ocr import available_reader
    from spectracut.spec.brand import Look
    from spectracut.storyboard.models import Board
    from spectracut.studio.departments import shots as sd
    from spectracut.studio.departments.capture import ScreenCapture

    reader = available_reader()
    assert reader is not None, "tesseract is needed on the controller for the legibility check"
    assets, _device = _assets(tmp_path)
    sha = _sha(SCREEN)
    frame = {"path": str(SCREEN), "sha256": sha}
    capture = ScreenCapture.model_validate(
        {
            "key": "beat:dashboard",
            "beat": "dashboard",
            "route": "/",
            "url": "http://127.0.0.1/",
            "device": "laptop",
            "fps": 24,
            "clip": [frame],
            "still": frame,
            "manifest": "m",
            "events": "e",
            "text": "t",
            "provenance": {
                "product_id": "alterlab",
                "url": "http://127.0.0.1/",
                "steps": [],
                "device": "laptop",
                "spec_sha256": sha,
                "frames_sha256": hashlib.sha256(sha.encode()).hexdigest(),
                "rig": "e2e",
                "demo_check": "passed",
                "deterministic": True,
            },
        }
    )
    board = Board.model_validate(
        {
            "shot_id": "device-hero",
            "beat": "dashboard",
            "action": "push in on the runs dashboard",
            "duration_s": 0.5,
            "move": "push",
            "device": "laptop",
        }
    )
    look = Look(id="device-hero", motion_blur=True, depth_of_field=True)
    base, _slots, extra = sd.device_recipe(
        _kit(), board, capture, variant="front", aspect="16:9", look=look
    )
    frames_n = sd.shot_frames(board)
    out = _render_frames(sd.tier_recipe(base, Tier.FINAL), assets, frames_n, extra)
    frames = sorted(out.glob("frame_*.png"))
    assert len(frames) == frames_n, frames
    first, last = (decode_png(f.read_bytes()) for f in (frames[0], frames[-1]))
    assert (last.width, last.height) == (1920, 1080)
    a, b = _luma(first), _luma(last)
    # Camera motion: the push changes the frame.
    diff = sum(abs(x - y) for ra, rb in zip(a, b, strict=True) for x, y in zip(ra, rb, strict=True))
    assert diff / (last.width * last.height) > 0.01
    # Depth of field: the focus pull lands on the screen, sharper at the end than the start.
    cx, cy = last.width // 2, last.height // 2
    box = (cx - last.width // 6, cy - last.height // 6, cx + last.width // 6, cy + last.height // 6)
    assert high_frequency_energy(b, box) > 1.2 * high_frequency_energy(a, box)
    # Legible at delivery size: OCR reads words of the UI off the in-focus frame.
    words = [t for t in reader.read(frames[-1].read_bytes()) if len(t.text.strip()) >= 3]
    assert len(words) >= 3, words
