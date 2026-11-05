# SPDX-License-Identifier: AGPL-3.0-or-later
"""E2E (#55): comp graphs on the farm: comp-only re-runs and cryptomatte screen isolation.

Runs the ``device-shot-comped`` graph on the real Flamenco farm with Blender.
``render_scene`` writes multilayer EXR with passes once. ``comp`` runs the shot's
comp graph on it as a compositor-only job, and ``finish`` grades the result.
Runs only with ``SPECTRACUT_E2E=1`` and ``SPECTRACUT_FLAMENCO_URL`` (post-deploy
checks ``comp-only-rerun-farm-e2e`` and ``cryptomatte-screen-grade-farm-e2e``).
The fake-Flamenco version of the re-run assertion is in ``test_comp.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from spectracut.cli import studio as studio_mod
from spectracut.harness.imagecmp import Image, decode_png
from spectracut.qa.legibility import screen_legibility
from spectracut.studio import device_shot as ds
from spectracut.studio.graph import ProductionGraph
from spectracut.studio.store import ProductionStore

E2E = os.environ.get("SPECTRACUT_E2E") == "1" and bool(os.environ.get("SPECTRACUT_FLAMENCO_URL"))
needs_farm = pytest.mark.skipif(not E2E, reason="set SPECTRACUT_E2E=1 and SPECTRACUT_FLAMENCO_URL")


def _farm(graph: ProductionGraph, production: str) -> Any:
    home = Path(os.environ.get("SPECTRACUT_HOME") or Path.home() / ".local/share/spectracut")
    deps = studio_mod.default_device_shot(studio_mod.Production(production, ProductionStore(home)))
    return ds.device_shot_runner(
        graph, deps.config, client=deps.client, judge=deps.judge, key=deps.key
    )


def _master(graph: ProductionGraph) -> Image:
    out = graph.output("finish")
    assert isinstance(out, dict)
    return decode_png((Path(str(out["output_dir"])) / "frame_0001.png").read_bytes())


def _render_ledger(graph: ProductionGraph) -> tuple[int, float]:
    entries = [e for e in graph.ledger.entries() if e.kind.startswith("render_")]
    return len(entries), sum(e.gpu_minutes for e in entries)


@pytest.mark.e2e
@needs_farm
def test_e2e_comp_change_rerenders_nothing() -> None:
    from test_comp import comped_graph, set_comp  # type: ignore[import-not-found]

    graph = comped_graph("device-clean")
    run = _farm(graph, "alterlab-comp-rerun-e2e")
    first = graph.run(run)
    print(first)
    assert not first.failed and "comp" in first.executed and "finish" in first.executed
    scene = graph.output("render_scene")
    assert isinstance(scene, dict) and scene["frames"]
    assert all(name.endswith(".exr") for name in scene["frames"])
    renders = _render_ledger(graph)

    assert set_comp(graph, "device-cinematic") == {"comp", "finish"}
    second = graph.run(run)
    print(second)
    assert second.executed == ["comp", "finish"] and not second.failed
    assert graph.output("render_scene") == scene
    assert _render_ledger(graph) == renders
    master = _master(graph)
    assert master.channels >= 3 and master.width > 0


def _diff_bbox(a: Image, b: Image, tolerance: int = 1) -> tuple[int, int, int, int] | None:
    assert (a.width, a.height, a.channels) == (b.width, b.height, b.channels)
    n, xs, ys = a.channels, [], []
    for y in range(a.height):
        for x in range(a.width):
            at = (y * a.width + x) * n
            if any(abs(a.pixels[at + c] - b.pixels[at + c]) > tolerance for c in range(3)):
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


@pytest.mark.e2e
@needs_farm
def test_e2e_cryptomatte_isolates_screen() -> None:
    """``screen-grade`` vs ``device-clean``: differences stay inside the screen's matte.

    Both graphs differ only in screen ops, so every pixel that differs by more
    than 1 code value lies in the screen. Their bounding box must be a small
    part of the frame (a grade leaking past the matte would cover it all), and
    the graded screen inside it must stay legible.
    """
    from test_comp import comped_graph, set_comp  # type: ignore[import-not-found]

    graph = comped_graph("device-clean")
    run = _farm(graph, "alterlab-comp-crypto-e2e")
    first = graph.run(run)
    print(first)
    assert not first.failed
    clean = _master(graph)
    set_comp(graph, "screen-grade")
    second = graph.run(run)
    print(second)
    assert second.executed == ["comp", "finish"] and not second.failed
    graded = _master(graph)
    bbox = _diff_bbox(clean, graded)
    assert bbox is not None, "the screen grade changed nothing"
    x, y, w, h = bbox
    assert w * h <= 0.6 * clean.width * clean.height, bbox
    out = graph.output("finish")
    assert isinstance(out, dict)
    png = (Path(str(out["output_dir"])) / "frame_0001.png").read_bytes()
    assert screen_legibility(png, bbox).passed
