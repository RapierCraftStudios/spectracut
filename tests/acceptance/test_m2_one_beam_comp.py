# SPDX-License-Identifier: AGPL-3.0-or-later
"""M2 One Beam hero comp (#347): abstract style frames get glare, CA, vignette and grain.

The unit case runs always: the glass-stack hero, built by the scene builder from One
Beam kit entries, is finished by :class:`SceneStyleFrameRenderer` with the ``one-beam``
comp graph and the AlterLab brand kit against a fake Flamenco Manager, and its final
render is three jobs: passes render (multilayer EXR) -> ``comp.apply_graph`` (bloom,
glints, lens dispersion, no screen matte) -> ``finish.apply_look`` (the ``clean`` Look:
vignette and grain). The farm case (marked ``e2e``) runs only with ``SPECTRACUT_E2E=1``
and ``SPECTRACUT_FLAMENCO_URL`` (post-deploy check ``m2-one-beam-hero-comp-farm-e2e``):
it renders the hero on the farm, scores it with the scene verifier (at least
:data:`HERO_SCORE`) and writes the frame for the owner's review.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import JsonValue

from spectracut.finishing import comp
from spectracut.harness.kit import KITS_DIR, load_kit
from spectracut.providers.base import Completion, VisionScore
from spectracut.queue.flamenco import WorkerTag
from spectracut.queue.signing import generate_keypair, load_private_key
from spectracut.spec.brand import BrandKit, load_brand_kit
from spectracut.storyboard.models import ArtDirection, StyleFrame
from spectracut.studio import device_shot as ds
from spectracut.studio.departments.style_frame_farm import SceneStyleFrameRenderer
from spectracut.studio.profiles import load_profile

ROOT = Path(__file__).resolve().parents[2]
E2E = os.environ.get("SPECTRACUT_E2E") == "1" and bool(os.environ.get("SPECTRACUT_FLAMENCO_URL"))
TAG = "3070laptopgpu"
HERO_SCORE = 0.75
"""The scene verifier score the hero frame must reach on the farm."""

DESCRIPTION = (
    "A stack of clear glass tiers, centred and filling the frame, rim-lit against a dark "
    "gradient. A single emissive accent-blue beam passes through the stack, glowing softly "
    "where it meets the glass."
)
ART = ArtDirection.model_validate(
    {
        "mood_board": [{"ref": "glass product macro", "take": "rim-lit edges, soft glow"}],
        "palette": {"swatches": {"ink": "#101820", "beam": "#2f6bff", "metal": "#8a9099"}},
        "materials": ["clear glass", "brushed metal"],
        "look": {"id": "clean"},
    }
)
HERO_CALLS: list[JsonValue] = [
    {"tool": "scene.clear", "args": {}},
    {"kit": "glass_tiers", "args": {"name": "tiers", "tint": "#f5f7fa"}},
    {"kit": "beam", "args": {"color": "#2f6bff", "core_color": "#f5f7fa"}},
    {"kit": "studio_softbox", "args": {}},
    {"kit": "rim_key", "args": {}},
    {"tool": "camera.add", "args": {"name": "cam", "location": [0, -6, 1.2]}},
    {"tool": "camera.use", "args": {"name": "cam"}},
]


def _brand_kit() -> BrandKit:
    return load_brand_kit(ROOT / "brands" / "alterlab" / "brand-kit.yaml")


def _one_beam() -> comp.CompGraph:
    return comp.load_comps(comp.default_comps_dir())["one-beam"]


# ---- always-on unit test ----------------------------------------------------------------


class HeroText:
    """The builder's text model: the glass-stack hero from kit entries."""

    name = "hero-text"

    def complete(self, system: str, prompt: str, *, schema: dict[str, Any]) -> Completion:
        return Completion(output={"plan": "hero", "calls": HERO_CALLS}, usage={"claude": 1})


class Judge:
    name = "judge"
    last_usage: dict[str, int] = {}

    def score(self, frame_path: Path, rubric: str) -> VisionScore:
        return VisionScore(score=0.9, notes="")


