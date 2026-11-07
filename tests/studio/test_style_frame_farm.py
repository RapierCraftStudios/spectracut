# SPDX-License-Identifier: AGPL-3.0-or-later
"""Style frames rendered by Blender on the (fake) Flamenco farm at the final tier (#271)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import JsonValue

from spectracut.finishing.color import hex_to_linear
from spectracut.providers.base import Completion, VisionScore
from spectracut.providers.mock_image import MockImageProvider
from spectracut.queue.flamenco import WorkerTag
from spectracut.queue.signing import generate_keypair, load_private_key
from spectracut.storyboard.explore import record_image
from spectracut.storyboard.models import ArtDirection, StyleFrame
from spectracut.studio import device_shot as ds
from spectracut.studio.departments.style_frame_farm import (
    CONCEPT_FILE,
    PROVIDER,
    FarmRecipeRenderer,
    FarmStyleFrameRenderer,
    SceneStyleFrameRenderer,
    style_frame_recipe,
)

TAG = "3070laptopgpu"
ART = ArtDirection.model_validate(
    {
        "mood_board": [{"ref": "clean studio product shot", "take": "soft key light"}],
        "palette": {"swatches": {"ink": "#101820", "signal": "#00c2a8"}},
        "materials": ["matte anodised aluminium"],
        "look": {"id": "clean"},
    }
)


def _frame(tmp: Path, frame_id: str = "hero", *, concept: bool = True) -> StyleFrame:
    frame = StyleFrame(id=frame_id, description="Dashboard hero", prompt="a dashboard")
    if not concept:
        return frame
    path = tmp / f"{frame_id}-concept.png"
    MockImageProvider().generate(frame.prompt, path)
    return frame.model_copy(update={"concept": record_image(path, "mock-image")})


def _tools(recipe: Any) -> list[str]:
    return [step.tool for step in recipe.steps]


def test_recipe_pins_the_concept_image_on_the_backdrop(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    assert frame.concept is not None
    recipe = style_frame_recipe(frame, ART, CONCEPT_FILE)
    screen = next(s for s in recipe.steps if s.tool == "material.apply_screen")
    sha = frame.concept.sha256.removeprefix("sha256:")
    assert screen.args["image"] == CONCEPT_FILE and screen.args["image_sha256"] == sha
    assert ds.referenced_assets(recipe) == {CONCEPT_FILE: sha}
    assert "material.apply_preset" not in _tools(recipe)


def test_recipe_without_a_concept_uses_the_palette_colour(tmp_path: Path) -> None:
    recipe = style_frame_recipe(_frame(tmp_path, concept=False), ART)
    tools = _tools(recipe)
    assert "material.apply_screen" not in tools
    preset = next(s for s in recipe.steps if s.tool == "material.apply_preset")
    # #101820 linearised (sRGB EOTF), so it renders back to its own hex under BRAND_PIPELINE.
    assert preset.args["color"] == list(hex_to_linear("#101820"))
    assert preset.args["color"] == pytest.approx([0.005182, 0.009134, 0.014444], abs=1e-6)
    assert ds.referenced_assets(recipe) == {}


def test_recipe_ends_with_final_tier_png_output(tmp_path: Path) -> None:
    recipe = style_frame_recipe(_frame(tmp_path), ART, CONCEPT_FILE)
    tools = _tools(recipe)
    view = {
        s.args["property"]: s.args["value"]
        for s in recipe.steps[-5:-1]
        if s.tool == "render.set_property"
    }
    assert view == {
        "view_settings.display_device": "sRGB",
        "view_settings.view_transform": "Standard",
        "view_settings.look": "None",
        "view_settings.exposure": 0.0,
    }
    assert tools[0] == "scene.clear"
    assert "render.set_resolution" in tools
    assert recipe.steps[-1].tool == "render.set_output"
    assert recipe.steps[-1].args == {"format": "PNG"}


class FakeFlamenco:
    """A Manager whose jobs complete at once, writing one frame and report.json."""

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    def list_tags(self) -> list[WorkerTag]:
        return [WorkerTag(id="tag-1", name=TAG)]

    def submit(self, job: dict[str, Any]) -> dict[str, Any]:
        self.submitted.append(job)
        settings = job["settings"]
        signed = json.loads(Path(settings["job_file"]).read_text(encoding="utf-8"))
        out = Path(settings["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "frame_0001.png").write_bytes(signed["envelope"]["recipe_hash"].encode())
        report = {"reports": [], "scene_graph_hash": "sha256:" + "a" * 64}
        (out / "report.json").write_text(json.dumps(report), encoding="utf-8")
        return {"id": f"fl-{len(self.submitted)}"}

    def job_status(self, job_id: str) -> str:
        return "completed"


class Ticks:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 90.0
        return self.t


@dataclass
class Deps:
    config: ds.DeviceShotConfig
    client: FakeFlamenco
    key: Ed25519PrivateKey
    options: ds.ExecutorOptions | None


def test_renderer_runs_one_farm_job_per_frame_and_reports_gpu_minutes(tmp_path: Path) -> None:
    fake = FakeFlamenco()
    private, _public = generate_keypair()
    built: list[int] = []

    def deps() -> Deps:
        built.append(1)
        config = ds.DeviceShotConfig(
            flamenco_url="http://fake",
            shared=tmp_path / "shared",
            worker_tag=TAG,
        )
        opts = ds.ExecutorOptions(clock=Ticks(), sleep=lambda _s: None, poll_interval=0.0)
        return Deps(config, fake, load_private_key(private), opts)

    render = FarmStyleFrameRenderer(deps, "film", tmp_path / "work")
    frames = [_frame(tmp_path, "hero"), _frame(tmp_path, "cta", concept=False)]
    for frame in frames:
        out = tmp_path / "out" / f"{frame.id}.png"
        record, usage = render(frame, ART, out)
        digest = hashlib.sha256(out.read_bytes()).hexdigest()
        assert record.path == str(out) and record.sha256 == f"sha256:{digest}"
        assert record.provider == PROVIDER
        assert usage.gpu_minutes > 0
    assert built == [1]
    assert len(fake.submitted) == len(frames)
    assert all(job["worker_tag"] == "tag-1" for job in fake.submitted)
    # With no assets-dir override the concept is staged into shared storage, pinned.
    staged = list((tmp_path / "shared").rglob(CONCEPT_FILE))
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in staged] == [
        frames[0].concept.sha256.removeprefix("sha256:")  # type: ignore[union-attr]
    ]


def test_recipe_renderer_runs_any_recipe_as_one_preview_farm_job(tmp_path: Path) -> None:
    from spectracut.spec.recipe import Recipe, RecipeStep
    from spectracut.studio.departments.shots import tier_recipe
    from spectracut.studio.node import Tier

    fake = FakeFlamenco()
    private, _public = generate_keypair()
    built: list[int] = []

    def deps() -> Deps:
        built.append(1)
        config = ds.DeviceShotConfig(
            flamenco_url="http://fake", shared=tmp_path / "shared", worker_tag=TAG
        )
        opts = ds.ExecutorOptions(clock=Ticks(), sleep=lambda _s: None, poll_interval=0.0)
        return Deps(config, fake, load_private_key(private), opts)

    recipe = Recipe(
        id="one-beam",
        shot_id="one-beam",
        blender_version="4.5",
        steps=[
            RecipeStep(index=0, tool="scene.clear", args={}),
            RecipeStep(index=1, tool="light.add", args={"kind": "area"}),
        ],
    )
    render = FarmRecipeRenderer(deps, "film", tmp_path / "work")
    out = tmp_path / "out" / "frame.png"
    record, usage = render(recipe, out)
    assert record.path == str(out) and record.provider == PROVIDER
    assert record.sha256 == f"sha256:{hashlib.sha256(out.read_bytes()).hexdigest()}"
    assert usage.gpu_minutes > 0
    assert built == [1] and len(fake.submitted) == 1
    signed = json.loads(Path(fake.submitted[0]["settings"]["job_file"]).read_text("utf-8"))
    sent = signed["envelope"]["recipe"]
    expected = tier_recipe(recipe, Tier.PREVIEW)
    assert [s["tool"] for s in sent["steps"]] == _tools(expected)
    assert signed["envelope"]["recipe_hash"] == expected.content_hash
    assert fake.submitted[0]["worker_tag"] == "tag-1"


class SceneText:
    """The builder's text model: one fixed build, records the prompts."""

    name = "scene-text"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, system: str, prompt: str, *, schema: dict[str, Any]) -> Completion:
        self.prompts.append(prompt)
        calls: list[JsonValue] = [
            {"tool": "scene.clear", "args": {}},
            {"tool": "object.add_primitive", "args": {"kind": "cube", "name": "slab"}},
            {"tool": "camera.add", "args": {"name": "cam", "location": [0, -6, 1]}},
            {"tool": "camera.use", "args": {"name": "cam"}},
        ]
        return Completion(output={"plan": "slab", "calls": calls}, usage={"claude_requests": 1})


