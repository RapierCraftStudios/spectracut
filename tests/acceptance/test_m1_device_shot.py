# SPDX-License-Identifier: AGPL-3.0-or-later
"""M1 gate (#153): an AlterLab device shot through the studio, queue and Flamenco.

Unit cases run always, with a fake Flamenco Manager that "renders" by writing
frames and ``report.json`` into the job's output dir, and a mock vision judge.
E2E cases (marked ``e2e``) run on the farm only with ``SPECTRACUT_E2E=1`` and
``SPECTRACUT_FLAMENCO_URL`` set (``SPECTRACUT_HOME=$HOME/.local/share/spectracut``,
Manager http://100.105.62.23:8085, storage /srv/flamenco).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from spectracut.cli import studio as studio_mod
from spectracut.cli.app import app
from spectracut.providers.base import VisionScore
from spectracut.queue.flamenco import WorkerTag, gpu_slug, worker_tags
from spectracut.queue.profile import RenderProfile
from spectracut.queue.signing import (
    generate_keypair,
    load_private_key,
    load_public_key,
    parse_signed_job,
    verify_job,
)
from spectracut.spec.asset import LicenseRecord
from spectracut.spec.brand import BrandKit, load_brand_kit
from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.studio import device_shot as ds
from spectracut.studio.gates import GateMode
from spectracut.studio.node import Tier
from spectracut.studio.profiles import load_profile
from spectracut.studio.store import ProductionStore

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "brands" / "alterlab" / "brand-kit.yaml"
SCREEN = ROOT / "brands" / "alterlab" / "assets" / "ui" / "dashboard.png"
SIDECAR = SCREEN.with_name("dashboard.png.license.json")
# The farm's legion host (registered render profile); its job tag is its GPU slug, the tag
# `spectracut-host tags` prints and `spectracut setup` assigns, never the worker's name.
LEGION = RenderProfile(
    blender_version="4.5.15",
    gpu_model="NVIDIA GeForce RTX 3070 Laptop GPU",
    gpu_driver="581.80",
    vram_gb=8,
    os="windows",
)
TAG = "3070laptopgpu"

runner = CliRunner()


def _brief() -> Brief:
    return Brief(
        id="alterlab-dashboard",
        product="AlterLab dashboard",
        audience=AudienceProfile(personas=[Persona(name="Ops lead", description="runs a lab")]),
        sources=[ClaimSource(id="site", title="AlterLab", ref="https://alterlab.io")],
        proof_points=[ProofPoint(claim="One view of every run", source_id="site")],
        cta="See the dashboard",
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- brand kit, provenance, profile --------------------------------------------


def test_alterlab_brand_kit_loads_and_bad_kits_name_the_file(tmp_path: Path) -> None:
    kit = load_brand_kit(KIT)
    assert isinstance(kit, BrandKit) and kit.id == "alterlab"
    look = next(lk for lk in kit.looks if lk.id == kit.default_look_id)
    assert look.id == "clean" and look.bloom <= 0.05 and look.grain <= 0.05
    assert kit.forbidden_words and kit.claim_sources
    for text, needle in (
        ("- just\n- a list\n", "must be a YAML mapping"),
        ("id: [unclosed\n", "is not valid YAML"),
        ("id: alterlab\n", "is invalid"),
    ):
        bad = tmp_path / "kit.yaml"
        bad.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match=needle) as err:
            load_brand_kit(bad)
        assert str(bad) in str(err.value)
    with pytest.raises(ValueError, match="cannot read brand kit"):
        load_brand_kit(tmp_path / "missing.yaml")


def test_dashboard_is_alterlab_owned_with_recorded_provenance() -> None:
    sidecar = json.loads(SIDECAR.read_text(encoding="utf-8"))
    assert set(sidecar) == {"sha256", "license"}
    record = LicenseRecord.model_validate(sidecar["license"])
    assert record.source == "AlterLab" and record.commercial_safe and not record.non_commercial
    assert "make_alterlab_dashboard.py" in record.attribution
    assert sidecar["sha256"] == _sha(SCREEN)
    check = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "make_alterlab_dashboard.py"),
            "--check",
            str(SCREEN),
        ],
        check=False,
    )
    assert check.returncode == 0


def test_dashboard_generator_check_rejects_other_bytes(tmp_path: Path) -> None:
    other = tmp_path / "other.png"
    other.write_bytes(SCREEN.read_bytes() + b"\0")
    script = str(ROOT / "scripts" / "make_alterlab_dashboard.py")
    proc = subprocess.run(
        [sys.executable, script, "--check", str(other)], check=False, capture_output=True
    )
    assert proc.returncode == 1


def test_device_shot_profile_stages_tiers_and_gates() -> None:
    p = load_profile("device-shot")
    assert p.stages == ("shot", "render_preview", "render_final")
    assert p.tier == {
        "shot": Tier.PREVIEW,
        "render_preview": Tier.PREVIEW,
        "render_final": Tier.FINAL,
    }
    assert all(p.gates[s] is GateMode.AUTO for s in p.stages)
    assert all(p.revision_budget[s] == 0 for s in p.stages)


# ---- recipe and tier steps ----------------------------------------------------------


def test_device_recipe_is_deterministic_and_pins_the_screen() -> None:
    kit, sha = load_brand_kit(KIT), _sha(SCREEN)
    a = ds.build_device_recipe(_brief(), kit, screen_image="ui/dashboard.png", screen_sha256=sha)
    b = ds.build_device_recipe(_brief(), kit, screen_image="ui/dashboard.png", screen_sha256=sha)
    assert a.content_hash == b.content_hash and a == b
    screens = [s for s in a.steps if s.tool == "material.apply_screen"]
    assert len(screens) == 1 and screens[0].args["image_sha256"] == sha
    assert screens[0].args["image"] == "ui/dashboard.png"
    tools = {s.tool for s in a.steps}
    assert {"object.add_primitive", "camera.add", "light.add"} <= tools
    names = {s.args.get("name") for s in a.steps if s.tool == "object.add_primitive"}
    assert names == {"device_body", "device_screen", "plinth"}
    colours: list[Any] = [s.args["color"] for s in a.steps if s.tool == "material.apply_preset"]
    assert [round(c * 255) for c in colours[1]] == [0x0B, 0x12, 0x20]  # body: kit ink
    other = ds.build_device_recipe(
        _brief(), kit, screen_image="ui/dashboard.png", screen_sha256="0" * 64
    )
    assert other.content_hash != a.content_hash


def test_tier_steps_preview_eevee_half_final_seeded_denoised_cycles() -> None:
    preview = [(s.tool, s.args) for s in ds.tier_steps(Tier.PREVIEW)]
    assert ("render.set_engine", {"engine": "eevee"}) in preview
    assert any(t == "render.set_resolution" and a["percentage"] == 50 for t, a in preview)
    final = {(s.tool, str(s.args.get("property"))): s.args for s in ds.tier_steps(Tier.FINAL)}
    assert final[("render.set_engine", "None")] == {"engine": "cycles"}
    assert final[("render.set_property", "cycles.seed")]["value"] == ds.FINAL_SEED
    assert final[("render.set_property", "cycles.use_denoising")]["value"] is True
    assert final[("render.set_property", "cycles.denoiser")]["value"] == "OPENIMAGEDENOISE"
    with pytest.raises(ValueError):
        ds.tier_steps(Tier.HERO)


def test_signed_job_lands_in_shared_jobs_and_verifies(tmp_path: Path) -> None:
    private, public = generate_keypair()
    kit, sha = load_brand_kit(KIT), _sha(SCREEN)
    recipe = ds.with_tier(
        ds.build_device_recipe(_brief(), kit, screen_image="ui/dashboard.png", screen_sha256=sha),
        Tier.FINAL,
    )
    config = ds.DeviceShotConfig(flamenco_url="http://fake", shared=tmp_path, worker_tag=TAG)
    envelope = ds._envelope("job-1", recipe, config, datetime(2026, 11, 5, tzinfo=UTC))
    path = ds.sign_to_shared(envelope, load_private_key(private), tmp_path)
    assert path == tmp_path / "jobs" / "job-1.json"
    text = path.read_text(encoding="utf-8")
    assert private not in text
    assert verify_job(parse_signed_job(text), load_public_key(public)) == envelope


def test_tag_is_the_legion_gpu_slug_setup_assigns_not_a_worker_name() -> None:
    assert gpu_slug(LEGION.gpu_model) == TAG
    assert worker_tags(LEGION)[0] == TAG
    assert TAG not in {"legion", "legion-rtx3070", "mrdubey-rtx4070ti"}


def test_config_from_env_needs_farm_settings() -> None:
    with pytest.raises(LookupError, match="SPECTRACUT_FLAMENCO_URL"):
        ds.DeviceShotConfig.from_env({})
    cfg = ds.DeviceShotConfig.from_env(
        {
            "SPECTRACUT_FLAMENCO_URL": "http://m:8085",
            "SPECTRACUT_FLAMENCO_SHARED": "/srv/flamenco",
            "SPECTRACUT_FLAMENCO_TAG": TAG,
            "SPECTRACUT_ASSETS_DIR": "/srv/flamenco/assets",
        }
    )
    assert cfg.shared == Path("/srv/flamenco") and cfg.assets_dir == "/srv/flamenco/assets"


# ---- studio new / run / status / replay through a fake Flamenco ----------------


class FakeFlamenco:
    """A Manager whose jobs complete at once, writing frames and report.json."""

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []
        self.corrupt_next = False
        self.drop_scene_hash = False

    def list_tags(self) -> list[WorkerTag]:
        return [WorkerTag(id="tag-1", name=TAG)]

    def submit(self, job: dict[str, Any]) -> dict[str, Any]:
        self.submitted.append(job)
        settings = job["settings"]
        signed = json.loads(Path(settings["job_file"]).read_text(encoding="utf-8"))
        recipe_hash = signed["envelope"]["recipe_hash"]
        out = Path(settings["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        start, end = (int(x) for x in settings["frames"].split("-"))
        for frame in range(start, end + 1):
            body = f"{recipe_hash}:{frame}".encode()
            if self.corrupt_next:
                body += b"!"
            (out / f"frame_{frame:04d}.png").write_bytes(body)
        self.corrupt_next = False
        report: dict[str, Any] = {"reports": []}
        if not self.drop_scene_hash:
            report["scene_graph_hash"] = (
                "sha256:" + hashlib.sha256(recipe_hash.encode()).hexdigest()
            )
        (out / "report.json").write_text(json.dumps(report), encoding="utf-8")
        return {"id": f"fl-{len(self.submitted)}"}

    def job_status(self, job_id: str) -> str:
        return "completed"


class MockJudge:
    name = "mock"

    def __init__(self) -> None:
        self.frames: list[Path] = []

    def score(self, frame_path: Path, rubric: str) -> VisionScore:
        self.frames.append(frame_path)
        return VisionScore(score=0.92, notes="screen legible")


class Ticks:
    """A monotonic clock that advances 90 seconds per read."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 90.0
        return self.t