class FakeFlamenco:
    """Jobs complete at once, writing ``frame_0001`` in the recipe's output format."""

    def __init__(self) -> None:
        self.recipes: list[dict[str, Any]] = []
        self.names: list[str] = []

    def list_tags(self) -> list[WorkerTag]:
        return [WorkerTag(id="tag-1", name=TAG)]

    def submit(self, job: dict[str, Any]) -> dict[str, Any]:
        settings = job["settings"]
        signed = json.loads(Path(settings["job_file"]).read_text(encoding="utf-8"))
        recipe = signed["envelope"]["recipe"]
        self.recipes.append(recipe)
        self.names.append(str(job["name"]))
        outputs = [s["args"]["format"] for s in recipe["steps"] if s["tool"] == "render.set_output"]
        suffix = ".exr" if str(outputs[-1]).startswith("OPEN_EXR") else ".png"
        out = Path(settings["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / f"frame_0001{suffix}").write_bytes(signed["envelope"]["recipe_hash"].encode())
        report = {"reports": [], "scene_graph_hash": "sha256:" + "a" * 64}
        (out / "report.json").write_text(json.dumps(report), encoding="utf-8")
        return {"id": f"fl-{len(self.recipes)}"}

    def job_status(self, job_id: str) -> str:
        return "completed"


class Ticks:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 60.0
        return self.t


@dataclass
class Deps:
    config: ds.DeviceShotConfig
    client: FakeFlamenco
    key: Ed25519PrivateKey
    options: ds.ExecutorOptions | None


def test_hero_frame_renders_passes_then_one_beam_comp_then_the_brand_look(
    tmp_path: Path,
) -> None:
    fake = FakeFlamenco()
    private, _public = generate_keypair()
    config = ds.DeviceShotConfig(
        flamenco_url="http://fake", shared=tmp_path / "shared", worker_tag=TAG
    )
    opts = ds.ExecutorOptions(clock=Ticks(), sleep=lambda _s: None, poll_interval=0.0)
    deps = Deps(config, fake, load_private_key(private), opts)
    kit, brand = load_kit(KITS_DIR / "one-beam"), _brand_kit()
    render = SceneStyleFrameRenderer(
        HeroText,
        Judge,
        load_profile("standard"),
        lambda: deps,
        "alterlab",
        tmp_path / "work",
        brand_kit=lambda: brand,
        kit=lambda: kit,
        comp=_one_beam,
    )
    out = tmp_path / "out" / "hero.png"
    frame = StyleFrame(id="hero", description=DESCRIPTION, prompt="glass stack, blue beam")
    record, usage = render(frame, ART, out)
    assert out.is_file() and record.path == str(out) and usage.gpu_minutes > 0

    finals = [r for r, n in zip(fake.recipes, fake.names, strict=True) if "style_frames" in n]
    assert len(finals) == 3
    passes, comped, finished = finals
    # The hero is built from the kit: its entries expand into the passes render.
    tools = [s["tool"] for s in passes["steps"]]
    assert "world.set_hdri" in tools and "render.set_passes" in tools
    assert passes["steps"][-1]["args"]["format"] == "OPEN_EXR_MULTILAYER"
    # Glare/bloom, glints and subtle chromatic aberration on the whole frame.
    apply = comped["steps"][-1]
    assert apply["tool"] == "comp.apply_graph" and apply["args"]["screen_matte"] is None
    ops = {op["op"]: op for op in apply["args"]["ops"]}
    assert set(ops) == {"bloom", "glints", "lens"}
    assert ops["bloom"]["strength"] > 0 and 0 < ops["lens"]["dispersion"] <= 0.02
    # Then the brand kit's clean Look: vignette and grain, within its QA thresholds.
    look = finished["steps"][-1]
    assert look["tool"] == "finish.apply_look" and look["args"]["look_id"] == "clean"
    assert look["args"]["lens"]["vignette"] > 0 and look["args"]["grain"] > 0
    thresholds = brand.looks[0].qa_thresholds
    assert look["args"]["grain"] <= thresholds.grain_max
    assert look["args"]["bloom"] <= thresholds.bloom_max
    assert look["args"]["frames"] == ["frames/frame_0001.exr"]


# ---- farm e2e ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.skipif(not E2E, reason="set SPECTRACUT_E2E=1 and SPECTRACUT_FLAMENCO_URL")
def test_e2e_hero_frame_scores_on_the_farm(tmp_path: Path) -> None:
    from spectracut.cli import studio as studio_mod
    from spectracut.critique.scene_verifier import PACK, scene_verifier_prompt
    from spectracut.providers.base import select_text_provider, select_vision_judge
    from spectracut.studio.store import ProductionStore

    home = Path(os.environ.get("SPECTRACUT_HOME") or Path.home() / ".local/share/spectracut")
    production = "alterlab-one-beam-comp-e2e"
    deps = studio_mod.default_device_shot(studio_mod.Production(production, ProductionStore(home)))
    kit, brand = load_kit(KITS_DIR / "one-beam"), _brand_kit()
    judge = select_vision_judge()
    profile = load_profile("standard")
    render = SceneStyleFrameRenderer(
        select_text_provider,
        lambda: judge,
        profile,
        lambda: deps,
        production,
        tmp_path / "work",
        brand_kit=lambda: brand,
        kit=lambda: kit,
        comp=_one_beam,
    )
    out_dir = Path(os.environ.get("SPECTRACUT_E2E_OUT") or tmp_path / "out")
    out = out_dir / "one-beam-hero.png"
    frame = StyleFrame(id="hero", description=DESCRIPTION, prompt="glass stack, blue beam")
    record, usage = render(frame, ART, out)
    assert out.is_file() and record.path == str(out) and usage.gpu_minutes > 0
    rubric = (PACK / "rubric.md").read_text(encoding="utf-8")
    prompt = scene_verifier_prompt(DESCRIPTION, ART, brand_kit=brand, rubric=rubric)
    verdict = judge.score(out, prompt)
    print(f"one-beam hero: {out} scored {verdict.score:.2f}: {verdict.notes}")
    assert verdict.score >= HERO_SCORE, verdict.notes
