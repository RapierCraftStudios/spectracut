# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-shot comp graphs (#55): the library, render passes, comp-only re-runs, legibility.

The re-run case drives the ``device-shot-comped`` graph through a fake Flamenco
Manager. The same assertion on a real farm is ``test_comp_e2e.py``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from test_looks import (  # type: ignore[import-not-found]
    KIT,
    LOOKS,
    FakeFlamenco,
    _brief,
    _kit,
    _runner,
)

from spectracut.cli import studio as studio_mod
from spectracut.finishing import comp
from spectracut.finishing.color import ColorPipeline
from spectracut.finishing.looks import load_looks
from spectracut.qa.legibility import AA_RATIO, linear_legibility
from spectracut.studio import device_shot as ds
from spectracut.studio.estimate import breakdown
from spectracut.studio.graph import ProductionGraph
from spectracut.studio.node import Tier

ROOT = Path(__file__).resolve().parents[2]
COMPS = ROOT / "comps"
EXR_FORMATS = ("OPEN_EXR", "OPEN_EXR_MULTILAYER")


# ---- the library -------------------------------------------------------------------


def test_library_loads_every_comp_graph() -> None:
    comps = comp.load_comps(COMPS)
    assert set(comps) == {"device-clean", "device-cinematic", "screen-grade"}
    for graph in comps.values():
        assert graph.screen_matte == comp.DEFAULT_SCREEN_MATTE == "device_screen"
    ops = {name: [op.op for op in g.ops] for name, g in comps.items()}
    assert ops["device-clean"] == ["ui_sharpen", "screen_glow", "lens"]
    assert ops["device-cinematic"] == ["screen_glow", "glints", "light_wrap", "lens"]
    assert ops["screen-grade"] == ["screen_grade", "ui_sharpen"]
    assert comp.default_comps_dir() == COMPS


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("- a list\n", "must be a YAML mapping"),
        ("id: [unclosed\n", "is not valid YAML"),
        ("id: bad\nops: [{op: script, code: x}]\n", "is invalid"),
        ("id: bad\nops: [{op: lens, dispersion: 0.5}]\n", "is invalid"),
        ("id: bad\nops: [{op: lens, extra: 1}]\n", "is invalid"),
        ("id: bad\nshell: rm\n", "is invalid"),
        ("id: bad\nops: [{op: lens}, {op: lens}]\n", "repeats op"),
        ("id: other\n", "the file must be other.yaml"),
        ("id: bad\nops: [{op: screen_glow, strength: 0.5, threshold: 0.0}]\n", "illegible"),
    ],
)
def test_bad_comp_graphs_name_the_file(tmp_path: Path, text: str, needle: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=needle) as err:
        comp.load_comp(path)
    assert str(path) in str(err.value)
    with pytest.raises(ValueError, match="does not exist"):
        comp.load_comps(tmp_path / "nope")


def test_comp_params_key_on_the_graph_only() -> None:
    clean = comp.load_comps(COMPS)["device-clean"]
    params = comp.comp_params(clean)
    assert set(params) == {"comp_id", "comp_version", "screen_matte", "ops"}
    assert params["screen_matte"] == "device_screen"
    bumped = clean.model_copy(update={"version": 2})
    assert comp.comp_hash(bumped) != comp.comp_hash(clean)
    assert comp.comp_hash(clean) == comp.comp_hash(comp.load_comps(COMPS)["device-clean"])


# ---- render passes and comp steps ----------------------------------------------------


def test_passes_steps_write_multilayer_exr_with_depth_vectors_and_cryptomatte() -> None:
    steps = comp.passes_steps(start=5)
    assert [s.index for s in steps] == [5, 6]
    assert steps[0].tool == "render.set_passes"
    passes = steps[0].args["passes"]
    assert isinstance(passes, list)
    assert {"combined", "z", "vector", "cryptomatte_object"} <= set(passes)
    assert steps[1].tool == "render.set_output"
    assert steps[1].args == {"format": "OPEN_EXR_MULTILAYER", "color_depth": "32"}