@pytest.fixture
def farm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[FakeFlamenco, MockJudge, Path, str]]:
    private, _ = generate_keypair()
    fake, judge, shared = FakeFlamenco(), MockJudge(), tmp_path / "shared"
    config = ds.DeviceShotConfig(
        flamenco_url="http://fake", shared=shared, worker_tag=TAG, assets_dir=str(tmp_path / "a")
    )

    def factory(production: studio_mod.Production) -> studio_mod.DeviceShotDeps:
        return studio_mod.DeviceShotDeps(
            config=config,
            client=fake,
            key=load_private_key(private),
            judge=judge,
            options=ds.ExecutorOptions(clock=Ticks(), sleep=lambda _s: None, poll_interval=0.0),
        )

    monkeypatch.setattr(studio_mod, "device_shot_factory", factory)
    yield fake, judge, shared, private


def _new_and_run(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    brief = tmp_path / "brief.json"
    brief.write_text(_brief().model_dump_json(), encoding="utf-8")
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
            "--home",
            str(home),
        ],
    )
    assert new.exit_code == 0, new.output
    run = runner.invoke(app, ["studio", "run", "alterlab-dashboard", "--home", str(home)])
    assert run.exit_code == 0, run.output
    assert "executed: shot, render_preview, render_final" in run.output
    return home


