# SPDX-License-Identifier: AGPL-3.0-or-later
"""The host reports its render profile, and it is part of the frame cache key."""

from __future__ import annotations

import pytest
from spectracut_host.profile import (
    gpu_slug,
    parse_blender_version,
    parse_nvidia_smi,
    probe_profile,
    worker_tags,
)

from spectracut.queue.profile import RenderProfile, frame_cache_key

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
BASE = RenderProfile(
    blender_version="4.5.3",
    gpu_model="NVIDIA GeForce RTX 4070 Ti",
    gpu_driver="560.94",
    vram_gb=12,
    os="windows",
)


def test_same_recipe_and_profile_give_same_key() -> None:
    assert frame_cache_key(HASH_A, BASE) == frame_cache_key(HASH_A, BASE.model_copy())
    assert frame_cache_key(HASH_A, BASE).startswith("sha256:")


@pytest.mark.parametrize(
    "change",
    [
        {"blender_version": "4.5.4"},
        {"gpu_model": "NVIDIA GeForce RTX 3090"},
        {"gpu_driver": "561.09"},
    ],
)
def test_profile_changes_the_key(change: dict[str, str]) -> None:
    other = BASE.model_copy(update=change)
    assert frame_cache_key(HASH_A, other) != frame_cache_key(HASH_A, BASE)


def test_recipe_hash_changes_the_key() -> None:
    assert frame_cache_key(HASH_A, BASE) != frame_cache_key(HASH_B, BASE)


def test_bad_recipe_hash_rejected() -> None:
    with pytest.raises(ValueError):
        frame_cache_key("abc", BASE)


def test_probe_profile_reports_blender_gpu_driver() -> None:
    outputs = {
        "blender": "Blender 4.5.3 LTS\n\tbuild hash: abc\n",
        "nvidia-smi": "NVIDIA GeForce RTX 4070 Ti, 560.94, 12282\n",
    }

    def runner(cmd: list[str]) -> str | None:
        return outputs.get(cmd[0])

    profile = probe_profile("blender", runner)
    assert profile.blender_version == "4.5.3"
    assert profile.gpu_model == "NVIDIA GeForce RTX 4070 Ti"
    assert profile.gpu_driver == "560.94"
    assert profile.vram_gb == 12


def test_probe_profile_falls_back_to_unknown() -> None:
    profile = probe_profile("blender", lambda cmd: None)
    assert profile.blender_version == "unknown"
    assert profile.gpu_model == "unknown"
    assert profile.gpu_driver == "unknown"
    assert profile.vram_gb == 0


def test_parsers_tolerate_garbage() -> None:
    assert parse_blender_version("nothing") == "unknown"
    assert parse_nvidia_smi("only-one-field") == ("unknown", "unknown", 0)
    assert parse_nvidia_smi("GPU, 1.0, n/a") == ("GPU", "1.0", 0)


@pytest.mark.parametrize(
    ("model", "slug"),
    [
        ("NVIDIA GeForce RTX 4070 Ti", "4070ti"),
        ("NVIDIA GeForce RTX 3090", "3090"),
        ("NVIDIA RTX A6000", "a6000"),
        ("unknown", None),
    ],
)
def test_gpu_slug(model: str, slug: str | None) -> None:
    assert gpu_slug(model) == slug


def test_worker_tags_carry_gpu_vram_and_capabilities() -> None:
    assert worker_tags(BASE) == ["4070ti", "vram-12gb", "eevee", "cycles", "workbench"]
    assert worker_tags(BASE, ["Cycles"]) == ["4070ti", "vram-12gb", "cycles"]
    bare = RenderProfile(blender_version="4.5.3")
    assert worker_tags(bare, ["cycles"]) == ["cycles"]
