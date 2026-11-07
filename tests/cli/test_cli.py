# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``spectracut`` CLI over the studio: studio, render, status, hosts, doctor, serve, make, mcp."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from spectracut.cli import doctor as doctor_mod
from spectracut.cli import studio as studio_mod
from spectracut.cli.app import app
from spectracut.cli.render import load_hosts
from spectracut.queue.cache import FrameCache
from spectracut.queue.executors.base import JobResult
from spectracut.queue.profile import RenderProfile
from spectracut.queue.signing import (
    SIGNING_KEY_ENV,
    generate_keypair,
    load_public_key,
    parse_signed_job,
    verify_job,
)
from spectracut.spec.brief import (
    AudienceProfile,
    Brief,
    ClaimSource,
    CreativeBrief,
    Persona,
    ProofPoint,
)
from spectracut.spec.recipe import Recipe, RecipeStep
from spectracut.studio import (
    NodeStatus,
    ProductionGraph,
    RunContext,
    RunResult,
    Usage,
    load_profile,
)
from spectracut.studio.departments import department_for
from spectracut.studio.estimate import breakdown
from spectracut.studio.graph import GraphSnapshot
from spectracut.studio.store import ProductionStore

runner = CliRunner()
PROFILE = RenderProfile(blender_version="4.5.3", gpu_model="NVIDIA GeForce RTX 4070 Ti", vram_gb=12)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    monkeypatch.setenv("SPECTRACUT_HOME", str(root))
    monkeypatch.setenv("SPECTRACUT_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)
    return root


def _brief() -> Brief:
    return Brief(
        id="spellwise",
        product="Spellwise",
        audience=AudienceProfile(personas=[Persona(name="Dev", description="ships often")]),
        sources=[ClaimSource(id="docs", title="Docs", ref="https://example.test/docs")],
        proof_points=[ProofPoint(claim="Fast spellcheck", source_id="docs")],
        cta="Try it",
    )


@pytest.fixture
def brief_file(tmp_path: Path) -> Path:
    path = tmp_path / "brief.json"
    path.write_text(_brief().model_dump_json(), encoding="utf-8")
    return path


def _fake_runner(ctx: RunContext) -> RunResult:
    return RunResult(
        output={"node": ctx.spec.id, "inputs": sorted(ctx.inputs)},
        usage=Usage(ai_usage={"claude_requests": 1}),
    )


# ---- (a) studio new ---------------------------------------------------------


def test_studio_new_creates_production_and_prints_estimate(home: Path, brief_file: Path) -> None:
    result = runner.invoke(app, ["studio", "new", str(brief_file), "--profile", "standard"])
    assert result.exit_code == 0, result.output
    assert "production spellwise created" in result.output
    for stage in load_profile("standard").stages:
        assert f"  {stage}: department={department_for(stage)}" in result.output
    assert "human gates=5" in result.output
    assert (home / "productions" / "spellwise" / "graph.json").is_file()
    again = runner.invoke(app, ["studio", "new", str(brief_file)])
    assert again.exit_code == 2 and "already exists" in again.output


def test_studio_new_rejects_bad_brief_and_profile(
    home: Path, tmp_path: Path, brief_file: Path
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"id": "x"}', encoding="utf-8")
    result = runner.invoke(app, ["studio", "new", str(bad)])
    assert result.exit_code == 2 and "error:" in result.output
    missing = runner.invoke(app, ["studio", "new", str(tmp_path / "nope.json")])
    assert missing.exit_code == 2 and "error:" in missing.output
    unknown = runner.invoke(app, ["studio", "new", str(brief_file), "--profile", "nope"])
    assert unknown.exit_code == 2 and "error:" in unknown.output
    assert not (home / "productions" / "spellwise").exists()


# ---- (b) studio run / approve / status ---------------------------------------


def test_studio_run_approve_status_across_invocations(
    home: Path, brief_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(studio_mod, "runner_factory", lambda production, graph: _fake_runner)
    assert runner.invoke(app, ["studio", "new", str(brief_file)]).exit_code == 0

    first = runner.invoke(app, ["studio", "run", "spellwise"])
    assert first.exit_code == 0, first.output
    assert "awaiting approval: brief" in first.output

    approved = runner.invoke(app, ["studio", "approve", "spellwise", "brief", "--comment", "ok"])
    assert approved.exit_code == 0, approved.output
    assert "brief: clean" in approved.output

    status = runner.invoke(app, ["studio", "status", "spellwise"])
    assert status.exit_code == 0, status.output
    assert "  brief: clean tier=preview gate=human human=approved" in status.output
    assert "ledger: runs=1" in status.output

    twice = runner.invoke(app, ["studio", "approve", "spellwise", "brief"])
    assert twice.exit_code == 2 and "error:" in twice.output
    unknown = runner.invoke(app, ["studio", "approve", "spellwise", "nope"])
    assert unknown.exit_code == 2 and "error:" in unknown.output

    second = runner.invoke(app, ["studio", "run", "spellwise"])
    assert second.exit_code == 0, second.output
    assert "awaiting approval: concept" in second.output
    rejected = runner.invoke(app, ["studio", "reject", "spellwise", "concept"])
    assert rejected.exit_code == 0 and "concept: rejected" in rejected.output


def test_studio_run_without_departments_fails_clearly(
    home: Path, brief_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(studio_mod, "DEPARTMENTS", {})
    assert runner.invoke(app, ["studio", "new", str(brief_file)]).exit_code == 0
    result = runner.invoke(app, ["studio", "run", "spellwise"])
    assert result.exit_code == 1
    assert "failed: brief" in result.output
    assert "no department registered for 'strategist'" in result.output


def test_studio_run_on_an_old_layout_graph_asks_to_recreate_it(
    home: Path, brief_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(studio_mod, "runner_factory", lambda production, graph: _fake_runner)
    assert runner.invoke(app, ["studio", "new", str(brief_file)]).exit_code == 0
    store = ProductionStore(home)
    graph, _ = store.load("spellwise")
    stale = graph.spec("style_frames").model_copy(update={"inputs": ("script",)})
    graph.update_node(stale)  # the positional chain productions were created with
    store.save("spellwise", graph)

    result = runner.invoke(app, ["studio", "run", "spellwise"])
    assert result.exit_code == 2, result.output
    assert "older production graph" in result.output
    assert "recreate it with" in result.output and "--force" in result.output
    assert "undeclared kind" not in result.output
    reloaded, _ = store.load("spellwise")
    assert all(s is NodeStatus.PENDING for s in reloaded.statuses().values())


def test_studio_new_duration_prints_gpu_hours_and_wall_time(home: Path, brief_file: Path) -> None:
    result = runner.invoke(app, ["studio", "new", str(brief_file), "--duration", "60"])
    assert result.exit_code == 0, result.output
    assert "60 s, 15 shots" in result.output
    total = next(line for line in result.output.splitlines() if line.startswith("total:"))
    assert "gpu_hours=" in total and "wall_minutes=" in total and "human gates=5" in total


def _voice_runner(ctx: RunContext) -> RunResult:
    chars = 900 if ctx.spec.kind == "voiceover" else 0
    return RunResult(
        output={"node": ctx.spec.id},
        usage=Usage(ai_usage={"claude_requests": 1}, elevenlabs_characters=chars),
    )


def test_studio_budget_cap_holds_node_until_approve_budget(
    home: Path, brief_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(studio_mod, "runner_factory", lambda production, graph: _voice_runner)
    created = runner.invoke(
        app, ["studio", "new", str(brief_file), "--cap-elevenlabs-characters", "1000"]
    )
    assert created.exit_code == 0, created.output
    store = ProductionStore(home)
    held = None
    for _ in range(20):
        result = runner.invoke(app, ["studio", "run", "spellwise"])
        assert result.exit_code == 0, result.output
        if "needs approval:" in result.output:
            held = result.output
            break
        graph, _ = store.load("spellwise")
        for nid in graph.order():
            if graph.status(nid) is NodeStatus.AWAITING_APPROVAL:
                assert runner.invoke(app, ["studio", "approve", "spellwise", nid]).exit_code == 0
    assert held is not None
    assert "needs approval: voiceover elevenlabs_characters projected=" in held
    assert "limit=1000" in held
    graph, _ = store.load("spellwise")
    assert graph.status("voiceover") is NodeStatus.PENDING
    assert graph.ledger.totals().elevenlabs_characters == 0

    approved = runner.invoke(app, ["studio", "approve-budget", "spellwise", "voiceover"])
    assert approved.exit_code == 0 and "voiceover: budget approved" in approved.output
    unknown = runner.invoke(app, ["studio", "approve-budget", "spellwise", "nope"])
    assert unknown.exit_code == 2 and "error:" in unknown.output
    again = runner.invoke(app, ["studio", "run", "spellwise"])
    assert again.exit_code == 0, again.output
    assert "voiceover" in again.output.split("executed:")[1].splitlines()[0]
    assert "needs approval" not in again.output


# ---- (c, d) doctor ----------------------------------------------------------


def test_doctor_all_off_without_config(home: Path) -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[:3] == ["tokenbaton: off", "computeshare: off", "forgedock: off"]
    assert "components:" in lines
    assert "  blender 4.5.15: not installed" in lines


def test_doctor_on_and_healthy_with_probes(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.toml").write_text(
        "[integrations.tokenbaton]\nenabled = true\n"
        '[integrations.computeshare]\nenabled = true\nrouter_url = "http://router.test"\n'
        "[integrations.forgedock]\nenabled = false\n",
        encoding="utf-8",
    )
    seen: dict[str, Mapping[str, Any]] = {}

    def failing(cfg: Mapping[str, Any]) -> tuple[bool, str]:
        return False, "token sk-ant-abcdefghijklmnopqrstuvwxyz0123456789 rejected"

    def passing(cfg: Mapping[str, Any]) -> tuple[bool, str]:
        seen["computeshare"] = cfg
        return True, ""

    monkeypatch.setitem(doctor_mod.DEFAULT_PROBES, "tokenbaton", failing)
    monkeypatch.setitem(doctor_mod.DEFAULT_PROBES, "computeshare", passing)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("tokenbaton: on (unhealthy: token ")
    assert "sk-ant-abcdefghij" not in result.output
    assert "computeshare: healthy" in result.output
    assert "forgedock: off" in result.output
    assert seen["computeshare"]["router_url"] == "http://router.test"

    as_json = runner.invoke(app, ["doctor", "--json"])
    assert as_json.exit_code == 0, as_json.output
    data = json.loads(as_json.output)
    states = {i["name"]: i["state"] for i in data["integrations"]}
    assert states == {"tokenbaton": "on", "computeshare": "healthy", "forgedock": "off"}
    assert data["components"][0] == "tested set 2026.11"
    assert "blender 4.5.15: not installed" in data["components"]


def test_integration_reports_probe_crash_is_unhealthy() -> None:
    def boom(cfg: Mapping[str, Any]) -> tuple[bool, str]:
        raise RuntimeError("kaput")

    reports = doctor_mod.integration_reports(
        {"integrations": {"forgedock": {"enabled": True}}}, {"forgedock": boom}
    )
    assert [(r.name, r.state) for r in reports] == [
        ("tokenbaton", "off"),
        ("computeshare", "off"),
        ("forgedock", "on"),
    ]
    assert "kaput" in reports[2].detail


def test_computeshare_probe_needs_router_url() -> None:
    assert doctor_mod.probe_computeshare({}) == (False, "router_url is not set")


# ---- (e) hosts ----------------------------------------------------------------


def test_hosts_add_list_remove_round_trip(home: Path, tmp_path: Path) -> None:
    profile_file = tmp_path / "profile.json"
    profile_file.write_text(PROFILE.model_dump_json(), encoding="utf-8")
    add = [
        "hosts", "add", "--name", "legion", "--cpus", "8", "--mem-gb", "32",
        "--gpu", "NVIDIA GeForce RTX 4070 Ti:12", "--profile-file", str(profile_file),
    ]  # fmt: skip
    assert runner.invoke(app, add).exit_code == 0
    dup = runner.invoke(app, add)
    assert dup.exit_code == 2 and "already registered" in dup.output

    listed = runner.invoke(app, ["hosts", "list"])
    assert listed.exit_code == 0
    assert "legion: cpus=8 mem_gb=32 gpus=[NVIDIA GeForce RTX 4070 Ti 12 GB]" in listed.output

    (slot,) = load_hosts(home)
    assert (slot.name, slot.cpus, slot.mem_gb) == ("legion", 8, 32)
    assert slot.gpus[0].vram_gb == 12 and slot.profile == PROFILE

    assert runner.invoke(app, ["hosts", "remove", "legion"]).exit_code == 0
    assert load_hosts(home) == ()
    assert runner.invoke(app, ["hosts", "remove", "legion"]).exit_code == 2


# ---- (f) render and status ---------------------------------------------------


def _recipe_files(tmp_path: Path) -> tuple[Recipe, Path, Path]:
    steps = [
        RecipeStep(index=0, tool="scene.clear", args={}),
        RecipeStep(index=1, tool="render.set_engine", args={"engine": "eevee"}),
    ]
    recipe = Recipe(id="r1", shot_id="shot-1", blender_version="4.5", steps=steps)
    recipe_file = tmp_path / "recipe.json"
    recipe_file.write_text(recipe.model_dump_json(), encoding="utf-8")
    profile_file = tmp_path / "render-profile.json"
    profile_file.write_text(PROFILE.model_dump_json(), encoding="utf-8")
    return recipe, recipe_file, profile_file


def test_render_cache_hit_writes_no_job(home: Path, tmp_path: Path) -> None:
    recipe, recipe_file, profile_file = _recipe_files(tmp_path)
    FrameCache(home / "cache").put(recipe.content_hash, PROFILE, JobResult(output={"f": 1}))
    result = runner.invoke(app, ["render", str(recipe_file), "--profile", str(profile_file)])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("cached sha256:")
    assert not (home / "jobs").exists()


def test_render_miss_writes_signed_job(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe, recipe_file, profile_file = _recipe_files(tmp_path)
    args = ["render", str(recipe_file), "--profile", str(profile_file), "--frames", "1-3"]

    no_key = runner.invoke(app, args)
    assert no_key.exit_code == 2 and "error:" in no_key.output
    assert not (home / "jobs").exists()

    private, public = generate_keypair()
    monkeypatch.setenv(SIGNING_KEY_ENV, private)
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert private not in result.output
    (job_file,) = (home / "jobs").glob("*.json")
    env = verify_job(parse_signed_job(job_file.read_bytes()), load_public_key(public))
    assert env.recipe_hash == recipe.content_hash
    assert (env.frame_start, env.frame_end) == (1, 3)
    assert private not in job_file.read_text(encoding="utf-8")

    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0, status.output
    assert f"{env.job_id}: queued" in status.output
    (home / "jobs" / f"{env.job_id}.result.json").write_text("{}", encoding="utf-8")
    assert f"{env.job_id}: done" in runner.invoke(app, ["status"]).output


def test_status_lists_productions(home: Path, brief_file: Path) -> None:
    assert runner.invoke(app, ["studio", "new", str(brief_file)]).exit_code == 0
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "productions: 1" in result.output
    assert "spellwise: profile=standard pending=15" in result.output
    assert "jobs: 0" in result.output


# ---- (g) graph snapshot --------------------------------------------------------


def test_graph_snapshot_round_trip(tmp_path: Path) -> None:
    profile = load_profile("standard")
    graph = ProductionGraph("spellwise", profile=profile)
    graph.add_nodes(breakdown(_brief(), profile))
    graph.run(_fake_runner)
    graph.approve("brief", "ok")
    graph.run(_fake_runner)
    assert graph.status("concept") is NodeStatus.AWAITING_APPROVAL

    snap = GraphSnapshot.model_validate_json(graph.snapshot().model_dump_json())
    restored = ProductionGraph.from_snapshot(snap)
    assert restored.statuses() == graph.statuses()
    assert restored.output("brief") == graph.output("brief")
    assert restored.decision("brief") == graph.decision("brief")
    assert restored.ledger.totals() == graph.ledger.totals()

    restored.approve("concept")
    report = restored.run(_fake_runner)
    assert "script" in report.executed

    store = ProductionStore(tmp_path)
    store.create("spellwise", _brief(), graph)
    loaded, meta = store.load("spellwise")
    assert loaded.statuses() == graph.statuses() and meta.profile == "standard"
    assert store.list() == ["spellwise"]


def test_mcp_command_is_listed_and_documented() -> None:
    top = runner.invoke(app, ["--help"])
    assert top.exit_code == 0 and "mcp" in top.output
    result = runner.invoke(app, ["mcp", "--help"])
    assert result.exit_code == 0
    assert "MCP over stdio" in result.output


def test_studio_new_idea_headless_writes_brief_and_assumptions(home: Path, tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(
        "# AlterLab\n\nAlterLab renders product films from a short idea with agents.\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "studio",
            "new",
            "--idea",
            "launch film for AlterLab",
            "--source",
            str(readme),
            "--headless",
        ],
    )
    assert result.exit_code == 0, result.output
    prod = home / "productions" / "alterlab-launch"
    assert CreativeBrief.model_validate_json((prod / "brief.json").read_bytes()).assumptions
    assert "**tone**" in (prod / "assumptions.md").read_text(encoding="utf-8")
    both = runner.invoke(app, ["studio", "new", "x.json", "--idea", "i"])
    assert both.exit_code == 2 and "exactly one" in both.output