def test_scene_render_recipe_with_passes_and_without() -> None:
    base = ds.build_device_recipe(
        _brief(), _kit(), screen_image="ui/dashboard.png", screen_sha256="0" * 64
    )
    clean = load_looks(LOOKS)["clean"]
    plain = ds.scene_render_recipe(base, clean, ColorPipeline())
    assert plain.steps[-1].tool == "render.set_output"
    assert plain.steps[-1].args == {"format": "OPEN_EXR", "color_depth": "16"}
    assert "render.set_passes" not in {s.tool for s in plain.steps}
    passes = ds.scene_render_recipe(base, clean, ColorPipeline(), passes=True)
    tail = [(s.tool, s.args) for s in passes.steps[-2:]]
    assert tail == [(s.tool, s.args) for s in comp.passes_steps()]
    assert [s.index for s in passes.steps] == list(range(len(passes.steps)))
    assert passes.steps[: len(plain.steps) - 1] == plain.steps[:-1]


def test_comp_steps_end_in_apply_graph_on_single_layer_exr() -> None:
    graph = comp.load_comps(COMPS)["screen-grade"]
    steps = comp.comp_steps(graph, ["frames/frame_0001.exr"], start=2)
    assert [(s.index, s.tool) for s in steps] == [(2, "render.set_output"), (3, "comp.apply_graph")]
    assert steps[0].args == {"format": "OPEN_EXR", "color_depth": "16"}
    assert steps[1].args == {**comp.comp_params(graph), "frames": ["frames/frame_0001.exr"]}
    with pytest.raises(ValueError, match="at least one input frame"):
        comp.comp_steps(graph, [])


def test_comped_profile_stages() -> None:
    p = ds.comped_profile()
    assert p.stages == ("shot", "render_preview", "render_scene", "comp", "finish")
    assert p.tier == {
        "shot": Tier.PREVIEW,
        "render_preview": Tier.PREVIEW,
        "render_scene": Tier.FINAL,
        "comp": Tier.FINAL,
        "finish": Tier.FINAL,
    }
    assert set(p.revision_budget.values()) == {0}
    assert set(p.gates.values()) == {"auto"}


# ---- a comp change re-runs compositing only ---------------------------------------


class MultilayerFlamenco(FakeFlamenco):  # type: ignore[misc]
    """``FakeFlamenco`` that writes ``.exr`` frames for single-layer and multilayer EXR."""

    def submit(self, job: dict[str, Any]) -> dict[str, Any]:
        self.submitted.append(job)
        settings = job["settings"]
        envelope = json.loads(Path(settings["job_file"]).read_text(encoding="utf-8"))["envelope"]
        outputs = [s for s in envelope["recipe"]["steps"] if s["tool"] == "render.set_output"]
        exr = bool(outputs) and outputs[-1]["args"]["format"] in EXR_FORMATS
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


def comped_graph(comp_id: str = "device-clean", *, passes: bool = True) -> ProductionGraph:
    kit = _kit()
    graph = ProductionGraph("comped", profile=ds.comped_profile())
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
        extra: dict[str, Any] = {}
        if spec.kind == ds.SHOT_STAGE:
            extra = shot_params
        elif spec.kind == ds.RENDER_SCENE_STAGE:
            extra = {**pick, ds.RENDER_PASSES_PARAM: passes}
        elif spec.kind == ds.FINISH_STAGE:
            extra = pick
        elif spec.kind == ds.COMP_STAGE:
            extra = {ds.COMP_ID_PARAM: comp_id}
        specs.append(spec.model_copy(update={"params": {**spec.params, **extra}}))
    graph.add_nodes(specs)
    return graph


def set_comp(graph: ProductionGraph, comp_id: str) -> frozenset[str]:
    params = {**graph.spec("comp").params, ds.COMP_ID_PARAM: comp_id}
    return graph.set_params("comp", params)


def _render_ledger(graph: ProductionGraph) -> tuple[int, float]:
    entries = [e for e in graph.ledger.entries() if e.kind.startswith("render_")]
    return len(entries), sum(e.gpu_minutes for e in entries)


