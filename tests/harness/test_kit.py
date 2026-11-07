# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lookdev kits (#346): the One Beam kit loads, expands and compiles to runtime steps only."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import JsonValue
from spectracut_host.guard import runtime_tool_names

from spectracut.harness.errors import HarnessError
from spectracut.harness.kit import KITS_DIR, Kit, kit_from, load_kit
from spectracut.providers.base import Completion
from spectracut.storyboard.models import ArtDirection
from spectracut.studio import device_shot as ds
from spectracut.studio.graph import ProductionGraph, RunContext, RunResult
from spectracut.studio.node import NodeSpec
from spectracut.studio.scene_builder import (
    CALLS_SCHEMA,
    SceneBrief,
    SceneBuild,
    SceneBuilder,
    compile_calls,
    scene_build_runner,
)

ROOT = Path(__file__).resolve().parents[2]
TOOLS_PY = ROOT / "host" / "bpy_runtime" / "tools.py"
KIT_DIR = KITS_DIR / "one-beam"
ASSETS = {"glass_tiers", "beam", "cable_knot", "readout_slab", "device_stage"}
LIGHTING = {"studio_softbox", "rim_key"}
ART = ArtDirection.model_validate(
    {
        "mood_board": [{"ref": "glass product macro", "take": "rim-lit edges"}],
        "palette": {"swatches": {"ink": "#101820", "beam": "#00c2a8"}},
        "materials": ["clear glass"],
        "look": {"id": "clean"},
    }
)


@pytest.fixture(scope="module")
def kit() -> Kit:
    return load_kit(KIT_DIR)


def _copy_kit(tmp: Path, mutate: Any) -> Path:
    """The One Beam kit copied to ``tmp`` with ``mutate(raw)`` applied to its yaml."""
    shutil.copytree(KIT_DIR, tmp / "one-beam")
    path = tmp / "one-beam" / "kit.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(raw)
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _runtime_ok(tools: list[str]) -> None:
    assert set(tools) <= runtime_tool_names(TOOLS_PY)


def test_one_beam_kit_has_every_asset_and_lighting_preset(kit: Kit) -> None:
    kinds = {name: e.kind for name, e in kit.entries.items()}
    assert ASSETS <= {n for n, k in kinds.items() if k == "asset"}
    assert LIGHTING <= {n for n, k in kinds.items() if k == "lighting"}
    for entry in kit.entries.values():
        assert entry.provenance.origin in ("original", "CC0") and entry.provenance.author
    for f in kit.files:
        assert f.provenance.origin in ("original", "CC0")


def test_every_entry_expands_and_compiles_to_runtime_steps_only(kit: Kit) -> None:
    for name in kit.entries:
        recipe = compile_calls(
            [{"kit": name, "args": {}}], recipe_id=f"kit-{name}", shot_id="s", kit=kit
        )
        tools = [s.tool for s in recipe.steps]
        assert tools, name
        _runtime_ok(tools)


def test_expansion_substitutes_params_places_and_scales(kit: Kit) -> None:
    calls = kit.expand(
        "beam", {"name": "hero", "color": "#00c2a8", "location": [1, 2, 3], "scale": 2.0}
    )
    names = [c["args"].get("name") for c in calls if isinstance(c["args"], dict)]
    assert {"hero", "hero_core", "hero_glow", "hero_light"} <= set(names)
    body = next(
        c["args"] for c in calls if isinstance(c["args"], dict) and c["args"].get("name") == "hero"
    )
    assert isinstance(body, dict)
    assert body["location"] == [1.0, 2.0, 3.0]
    assert body["dimensions"] == [0.08, 0.08, 6.0]  # default length 3.0 scaled by 2
    tint = next(
        c["args"]
        for c in calls
        if c["tool"] == "material.apply_preset" and c["args"]["object"] == "hero"  # type: ignore[index]
    )
    assert isinstance(tint, dict) and tint["color"] != "#00c2a8"  # hex -> linear RGB
    hdri = kit.expand("studio_softbox", {})[0]["args"]
    assert isinstance(hdri, dict)
    pinned = kit.file("hdri/studio-softbox.hdr")
    assert hdri["path"] == "kits/one-beam/hdri/studio-softbox.hdr"
    assert hdri["sha256"] == pinned.sha256


def test_compiled_kit_hdri_is_staged_at_its_pinned_relpath(kit: Kit) -> None:
    recipe = compile_calls(
        [{"kit": "studio_softbox", "args": {}}], recipe_id="r", shot_id="s", kit=kit
    )
    refs = ds.referenced_assets(recipe)
    staged = kit.files_to_stage()
    assert refs and set(refs) <= set(staged)
    for rel, sha in refs.items():
        import hashlib

        assert hashlib.sha256(staged[rel].read_bytes()).hexdigest() == sha.removeprefix("sha256:")