def test_device_shot_new_needs_a_brand_kit(tmp_path: Path) -> None:
    brief = tmp_path / "brief.json"
    brief.write_text(_brief().model_dump_json(), encoding="utf-8")
    args = ["studio", "new", str(brief), "--profile", "device-shot", "--home", str(tmp_path)]
    result = runner.invoke(app, args)
    assert result.exit_code == 2 and "--brand-kit" in result.output
    bad = tmp_path / "kit.yaml"
    bad.write_text("id: x\n", encoding="utf-8")
    result = runner.invoke(app, [*args, "--brand-kit", str(bad)])
    assert result.exit_code == 2 and "is invalid" in result.output


def test_studio_run_renders_preview_and_final_and_status_shows_ledger(
    tmp_path: Path, farm: tuple[FakeFlamenco, MockJudge, Path, str]
) -> None:
    fake, judge, shared, private = farm
    home = _new_and_run(tmp_path)
    graph, _ = ProductionStore(home).load("alterlab-dashboard")
    shot: Any = graph.output("shot")
    assert isinstance(shot, dict)
    screen = [s for s in shot["recipe"]["steps"] if s["tool"] == "material.apply_screen"]
    assert screen[0]["args"]["image_sha256"] == _sha(SCREEN)
    assert len(fake.submitted) == 2
    for node, engine in (("render_preview", "eevee"), ("render_final", "cycles")):
        out = graph.output(node)
        assert isinstance(out, dict)
        assert str(out["scene_graph_hash"]).startswith("sha256:")
        frames = out["frames"]
        assert isinstance(frames, dict) and set(frames) == {"frame_0001.png"}
        frame = Path(str(out["output_dir"])) / "frame_0001.png"
        assert frames["frame_0001.png"] == _sha(frame)
        assert Path(str(out["output_dir"])).parent == shared / "out"
        assert out["worker_tag"] == TAG
        job_file = shared / "jobs" / f"{out['job_id']}.json"
        steps = json.loads(job_file.read_text())["envelope"]["recipe"]["steps"]
        assert {"tool": "render.set_engine", "args": {"engine": engine}} in [
            {"tool": s["tool"], "args": s["args"]} for s in steps
        ]
    assert fake.submitted[0]["settings"]["assets_dir"] == str(tmp_path / "a")
    preview = graph.output("render_preview")
    assert isinstance(preview, dict) and preview["vision_qa"] == {
        "score": 0.92,
        "notes": "screen legible",
    }
    assert len(judge.frames) == 1
    status = runner.invoke(app, ["studio", "status", "alterlab-dashboard", "--home", str(home)])
    assert status.exit_code == 0, status.output
    minutes = re.search(r"gpu_minutes=([0-9.]+)", status.output)
    assert minutes is not None and float(minutes.group(1)) > 0
    assert "vision_qa_requests=1" in status.output
    assert private not in status.output
    for path in (home / "productions").rglob("*"):
        if path.is_file():
            assert private not in path.read_text(encoding="utf-8", errors="replace")


