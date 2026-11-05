# SPDX-License-Identifier: AGPL-3.0-or-later
"""Looks (#54): the preset library, kit resolution, render rules vs finishing, render reuse.

The reuse case runs the ``device-shot-graded`` graph through a fake Flamenco
Manager; the same assertion on a real farm is ``test_looks_e2e.py``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from spectracut.cli import studio as studio_mod
from spectracut.finishing.color import ColorPipeline
from spectracut.finishing.looks import (
    finish_params,
    finish_steps,
    load_look,
    load_looks,
    render_rules,
    resolve_look,
)
from spectracut.providers.base import VisionScore
from spectracut.queue.flamenco import WorkerTag
from spectracut.queue.signing import generate_keypair, load_private_key
from spectracut.spec.brand import BrandKit, Look, load_brand_kit
from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.studio import device_shot as ds
from spectracut.studio.estimate import breakdown
from spectracut.studio.graph import ProductionGraph
from spectracut.studio.node import NodeStatus, Tier

ROOT = Path(__file__).resolve().parents[2]
LOOKS = ROOT / "looks"
KIT = ROOT / "brands" / "alterlab" / "brand-kit.yaml"
TAG = "3070laptopgpu"


@pytest.fixture(autouse=True)
def _qa_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Render QA reports go to SPECTRACUT_HOME; keep them out of the real home."""
    home = tmp_path / "qa-home"
    monkeypatch.setenv("SPECTRACUT_HOME", str(home))
    return home


# ---- the library -------------------------------------------------------------------


def test_library_loads_and_every_look_declares_its_thresholds() -> None:
    looks = load_looks(LOOKS)
    assert set(looks) == {"neutral", "clean", "cinematic"}
    for path in LOOKS.glob("*.yaml"):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(raw["qa_thresholds"]) == {"grain_max", "haze_max", "bloom_max"}, path
        assert isinstance(raw["version"], int)
    neutral, cinematic = looks["neutral"], looks["cinematic"]
    assert neutral.grade.cdl is not None and neutral.grade.cdl.slope == (1.0, 1.0, 1.0)
    assert (neutral.bloom, neutral.grain, neutral.lens.vignette) == (0.0, 0.0, 0.0)
    assert cinematic.bloom == 0.08 and cinematic.grain == 0.06
    lens = cinematic.lens
    assert lens.chromatic_aberration > 0 and lens.vignette > 0 and lens.halation > 0
    assert cinematic.qa_thresholds.grain_max >= cinematic.grain
    assert cinematic.qa_thresholds.grain_max > looks["clean"].qa_thresholds.grain_max


def test_clean_look_matches_the_alterlab_kit() -> None:
    kit = load_brand_kit(KIT)
    assert load_looks(LOOKS)["clean"] == resolve_look(kit)


@pytest.mark.parametrize(
    ("name", "text", "needle"),
    [
        ("a.yaml", "- a list\n", "must be a YAML mapping"),
        ("a.yaml", "id: [unclosed\n", "is not valid YAML"),
        ("a.yaml", "id: a\nbloom: 3\n", "is invalid"),
        ("a.yaml", "id: b\n", "the file must be b.yaml"),
    ],
)
def test_bad_looks_name_the_file(tmp_path: Path, name: str, text: str, needle: str) -> None:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=needle) as err:
        load_look(path)
    assert str(path) in str(err.value)
    with pytest.raises(ValueError, match="cannot read look"):
        load_look(tmp_path / "missing.yaml")
    with pytest.raises(ValueError, match="does not exist"):
        load_looks(tmp_path / "nope")


def test_kits_pick_looks() -> None:
    kit = _kit()
    assert resolve_look(kit).id == "clean"
    assert resolve_look(kit, "cinematic").id == "cinematic"
    with pytest.raises(ValueError, match="no look 'noir'; its looks are: cinematic, clean"):
        resolve_look(kit, "noir")


# ---- render rules vs finishing -----------------------------------------------------


def test_render_rules_carry_motion_blur_and_dof_only() -> None:
    clean = load_looks(LOOKS)["clean"]
    steps = render_rules(clean, start=3)
    assert [(s.index, s.tool, s.args) for s in steps] == [
        (3, "render.set_property", {"property": "render.use_motion_blur", "value": False}),
        (4, "camera.set_dof", {"enabled": False}),
    ]
    free = Look(id="free", motion_blur=True, depth_of_field=True)
    assert [s.args for s in render_rules(free)] == [
        {"property": "render.use_motion_blur", "value": True}
    ]


def test_finish_params_hold_finishing_only() -> None:
    cinematic = load_looks(LOOKS)["cinematic"]
    params = finish_params(cinematic)
    assert set(params) == {"look_id", "look_version", "grade", "lens", "bloom", "grain"}
    assert params["grade"] == {
        "cdl": {
            "slope": [1.06, 1.0, 0.92],
            "offset": [0.004, 0.0, -0.006],
            "power": [0.98, 1.0, 1.03],
            "saturation": 1.08,
        }
    }
    # render rules and thresholds never key finishing
    blurry = cinematic.model_copy(
        update={"motion_blur": True, "qa_thresholds": Look(id="x").qa_thresholds}
    )
    assert finish_params(blurry) == params