def test_a_comp_change_reruns_comp_and_finish_only(tmp_path: Path) -> None:
    fake = MultilayerFlamenco()
    graph = comped_graph("device-clean")
    run = _runner(graph, fake, tmp_path)
    first = graph.run(run)
    assert first.executed == ["shot", "render_preview", "render_scene", "comp", "finish"], first
    assert len(fake.submitted) == 4
    scene = graph.output("render_scene")
    assert isinstance(scene, dict) and isinstance(scene["frames"], dict | list)
    assert list(scene["frames"]) == ["frame_0001.exr"]
    assert scene["render_passes"] == list(comp.RENDER_PASSES)
    render_recipe = fake.recipe(1)
    assert {"tool": "render.set_passes", "args": {"passes": list(comp.RENDER_PASSES)}} in [
        {"tool": s["tool"], "args": s["args"]} for s in render_recipe["steps"]
    ]
    comped = graph.output("comp")
    assert isinstance(comped, dict) and comped["comp_id"] == "device-clean"
    assert comped["recipe_hash"] == scene["recipe_hash"]
    assert comped["recipe"] == scene["recipe"]
    assert comped["render_rules"] == scene["render_rules"]
    comp_recipe = fake.recipe(2)
    tools = [s["tool"] for s in comp_recipe["steps"]]
    assert tools == [
        "scene.clear",
        "render.set_resolution",
        "render.set_output",
        "comp.apply_graph",
    ]
    apply = comp_recipe["steps"][-1]["args"]
    assert apply["screen_matte"] == "device_screen"
    assert apply["frames"] == ["frames/frame_0001.exr"]
    assert "render.render" not in json.dumps(comp_recipe)
    finished = graph.output("finish")
    assert isinstance(finished, dict) and finished["render_recipe_hash"] == scene["recipe_hash"]
    renders_before = _render_ledger(graph)

    assert set_comp(graph, "device-cinematic") == {"comp", "finish"}
    second = graph.run(run)
    assert second.executed == ["comp", "finish"], second
    assert len(fake.submitted) == 6
    assert graph.output("render_scene") == scene
    assert _render_ledger(graph) == renders_before
    recomped = graph.output("comp")
    assert isinstance(recomped, dict) and recomped["comp_id"] == "device-cinematic"
    assert recomped["frames"] != comped["frames"]
    assert (
        fake.submitted[2]["settings"]["assets_dir"] == fake.submitted[4]["settings"]["assets_dir"]
    )

    params = {**graph.spec("finish").params, ds.LOOK_ID_PARAM: "cinematic"}
    assert graph.set_params("finish", params) == {"finish"}
    third = graph.run(run)
    assert third.executed == ["finish"] and len(fake.submitted) == 7
    assert _render_ledger(graph) == renders_before


def test_a_comp_param_change_dirties_comp_and_finish(tmp_path: Path) -> None:
    fake = MultilayerFlamenco()
    graph = comped_graph("device-clean")
    run = _runner(graph, fake, tmp_path)
    graph.run(run)
    library = tmp_path / "comps"
    library.mkdir()
    (library / "device-clean.yaml").write_text(
        (COMPS / "device-clean.yaml").read_text(encoding="utf-8").replace("0.3", "0.2"),
        encoding="utf-8",
    )
    params = {**graph.spec("comp").params, ds.COMPS_DIR_PARAM: str(library)}
    assert graph.set_params("comp", params) == {"comp", "finish"}
    second = graph.run(run)
    assert second.executed == ["comp", "finish"] and len(fake.submitted) == 6


def test_comp_refuses_a_render_without_passes_before_submitting(tmp_path: Path) -> None:
    fake = MultilayerFlamenco()
    graph = comped_graph("device-clean", passes=False)
    run = _runner(graph, fake, tmp_path)
    report = graph.run(run)
    assert report.failed == ["comp"] and len(fake.submitted) == 2


# ---- R6/R11: the screen stays legible through every comp graph ----------------------


@pytest.mark.parametrize("comp_id", sorted(comp.load_comps(COMPS)))
@pytest.mark.parametrize("case", ["dark-on-light", "light-on-dark"])
def test_every_comp_graph_keeps_the_screen_legible(comp_id: str, case: str) -> None:
    graph = comp.load_comps(COMPS)[comp_id]
    text, background = comp.UI_CASES[case]
    patch = comp.ui_text_patch(text, background)
    before = linear_legibility(patch)
    after = linear_legibility(comp.screen_transfer(graph, patch))
    assert after.contrast >= AA_RATIO
    assert after.contrast >= (1 - comp.MAX_CONTRAST_LOSS) * before.contrast


def test_scene_ops_never_touch_the_screen_model() -> None:
    scene_only = comp.CompGraph(
        id="scene-only",
        ops=[comp.Glints(strength=1.0), comp.LightWrap(amount=0.5), comp.LensOp(dispersion=0.05)],
    )
    patch = comp.ui_text_patch(0.012, 0.85)
    assert comp.screen_transfer(scene_only, patch) == patch
    with pytest.raises(ValueError, match="non-empty rectangle"):
        comp.screen_transfer(scene_only, [])