def test_studio_replay_matches_then_detects_changes(
    tmp_path: Path, farm: tuple[FakeFlamenco, MockJudge, Path, str]
) -> None:
    fake, _judge, shared, _ = farm
    home = _new_and_run(tmp_path)
    graph_before = (home / "productions" / "alterlab-dashboard" / "graph.json").read_bytes()
    recorded = ProductionStore(home).load("alterlab-dashboard")[0].output("render_final")
    assert isinstance(recorded, dict)
    replay = ["studio", "replay", "alterlab-dashboard", "render_final", "--home", str(home)]

    ok = runner.invoke(app, replay)
    assert ok.exit_code == 0, ok.output
    assert "match" in ok.output and str(recorded["scene_graph_hash"]) in ok.output
    assert len(fake.submitted) == 3  # resubmitted although the graph cache holds the node
    first = shared / "out" / f"{recorded['job_id']}-replay-1"
    assert first.is_dir() and f"-> {first}" in ok.output

    fake.corrupt_next = True
    changed = runner.invoke(app, replay)
    assert changed.exit_code == 1 and "frame_0001.png" in changed.output
    assert (shared / "out" / f"{recorded['job_id']}-replay-2").is_dir()

    fake.drop_scene_hash = True
    no_hash = runner.invoke(app, replay)
    assert no_hash.exit_code == 1 and "scene_graph_hash" in no_hash.output
    assert len(fake.submitted) == 5
    # replay never rewrites the production
    assert (home / "productions" / "alterlab-dashboard" / "graph.json").read_bytes() == graph_before