def test_finish_steps_write_an_8bit_master_through_the_pipeline() -> None:
    clean = load_looks(LOOKS)["clean"]
    steps = finish_steps(clean, ColorPipeline(), ["frames/frame_0001.exr"], start=2)
    assert [s.index for s in steps] == list(range(2, 2 + len(steps)))
    assert steps[0].tool == "render.set_output" and steps[0].args == {
        "format": "PNG",
        "color_depth": "8",
    }
    assert [s.tool for s in steps[1:4]] == ["render.set_property"] * 3
    last = steps[-1]
    assert last.tool == "finish.apply_look"
    assert last.args == {**finish_params(clean), "frames": ["frames/frame_0001.exr"]}
    lut_look = Look(id="lutty", grade={"lut": "warm.cube"})
    with pytest.raises(ValueError, match="pass its staged file"):
        finish_steps(lut_look, ColorPipeline(), ["frames/frame_0001.exr"])
    pinned = {"path": "lut/warm.cube", "sha256": "ab" * 32}
    with_lut = finish_steps(lut_look, ColorPipeline(), ["f.exr"], lut=pinned)[-1]
    assert with_lut.args["lut"] == pinned and with_lut.args["grade"] == {"lut": "warm.cube"}
    with pytest.raises(ValueError, match="at least one input frame"):
        finish_steps(clean, ColorPipeline(), [])


def test_graded_profile_stages_and_scene_render_recipe() -> None:
    p = ds.graded_profile()
    assert p.stages == ("shot", "render_preview", "render_scene", "finish")
    assert p.tier == {
        "shot": Tier.PREVIEW,
        "render_preview": Tier.PREVIEW,
        "render_scene": Tier.FINAL,
        "finish": Tier.FINAL,
    }
    assert set(p.revision_budget.values()) == {0}
    base = ds.build_device_recipe(
        _brief(), _kit(), screen_image="ui/dashboard.png", screen_sha256="0" * 64
    )
    recipe = ds.scene_render_recipe(base, load_looks(LOOKS)["clean"], ColorPipeline())
    tail = [(s.tool, s.args) for s in recipe.steps[len(base.steps) :]]
    assert tail[: len(ds.tier_steps(Tier.FINAL))] == [
        (s.tool, s.args) for s in ds.tier_steps(Tier.FINAL)
    ]
    assert ("render.set_property", {"property": "render.use_motion_blur", "value": False}) in tail
    assert tail[-1] == ("render.set_output", {"format": "OPEN_EXR", "color_depth": "16"})
    assert [s.index for s in recipe.steps] == list(range(len(recipe.steps)))


# ---- the same shot under two Looks reuses the render -------------------------------


def _brief() -> Brief:
    return Brief(
        id="alterlab-dashboard",
        product="AlterLab dashboard",
        audience=AudienceProfile(personas=[Persona(name="Ops lead", description="runs a lab")]),
        sources=[ClaimSource(id="site", title="AlterLab", ref="https://alterlab.io")],
        proof_points=[ProofPoint(claim="One view of every run", source_id="site")],
        cta="See the dashboard",
    )


def _kit(*extra: Look) -> BrandKit:
    kit = load_brand_kit(KIT)
    looks = [*kit.looks, load_looks(LOOKS)["cinematic"], *extra]
    return BrandKit.model_validate(
        {**kit.model_dump(mode="json"), "looks": [lk.model_dump(mode="json") for lk in looks]}
    )