class SceneJudge:
    name = "scene-judge"
    last_usage: dict[str, int] = {}

    def __init__(self, score: float) -> None:
        self.value = score
        self.rubrics: list[str] = []

    def score(self, frame_path: Path, rubric: str) -> VisionScore:
        self.rubrics.append(rubric)
        return VisionScore(score=self.value, notes="needs more beam")


def _scene_deps(tmp_path: Path, fake: FakeFlamenco, built: list[int]) -> Any:
    private, _public = generate_keypair()

    def deps() -> Deps:
        built.append(1)
        config = ds.DeviceShotConfig(
            flamenco_url="http://fake", shared=tmp_path / "shared", worker_tag=TAG
        )
        opts = ds.ExecutorOptions(clock=Ticks(), sleep=lambda _s: None, poll_interval=0.0)
        return Deps(config, fake, load_private_key(private), opts)

    return deps


def test_scene_renderer_builds_at_preview_then_renders_the_best_at_final(tmp_path: Path) -> None:
    from spectracut.studio.departments.shots import tier_recipe
    from spectracut.studio.node import Tier
    from spectracut.studio.profiles import load_profile
    from spectracut.studio.scene_builder import SceneBuild

    fake, text, judge = FakeFlamenco(), SceneText(), SceneJudge(0.95)
    built: list[int] = []
    render = SceneStyleFrameRenderer(
        lambda: text,
        lambda: judge,
        load_profile("standard"),
        _scene_deps(tmp_path, fake, built),
        "film",
        tmp_path / "work",
    )
    assert built == [] and not text.prompts  # lazy: nothing built until a frame renders
    frame = StyleFrame(id="hero", description="Dashboard hero", prompt="a glowing plinth")
    out = tmp_path / "out" / "hero.png"
    record, usage = render(frame, ART, out)
    assert record.path == str(out) and out.is_file() and record.provider == PROVIDER
    # The builder saw the description and the prompt; the verifier scored against both.
    assert "Dashboard hero" in text.prompts[0] and "a glowing plinth" in text.prompts[0]
    assert judge.rubrics and "Dashboard hero" in judge.rubrics[0]
    build = SceneBuild.model_validate_json(
        (tmp_path / "work" / "scene" / "hero" / "build.json").read_text("utf-8")
    )
    assert build.recipe is not None and build.status == "accepted"
    # Preview candidates, then one final-tier render of the best recipe.
    names = [str(job["name"]) for job in fake.submitted]
    assert names[-1].startswith("film-style_frames-") and len(names) == len(build.iterations) + 1
    signed = json.loads(Path(fake.submitted[-1]["settings"]["job_file"]).read_text("utf-8"))
    final = tier_recipe(build.recipe, Tier.FINAL)
    assert signed["envelope"]["recipe_hash"] == final.content_hash
    assert usage.gpu_minutes > build.usage.gpu_minutes > 0
    assert usage.ai_usage == build.usage.ai_usage and usage.ai_usage


def test_scene_renderer_with_no_recipe_names_the_frame(tmp_path: Path) -> None:
    from spectracut.providers.mock_text import MockTextProvider
    from spectracut.studio.profiles import load_profile

    render = SceneStyleFrameRenderer(
        MockTextProvider,  # schema-valid nonsense: every call is refused, nothing renders
        lambda: SceneJudge(0.95),
        load_profile("standard"),
        _scene_deps(tmp_path, FakeFlamenco(), []),
        "film",
        tmp_path / "work",
    )
    frame = StyleFrame(id="cta", description="End card", prompt="the logo")
    with pytest.raises(ValueError, match="style frame 'cta'.*no recipe"):
        render(frame, ART, tmp_path / "out" / "cta.png")