def test_bad_kit_args_raise_one_error_against_the_original_index(kit: Kit) -> None:
    calls: list[JsonValue] = [
        {"tool": "scene.clear", "args": {}},
        {"kit": "beam", "args": {"colour": "#fff"}},
        {"kit": "glass_tiers", "args": {"roughness": 3}},
        {"kit": "beam", "args": {"color": "red"}},
        {"kit": "nope", "args": {}},
    ]
    with pytest.raises(HarnessError) as info:
        kit.expand_calls(calls)
    problems = info.value.details["problems"]
    assert [(p["index"], p["tool"]) for p in problems] == [
        (1, "kit:beam"),
        (2, "kit:glass_tiers"),
        (3, "kit:beam"),
        (4, "kit:nope"),
    ]
    with pytest.raises(HarnessError) as compiled:
        compile_calls(calls, recipe_id="r", shot_id="s", kit=kit)
    assert [p["index"] for p in compiled.value.details["problems"]] == [1, 2, 3, 4]
    assert "calls[2] kit:glass_tiers: roughness must be <= 1" in str(compiled.value)


def test_kit_entries_without_a_kit_are_refused() -> None:
    with pytest.raises(HarnessError) as info:
        compile_calls([{"kit": "beam", "args": {}}], recipe_id="r", shot_id="s")
    assert info.value.details["problems"] == [
        {"index": 0, "tool": "kit:beam", "message": "kit entries need a kit"}
    ]


def test_calls_schema_accepts_kit_items() -> None:
    items = CALLS_SCHEMA["properties"]["calls"]["items"]
    assert items["required"] == ["args"] and {"tool", "kit", "args"} <= set(items["properties"])


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda raw: raw["entries"]["beam"].pop("provenance"), "provenance"),
        (lambda raw: raw["files"][0].pop("provenance"), "provenance"),
        (
            lambda raw: raw["entries"]["beam"]["calls"].append({"tool": "inspect.bounds"}),
            "unknown tool",
        ),
        (lambda raw: raw["entries"]["beam"]["calls"].append({"tool": "fake.tool"}), "unknown tool"),
        (
            lambda raw: raw["entries"]["beam"]["calls"][0]["args"].update(name="${nope}"),
            "not a declared param",
        ),
        (lambda raw: raw["files"][0].update(sha256="0" * 64), "sha256"),
    ],
)
def test_loader_refuses_a_bad_kit(tmp_path: Path, mutate: Any, needle: str) -> None:
    path = _copy_kit(tmp_path, mutate)
    with pytest.raises(HarnessError) as info:
        load_kit(path)
    assert info.value.code == "config.invalid" and needle in str(info.value)


def test_loader_refuses_a_missing_licence_sidecar(tmp_path: Path) -> None:
    path = _copy_kit(tmp_path, lambda raw: None)
    (path.parent / "hdri" / "studio-softbox.hdr.license.json").unlink()
    with pytest.raises(HarnessError, match="licence sidecar"):
        load_kit(path)


def test_prompt_lists_the_kit_only_when_one_is_given(kit: Kit, tmp_path: Path) -> None:
    from spectracut.critique.scene_verifier import scene_critic

    class Text:
        name = "t"

        def complete(self, system: str, prompt: str, *, schema: dict[str, Any]) -> Completion:
            raise AssertionError

    class Vision:
        name = "v"
        last_usage: dict[str, int] = {}

    from spectracut.studio.profiles import load_profile

    prof = load_profile("standard")
    critic = scene_critic(Vision(), profile=prof, description="d", art=ART)  # type: ignore[arg-type]
    b = SceneBuilder(Text(), lambda r, p: None, critic, prof, tmp_path)  # type: ignore[arg-type]
    from spectracut.critique.revise import RevisionRequest

    req = RevisionRequest(attempt=1, candidate=0, state=None, notes="", window=())
    plain = b.prompt(SceneBrief(id="s", description="d", art=ART), req)
    with_kit = b.prompt(SceneBrief(id="s", description="d", art=ART, kit=kit), req)
    assert "Kit assets" not in plain and "Kit assets and lighting presets" in with_kit
    assert with_kit.startswith(plain.split("\n\nBuild tool cheat sheet")[0])
    for name in kit.entries:
        assert f'"{name}"' in with_kit
    assert '{"kit": "<name>", "args": {...}}' in with_kit


def test_runner_reads_a_kit_param(kit: Kit, tmp_path: Path) -> None:
    seen: list[SceneBrief] = []

    class Builder:
        def build(self, brief: SceneBrief) -> SceneBuild:
            seen.append(brief)
            raise RuntimeError("stop")

    scene = scene_build_runner(Builder())  # type: ignore[arg-type]

    def runner(ctx: RunContext) -> RunResult:
        if ctx.spec.kind == "art_direction":
            return RunResult(output=ART.model_dump(mode="json"))
        return scene(ctx)

    from spectracut.studio.scene_builder import SCENE_BUILD

    graph = ProductionGraph("p", profile=None)  # type: ignore[arg-type]
    graph.add_node(NodeSpec(id="art", kind="art_direction", department="art-director"))
    graph.add_node(
        NodeSpec(
            id="hero",
            kind=SCENE_BUILD,
            department="scene-builder",
            inputs=("art",),
            params={"description": "d", "kit": str(KIT_DIR / "kit.yaml")},
        )
    )
    graph.run(runner)  # the stub builder fails the node after recording its brief
    assert seen and seen[0].kit is not None and seen[0].kit.id == "one-beam"
    assert seen[0].brand_kit is None


def test_kit_from_accepts_a_kit_a_path_or_none(kit: Kit) -> None:
    assert kit_from(None) is None and kit_from(kit) is kit
    assert kit_from(str(KIT_DIR)).id == "one-beam"  # type: ignore[union-attr]
    with pytest.raises(HarnessError):
        kit_from(3)
