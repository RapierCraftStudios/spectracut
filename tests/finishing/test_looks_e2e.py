# SPDX-License-Identifier: AGPL-3.0-or-later
"""E2E (#54): the same shot under two Looks on the farm differs only in finishing.

Runs the ``device-shot-graded`` graph on the real Flamenco farm with Blender:
``render_scene`` writes linear EXR frames once, then ``finish`` grades them under
``clean`` and then ``cinematic``; the second Look re-runs only ``finish`` and the
render's frames are unchanged. Runs only with ``SPECTRACUT_E2E=1`` and
``SPECTRACUT_FLAMENCO_URL`` (post-deploy check ``looks-reuse-render-farm-e2e``);
the fake-Flamenco version of the same assertion is in ``test_looks.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from spectracut.cli import studio as studio_mod
from spectracut.harness.imagecmp import decode_png
from spectracut.studio import device_shot as ds
from spectracut.studio.store import ProductionStore

E2E = os.environ.get("SPECTRACUT_E2E") == "1" and bool(os.environ.get("SPECTRACUT_FLAMENCO_URL"))


@pytest.mark.e2e
@pytest.mark.skipif(not E2E, reason="set SPECTRACUT_E2E=1 and SPECTRACUT_FLAMENCO_URL")
def test_e2e_switching_looks_reuses_the_render_on_the_farm() -> None:
    from test_looks import _graded_graph, _kit, _set_look  # type: ignore[import-not-found]

    home = Path(os.environ.get("SPECTRACUT_HOME") or Path.home() / ".local/share/spectracut")
    deps = studio_mod.default_device_shot(
        studio_mod.Production("alterlab-looks-e2e", ProductionStore(home))
    )
    graph = _graded_graph(_kit())
    run = ds.device_shot_runner(
        graph, deps.config, client=deps.client, judge=deps.judge, key=deps.key
    )
    first = graph.run(run)
    print(first)
    assert not first.failed and "finish" in first.executed
    scene = graph.output("render_scene")
    assert isinstance(scene, dict) and scene["frames"]
    assert all(name.endswith(".exr") for name in scene["frames"])
    clean = graph.output("finish")
    assert isinstance(clean, dict)

    assert _set_look(graph, "cinematic") == {"finish"}
    second = graph.run(run)
    print(second)
    assert second.executed == ["finish"] and not second.failed
    assert graph.output("render_scene") == scene
    cinematic = graph.output("finish")
    assert isinstance(cinematic, dict) and cinematic["look_id"] == "cinematic"
    assert cinematic["render_recipe_hash"] == clean["render_recipe_hash"] == scene["recipe_hash"]
    assert cinematic["frames"] != clean["frames"]
    for out in (clean, cinematic):
        master = Path(str(out["output_dir"])) / "frame_0001.png"
        image = decode_png(master.read_bytes())
        assert image.channels >= 3 and image.width > 0