class FakeFlamenco:
    """Completes jobs at once: EXR frames for an OPEN_EXR recipe, PNG otherwise."""

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    def list_tags(self) -> list[WorkerTag]:
        return [WorkerTag(id="tag-1", name=TAG)]

    def submit(self, job: dict[str, Any]) -> dict[str, Any]:
        self.submitted.append(job)
        settings = job["settings"]
        envelope = json.loads(Path(settings["job_file"]).read_text(encoding="utf-8"))["envelope"]
        exr = any(
            s["tool"] == "render.set_output" and s["args"]["format"] == "OPEN_EXR"
            for s in envelope["recipe"]["steps"]
        )
        out = Path(settings["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        start, end = (int(x) for x in settings["frames"].split("-"))
        for frame in range(start, end + 1):
            body = f"{envelope['recipe_hash']}:{frame}".encode()
            (out / f"frame_{frame:04d}.{'exr' if exr else 'png'}").write_bytes(body)
        digest = hashlib.sha256(envelope["recipe_hash"].encode()).hexdigest()
        (out / "report.json").write_text(
            json.dumps({"reports": [], "scene_graph_hash": f"sha256:{digest}"}), encoding="utf-8"
        )
        return {"id": f"fl-{len(self.submitted)}"}

    def job_status(self, job_id: str) -> str:
        return "completed"

    def recipe(self, index: int) -> dict[str, Any]:
        job_file = Path(self.submitted[index]["settings"]["job_file"])
        recipe: dict[str, Any] = json.loads(job_file.read_text(encoding="utf-8"))["envelope"][
            "recipe"
        ]
        return recipe


class Judge:
    name = "mock"

    def score(self, frame_path: Path, rubric: str) -> VisionScore:
        return VisionScore(score=0.9, notes="ok")


def _graded_graph(kit: BrandKit) -> ProductionGraph:
    graph = ProductionGraph("graded", profile=ds.graded_profile())
    shot_params = {
        **studio_mod._device_shot_params(KIT),
        ds.BRAND_KIT_PARAM: kit.model_dump(mode="json"),
    }
    pick: dict[str, Any] = {
        ds.BRAND_KIT_PARAM: kit.model_dump(mode="json"),
        ds.LOOK_ID_PARAM: "clean",
    }
    specs = []
    for spec in breakdown(_brief(), graph.profile):  # type: ignore[arg-type]
        extra = shot_params if spec.kind == ds.SHOT_STAGE else {}
        if spec.kind in (ds.RENDER_SCENE_STAGE, ds.FINISH_STAGE):
            extra = pick
        specs.append(spec.model_copy(update={"params": {**spec.params, **extra}}))
    graph.add_nodes(specs)
    return graph


def _runner(graph: ProductionGraph, fake: FakeFlamenco, tmp_path: Path) -> Any:
    private, _ = generate_keypair()
    config = ds.DeviceShotConfig(
        flamenco_url="http://fake", shared=tmp_path / "shared", worker_tag=TAG
    )
    options = ds.ExecutorOptions(sleep=lambda _s: None, poll_interval=0.0)
    return ds.device_shot_runner(
        graph, config, client=fake, judge=Judge(), key=load_private_key(private), options=options
    )


def _set_look(graph: ProductionGraph, look_id: str) -> frozenset[str]:
    params = {**graph.spec("finish").params, ds.LOOK_ID_PARAM: look_id}
    return graph.set_params("finish", params)


def test_two_looks_differ_only_in_finishing_and_reuse_the_render(tmp_path: Path) -> None:
    fake = FakeFlamenco()
    graph = _graded_graph(_kit())
    run = _runner(graph, fake, tmp_path)
    first = graph.run(run)
    assert first.executed == ["shot", "render_preview", "render_scene", "finish"], first
    scene = graph.output("render_scene")
    assert isinstance(scene, dict) and isinstance(scene["frames"], dict)
    assert list(scene["frames"]) == ["frame_0001.exr"]
    clean = graph.output("finish")
    assert isinstance(clean, dict) and clean["look_id"] == "clean"
    assert clean["render_recipe_hash"] == scene["recipe_hash"]
    assert clean["playback"] == ColorPipeline().playback()
    assert len(fake.submitted) == 3

    assert _set_look(graph, "cinematic") == {"finish"}
    second = graph.run(run)
    assert second.executed == ["finish"] and len(fake.submitted) == 4
    assert graph.output("render_scene") == scene
    cinematic = graph.output("finish")
    assert isinstance(cinematic, dict) and cinematic["look_id"] == "cinematic"
    assert cinematic["render_recipe_hash"] == scene["recipe_hash"]
    assert cinematic["frames"] != clean["frames"]
    # the two finish jobs read the same staged EXRs and differ only in finish.apply_look
    a, b = fake.recipe(2), fake.recipe(3)
    assert (
        fake.submitted[2]["settings"]["assets_dir"] == fake.submitted[3]["settings"]["assets_dir"]
    )
    differ = [(x, y) for x, y in zip(a["steps"], b["steps"], strict=True) if x != y]
    assert [x["tool"] for x, _ in differ] == ["finish.apply_look"]
    assert {s["tool"] for s in b["steps"]} == {
        "scene.clear",
        "render.set_resolution",
        "render.set_output",
        "render.set_property",
        "finish.apply_look",
    }
    assert b["steps"][-1]["args"]["frames"] == ["frames/frame_0001.exr"]

    _set_look(graph, "clean")
    third = graph.run(run)
    assert third.executed == [] and third.reused == ["finish"] and len(fake.submitted) == 4
    assert graph.output("finish") == clean


def test_a_look_with_other_render_rules_is_refused_before_submitting(tmp_path: Path) -> None:
    blurry = Look(id="blurry", motion_blur=True, depth_of_field=False)
    fake = FakeFlamenco()
    graph = _graded_graph(_kit(blurry))
    run = _runner(graph, fake, tmp_path)
    graph.run(run)
    _set_look(graph, "blurry")
    report = graph.run(run)
    assert report.failed == ["finish"] and len(fake.submitted) == 3
    assert graph.status("finish") is NodeStatus.FAILED
