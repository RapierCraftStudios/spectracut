# SPDX-License-Identifier: AGPL-3.0-or-later
"""M2 (#29): a style frame rendered by Blender at final tier on the farm, then approved.

Real end to end: the AlterLab dashboard device shot renders at the **final** tier on
the Flamenco farm (the same path as ``test_m1_device_shot.py``); its frame becomes the
style frame's render, passes the pre-production runner and waits at the human gate
until a person approves it. Runs only with ``SPECTRACUT_E2E=1`` and
``SPECTRACUT_FLAMENCO_URL`` (post-deploy check ``m2-preproduction-style-frames-farm-e2e``).
The mock-render version is ``tests/storyboard/test_preproduction.py``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spectracut.cli.app import app
from spectracut.providers.local.registry import MockLocalProvider
from spectracut.providers.mock_text import MockTextProvider
from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.storyboard.explore import record_image
from spectracut.storyboard.models import ArtDirection, ImageRecord, StyleFrame, StyleFrames
from spectracut.studio import (
    CapacityLimits,
    DepartmentRunner,
    GateMode,
    GatePolicy,
    NodeSpec,
    NodeStatus,
    ProductionGraph,
)
from spectracut.studio.departments.art import (
    ART_DEPARTMENTS,
    STYLE_FRAMES,
    PreproductionRunner,
    preproduction_nodes,
)
from spectracut.studio.node import Tier
from spectracut.studio.store import ProductionStore

E2E = os.environ.get("SPECTRACUT_E2E") == "1" and bool(os.environ.get("SPECTRACUT_FLAMENCO_URL"))
runner = CliRunner()
ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "brands" / "alterlab" / "brand-kit.yaml"

SOURCES = [{"id": "site", "title": "AlterLab", "ref": "https://alterlab.io"}]
CREATIVE_BRIEF = {
    "id": "alterlab-dashboard",
    "message": "One view of every run.",
    "audience": {"personas": [{"name": "Ops lead", "description": "runs a lab"}]},
    "sources": SOURCES,
    "proof_points": [{"claim": "One view of every run", "source_id": "site"}],
    "tone": "plain",
    "success_measure": "dashboard visits",
    "cta": "See the dashboard",
}


def _brief() -> Brief:
    return Brief(
        id="alterlab-dashboard",
        product="AlterLab dashboard",
        audience=AudienceProfile(personas=[Persona(name="Ops lead", description="runs a lab")]),
        sources=[ClaimSource(id="site", title="AlterLab", ref="https://alterlab.io")],
        proof_points=[ProofPoint(claim="One view of every run", source_id="site")],
        cta="See the dashboard",
    )


ART = {
    "mood_board": [{"ref": "clean studio product shot", "take": "soft key light"}],
    "palette": {"swatches": {"ink": "#101820"}},
    "look": {"id": "clean"},
}
FRAMES = {"frames": [{"id": "hero", "description": "Dashboard", "prompt": "clean dashboard"}]}


@pytest.mark.e2e
@pytest.mark.skipif(not E2E, reason="set SPECTRACUT_E2E=1 and SPECTRACUT_FLAMENCO_URL")
def test_e2e_style_frame_renders_at_final_tier_on_the_farm_and_needs_approval(
    tmp_path: Path,
) -> None:
    home = os.environ.get("SPECTRACUT_HOME") or str(Path.home() / ".local/share/spectracut")
    brief = tmp_path / "brief.json"
    brief.write_text(_brief().model_dump_json(), encoding="utf-8")
    pid = "alterlab-style-frame-e2e"
    base = ["--home", home]
    new = runner.invoke(
        app,
        [
            "studio",
            "new",
            str(brief),
            "--profile",
            "device-shot",
            "--brand-kit",
            str(KIT),
            "--id",
            pid,
        ]
        + base
        + ["--force"],
    )
    assert new.exit_code == 0, new.output

    def farm_render(frame: StyleFrame, art: ArtDirection, path: Path) -> ImageRecord:
        run = runner.invoke(app, ["studio", "run", pid, *base])
        assert run.exit_code == 0, run.output
        out = ProductionStore(Path(home)).load(pid)[0].output("render_final")
        assert isinstance(out, dict) and out["tier"] == "final"
        rendered = sorted(Path(str(out["output_dir"])).glob("*.png"))
        assert rendered, "the farm produced no frame"
        shutil.copyfile(rendered[0], path)
        return record_image(path, "blender-farm")

    def respond(system: str, prompt: str, schema: dict) -> dict:  # type: ignore[type-arg]
        return ART if schema["title"] == "ArtDirection" else FRAMES

    graph = ProductionGraph("style-e2e", gates=GatePolicy(defaults={STYLE_FRAMES: GateMode.HUMAN}))
    brief_out = CREATIVE_BRIEF
    graph.add_nodes(
        [NodeSpec(id="brief", kind="brief", department="stub"), *preproduction_nodes()[:2]]
    )
    inner = DepartmentRunner(
        ART_DEPARTMENTS, MockTextProvider(respond=respond), limits=CapacityLimits(), graph=graph
    )
    art = PreproductionRunner(
        inner,
        {d.name: d for d in ART_DEPARTMENTS},
        explorer=MockLocalProvider("image"),
        renderer=farm_render,
        voice=lambda text, path: None,  # type: ignore[arg-type,return-value]
        workdir=tmp_path / "work",
    )

    def run(ctx):  # type: ignore[no-untyped-def]
        from spectracut.studio.graph import RunResult

        return RunResult(output=brief_out) if ctx.spec.kind == "brief" else art(ctx)

    graph.run(run)
    assert graph.spec(STYLE_FRAMES).tier is Tier.FINAL
    assert graph.status(STYLE_FRAMES) is NodeStatus.AWAITING_APPROVAL
    frames = StyleFrames.model_validate(graph._pending[STYLE_FRAMES][1].output)
    assert frames.rendered and frames.frames[0].render is not None
    graph.approve(STYLE_FRAMES, "approved on the farm render")
    assert graph.status(STYLE_FRAMES) is NodeStatus.CLEAN