def test_replay_reports_missing_and_extra_frames_as_mismatch() -> None:
    base = dict(
        node_id="n",
        original_job_id="j",
        job_id="j-replay-1",
        output_dir=Path("/x"),
        original_scene_graph_hash="sha256:a",
        scene_graph_hash="sha256:a",
    )
    same = ds.ReplayResult(**base, original_frames={"f1": "h"}, frames={"f1": "h"})  # type: ignore[arg-type]
    assert same.matches
    extra = ds.ReplayResult(**base, original_frames={"f1": "h"}, frames={"f1": "h", "f2": "h"})  # type: ignore[arg-type]
    missing = ds.ReplayResult(**base, original_frames={"f1": "h", "f2": "h"}, frames={"f1": "h"})  # type: ignore[arg-type]
    assert not extra.matches and extra.mismatches() == ["f2"]
    assert not missing.matches and missing.mismatches() == ["f2"]


def test_studio_replay_exit_2_for_unknown_production_node_or_unbuilt_node(
    tmp_path: Path, farm: tuple[FakeFlamenco, MockJudge, Path, str]
) -> None:
    home = tmp_path / "home"
    missing = runner.invoke(app, ["studio", "replay", "nope", "render_final", "--home", str(home)])
    assert missing.exit_code == 2
    brief = tmp_path / "brief.json"
    brief.write_text(_brief().model_dump_json(), encoding="utf-8")
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
            "--home",
            str(home),
        ],
    )
    assert new.exit_code == 0, new.output
    for node in ("render_final", "ghost"):
        result = runner.invoke(
            app, ["studio", "replay", "alterlab-dashboard", node, "--home", str(home)]
        )
        assert result.exit_code == 2, result.output


# ---- E2E on the farm ----------------------------------------------------------------

E2E = os.environ.get("SPECTRACUT_E2E") == "1" and bool(os.environ.get("SPECTRACUT_FLAMENCO_URL"))


@pytest.mark.e2e
@pytest.mark.skipif(not E2E, reason="set SPECTRACUT_E2E=1 and SPECTRACUT_FLAMENCO_URL")
def test_e2e_device_shot_renders_on_the_farm_and_replays_identically(tmp_path: Path) -> None:
    home = os.environ.get("SPECTRACUT_HOME") or str(Path.home() / ".local/share/spectracut")
    brief = tmp_path / "brief.json"
    brief.write_text(_brief().model_dump_json(), encoding="utf-8")
    pid = "alterlab-dashboard-e2e"
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
            "--home",
            home,
            "--force",
        ],
    )
    assert new.exit_code == 0, new.output
    run = runner.invoke(app, ["studio", "run", pid, "--home", home])
    print(run.output)
    assert run.exit_code == 0, run.output
    graph, _ = ProductionStore(Path(home)).load(pid)
    for node in ("render_preview", "render_final"):
        out = graph.output(node)
        assert isinstance(out, dict) and out["frames"]
        print(node, out["scene_graph_hash"], json.dumps(out["frames"], sort_keys=True))
    status = runner.invoke(app, ["studio", "status", pid, "--home", home])
    print(status.output)
    assert "vision_qa_requests=1" in status.output
    minutes = re.search(r"gpu_minutes=([0-9.]+)", status.output)
    assert minutes is not None and float(minutes.group(1)) > 0
    replay = runner.invoke(app, ["studio", "replay", pid, "render_final", "--home", home])
    print(replay.output)
    assert replay.exit_code == 0, replay.output
