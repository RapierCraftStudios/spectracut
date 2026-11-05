# SPDX-License-Identifier: AGPL-3.0-or-later
"""M2 (#26): one production yields three 15 s 9:16 ads and one 30 s 16:9 ad.

The always-run case drives the real :class:`ProductionGraph` (human-gated render
nodes, one per shot and aspect), the real ``Edit`` and OpenTimelineIO round trip,
and the cut-down builder; only the render department is a counting fake. After
the cut-downs are made the graph is run again and must dispatch nothing.

The ``e2e`` case runs on the farm host after the M1 check
(``m1-device-shot-farm-e2e``) has rendered and stored production
``alterlab-dashboard-e2e`` (override with ``SPECTRACUT_CUTDOWN_PRODUCTION``): it
cuts down and re-runs that stored production through the CLI and asserts no new
farm work. The device-shot runner renders one 1920x1200 frame
(``studio/device_shot.py``), so the per-aspect media in that case are records
for the film's shots, not farm frames.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import JsonValue
from typer.testing import CliRunner

from spectracut.cli.app import app
from spectracut.edit.otio_io import from_otio, to_otio
from spectracut.edit.timeline import FPS_24, Clip, Edit, Track
from spectracut.formats.cutdowns import (
    AspectRender,
    CutDownSpec,
    RenderLibrary,
    cut_down,
    required_renders,
    variant_matrix,
)
from spectracut.formats.linter import ShotLook, lint
from spectracut.formats.safe_areas import Overlay, export_preset
from spectracut.qa.common import QACheck
from spectracut.qa.report import QAReport
from spectracut.spec._base import content_hash
from spectracut.studio.gates import GateMode, GatePolicy
from spectracut.studio.graph import ProductionGraph, RunContext, RunResult
from spectracut.studio.node import NodeSpec, NodeStatus
from spectracut.studio.store import ProductionStore

HOOKS = ("hook-dashboard", "hook-speed", "hook-team")
VISUAL = "product-tour"
CTA = "cta-signup"
SHOTS = (*HOOKS, VISUAL, CTA)
ASPECTS = ("16:9", "9:16")
SECONDS = {"hook-dashboard": 4, "hook-speed": 4, "hook-team": 4, VISUAL: 12, CTA: 6}
LOOKS = {
    "hook-dashboard": ShotLook(scale="cu", lens_mm=85, move="push"),
    "hook-speed": ShotLook(scale="ecu", lens_mm=100, move="static"),
    "hook-team": ShotLook(scale="mcu", lens_mm=50, move="dolly"),
    VISUAL: ShotLook(scale="ws", lens_mm=35, move="orbit"),
    CTA: ShotLook(scale="ms", lens_mm=50, move="static"),
}


def _slug(aspect: str) -> str:
    return aspect.replace(":", "x")


class CountingRenderer:
    """The departments: shots are declared, renders 'rendered' and counted."""

    def __init__(self) -> None:
        self.renders: list[str] = []

    def __call__(self, ctx: RunContext) -> RunResult:
        p = ctx.spec.params
        if ctx.spec.kind == "shot":
            return RunResult(output={"shot_id": p["shot_id"], "seconds": p["seconds"]})
        self.renders.append(ctx.spec.id)
        (shot,) = ctx.inputs.values()
        assert isinstance(shot, dict)
        out: dict[str, JsonValue] = {
            "shot_id": shot["shot_id"],
            "shot_hash": content_hash(shot),
            "aspect": p["aspect"],
            "media_ref": f"renders/{shot['shot_id']}-{_slug(str(p['aspect']))}.exr",
            "frames": 60 * 24,
        }
        return RunResult(output=out)


def _production() -> ProductionGraph:
    graph = ProductionGraph(
        "alterlab-launch", gates=GatePolicy(defaults={"render": GateMode.HUMAN})
    )
    for shot in SHOTS:
        graph.add_node(
            NodeSpec(
                id=f"shot-{shot}",
                kind="shot",
                department="layout",
                params={"shot_id": shot, "seconds": SECONDS[shot]},
            )
        )
        for aspect in ASPECTS:
            graph.add_node(
                NodeSpec(
                    id=f"render-{shot}-{_slug(aspect)}",
                    kind="render",
                    department="lighter",
                    inputs=(f"shot-{shot}",),
                    params={"aspect": aspect},
                )
            )
    return graph


def _library(graph: ProductionGraph) -> RenderLibrary:
    renders = []
    for nid, status in graph.statuses().items():
        if graph.spec(nid).kind != "render":
            continue
        decision = graph.decision(nid)
        approved = (
            status is NodeStatus.CLEAN and decision is not None and decision.human == "approved"
        )
        renders.append(
            AspectRender.model_validate({**_dict(graph.output(nid)), "approved": approved})
        )
    return RenderLibrary(renders=tuple(renders))


def _dict(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def _master(library: RenderLibrary) -> Edit:
    clips = []
    for shot in SHOTS:
        (r,) = [x for x in library.renders if x.shot_id == shot and x.aspect == "16:9"]
        clips.append(
            Clip(
                id=f"m-{shot}",
                shot_id=shot,
                shot_hash=r.shot_hash,
                media_ref=r.media_ref,
                source_out=FPS_24.frames(SECONDS[shot]),
            )
        )
    return Edit(id="launch-film", rate=FPS_24, tracks=[Track(id="v1", items=list(clips))])


def _cut_downs(master: Edit, library: RenderLibrary) -> list[tuple[CutDownSpec, Edit]]:
    specs = [
        CutDownSpec.for_variant(v, length_s=15, aspect="9:16", platform="tiktok")
        for v in variant_matrix(HOOKS, [VISUAL], [CTA])
    ]
    specs.append(
        CutDownSpec(
            id="launch-30s-16x9",
            length_s=30,
            aspect="16:9",
            platform="youtube",
            shots=[HOOKS[0], VISUAL, CTA],
        )
    )
    out = []
    for spec in specs:
        assert required_renders(master, library, spec) == []
        out.append((spec, cut_down(master, library, spec)))
    return out


def test_one_production_yields_three_15s_9x16_ads_and_a_30s_16x9_ad_without_new_renders() -> None:
    graph = _production()
    renderer = CountingRenderer()
    first = graph.run(renderer)
    assert sorted(first.awaiting_approval) == sorted(
        f"render-{s}-{_slug(a)}" for s in SHOTS for a in ASPECTS
    )
    for nid in first.awaiting_approval:
        graph.approve(nid, "approved in review")
    rendered = list(renderer.renders)
    assert len(rendered) == len(SHOTS) * len(ASPECTS)

    library = _library(graph)
    master = _master(library)
    cuts = _cut_downs(master, library)

    nine_sixteen = [e for s, e in cuts if s.aspect == "9:16" and s.length_s == 15]
    sixteen_nine = [e for s, e in cuts if s.aspect == "16:9" and s.length_s == 30]
    assert len(nine_sixteen) == 3 and len(sixteen_nine) == 1
    assert len({e.tracks[0].clips()[0].shot_id for e in nine_sixteen}) == 3
    approved = {(r.media_ref, r.aspect) for r in library.renders if r.approved}
    for spec, edit in cuts:
        assert edit.duration == FPS_24.frames(spec.length_s)
        for clip in edit.tracks[0].clips():
            assert (clip.media_ref, spec.aspect) in approved
        assert from_otio(to_otio(edit)) == edit
        assert lint(edit, LOOKS) == []
        export_preset(
            spec,
            [Overlay(name="cta", x=0.2, y=0.5, w=0.5, h=0.1)],
            qa=QAReport(
                render_id=spec.id,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                checks=[QACheck(name="ok", status="pass")],
                passed=True,
            ),
        )

    again = graph.run(renderer)
    assert not again.did_work
    assert renderer.renders == rendered


E2E = os.environ.get("SPECTRACUT_E2E") == "1" and bool(os.environ.get("SPECTRACUT_FLAMENCO_URL"))


@pytest.mark.e2e
@pytest.mark.skipif(not E2E, reason="set SPECTRACUT_E2E=1 and SPECTRACUT_FLAMENCO_URL")
def test_e2e_cutting_down_a_farm_production_submits_no_new_renders() -> None:
    home = os.environ.get("SPECTRACUT_HOME") or str(Path.home() / ".local/share/spectracut")
    pid = os.environ.get("SPECTRACUT_CUTDOWN_PRODUCTION", "alterlab-dashboard-e2e")
    store = ProductionStore(Path(home))
    assert store.exists(pid), f"run the m1-device-shot-farm-e2e check first ({pid})"
    graph, _ = store.load(pid)
    before = len(graph.ledger)
    renders = [n for n in graph.order() if graph.spec(n).kind.startswith("render")]
    assert renders and all(graph.status(n) is NodeStatus.CLEAN for n in renders)

    film = _production()
    renderer = CountingRenderer()
    for nid in film.run(renderer).awaiting_approval:
        film.approve(nid)
    library = _library(film)
    assert len(_cut_downs(_master(library), library)) == 4

    run = CliRunner().invoke(app, ["studio", "run", pid, "--home", home])
    assert run.exit_code == 0, run.output
    assert "executed" not in run.output, run.output
    reloaded, _ = ProductionStore(Path(home)).load(pid)
    assert len(reloaded.ledger) == before
