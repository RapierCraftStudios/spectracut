# SPDX-License-Identifier: AGPL-3.0-or-later
"""Finishing (#252): the edit, the graded master and the delivery set, built in code.

A fake Flamenco Manager "renders" by writing frames and ``report.json``, a mock vision
judge scores, and a fake ``run_qa`` records what each deliverable was checked with.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from spectracut.audio.recipe import Provenance
from spectracut.audio.vo import VoiceoverLine, VoiceoverTrack
from spectracut.edit import check_editorial
from spectracut.edit.otio_io import read_otio
from spectracut.edit.timeline import Clip, Edit, Marker, Track
from spectracut.formats.safe_areas import RESOLUTIONS
from spectracut.providers.base import VisionScore
from spectracut.qa.common import QACheck
from spectracut.qa.report import QAReport
from spectracut.queue.flamenco import WorkerTag
from spectracut.queue.signing import generate_keypair, load_private_key
from spectracut.spec.brand import load_brand_kit
from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.studio import device_shot as ds
from spectracut.studio.departments import STAGE_DEPARTMENTS, all_departments
from spectracut.studio.departments import finishing as fd
from spectracut.studio.departments.shots import RenderedShots, SceneBuilderShot
from spectracut.studio.departments.sound import Mix, MusicBed
from spectracut.studio.estimate import BRIEF_PARAM
from spectracut.studio.graph import RunContext, RunResult
from spectracut.studio.node import NodeSpec, Tier
from spectracut.templates import TEMPLATES

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "brands" / "alterlab" / "brand-kit.yaml"
TAG = "3070laptopgpu"
SHOT_IDS = [f"s{n}" for n in range(6)]
SHOT_FRAMES = 120  # every template animates 120 frames (5 s at 24 fps)


@pytest.fixture(autouse=True)
def _qa_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPECTRACUT_HOME", str(tmp_path / "qa-home"))
    monkeypatch.setattr("spectracut.qa.copy_ocr.available_reader", lambda: None)


class FakeFlamenco:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []
        self.recipes: list[dict[str, Any]] = []

    def list_tags(self) -> list[WorkerTag]:
        return [WorkerTag(id="tag-1", name=TAG)]

    def submit(self, job: dict[str, Any]) -> dict[str, Any]:
        self.submitted.append(job)
        settings = job["settings"]
        signed = json.loads(Path(settings["job_file"]).read_text(encoding="utf-8"))
        self.recipes.append(signed["envelope"]["recipe"])
        recipe_hash = signed["envelope"]["recipe_hash"]
        out = Path(settings["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        start, end = (int(x) for x in settings["frames"].split("-"))
        for frame in (start, end):
            (out / f"frame_{frame:04d}.png").write_bytes(f"{recipe_hash}:{frame}".encode())
        digest = hashlib.sha256(recipe_hash.encode()).hexdigest()
        report = {"reports": [], "scene_graph_hash": "sha256:" + digest}
        (out / "report.json").write_text(json.dumps(report), encoding="utf-8")
        return {"id": f"fl-{len(self.submitted)}"}

    def job_status(self, job_id: str) -> str:
        return "completed"


class Judge:
    name = "mock"
    last_usage: dict[str, int] = {"input_tokens": 100, "output_tokens": 40}

    def score(self, frame_path: Path, rubric: str) -> VisionScore:
        return VisionScore(score=0.9, notes="legible")


class Ticks:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 90.0
        return self.t


class FakeQA:
    """``run_qa`` stand-in: passes unless the render id contains ``fail``."""

    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def __call__(self, render_id: str, frames: Any, look: Any, **kw: Any) -> QAReport:
        self.calls.append({"render_id": render_id, "frames": len(frames), **kw})
        bad = self.fail is not None and self.fail in render_id
        checks = [QACheck(name="safe_area", status="fail" if bad else "pass", detail="x")]
        return QAReport(
            render_id=render_id, created_at=datetime.now(UTC), checks=checks, passed=not bad
        )


def _brief(cta: str = "Try it") -> Brief:
    return Brief(
        id="alterlab",
        product="AlterLab",
        audience=AudienceProfile(personas=[Persona(name="Dev", description="ships often")]),
        sources=[ClaimSource(id="docs", title="Docs", ref="https://example.test/docs")],
        proof_points=[ProofPoint(claim="Fast", source_id="docs")],
        cta=cta,
    )


def _animatic() -> Edit:
    picture = [
        Clip(id=f"pic-{s}", shot_id=s, media_ref=f"board:{s}", source_out=SHOT_FRAMES)
        for s in SHOT_IDS
    ]
    temp = [Clip(id="vo-a", media_ref="temp:a", source_out=48)]
    return Edit(
        id="animatic",
        tracks=[
            Track(id="picture", kind="video", items=picture),
            Track(id="vo", kind="audio", items=temp),
        ],
        markers=[Marker(name="cta", frame=600)],
    )


def _shots() -> RenderedShots:
    kit = load_brand_kit(KIT)
    rows = []
    for s in SHOT_IDS:
        recipe = TEMPLATES["typography"].build(kit, {"body": f"Every run {s}"})
        rows.append(
            {
                "shot_id": s,
                "template": "typography",
                "recipe": recipe.model_dump(mode="json"),
                "recipe_hash": recipe.content_hash,
                "tier": "final",
                "scene_graph_hash": "sha256:" + "1" * 64,
                "frames": {"frame_0001.png": "a"},
                "output_dir": f"/renders/{s}",
                "job_id": f"job-{s}",
                "worker_tag": TAG,
                "qa": {"passed": True},
                "vision_qa": {"score": 0.9},
            }
        )
    return RenderedShots.model_validate({"shots": rows})


def _vo(temp: bool = False) -> VoiceoverTrack:
    lines = [
        VoiceoverLine(id="a", beat="hook", text="One view", media_ref="/vo/a.wav", duration_s=2.0),
        VoiceoverLine(id="b", beat="cta", text="Try it", media_ref="/vo/b.wav", duration_s=1.5),
    ]
    return VoiceoverTrack(
        lines=lines,
        provider="local" if temp else "elevenlabs",
        temp_vo=temp,
        mark="temp-vo" if temp else None,
    )


BEATS = [0.1 + 0.5 * k for k in range(80)]
"""120 BPM, a tenth of a second off the animatic's 5 s board grid."""


def _music(commercial: bool = True, beats: list[float] | None = None) -> MusicBed:
    licence = "commercial" if commercial else "CC-BY-NC-4.0"
    return MusicBed(
        beats_s=beats or [],
        tempo_bpm=120.0 if beats else None,
        media_ref="/music/bed.wav",
        duration_s=40.0,
        end_card_s=5.0,
        plan_hash="sha256:" + "2" * 64,
        provenance=Provenance(
            source="generated", origin="musicgen", licence=licence, commercial=commercial
        ),
    )


def _mix(tmp_path: Path) -> Mix:
    return Mix(
        master=str(tmp_path / "mix.wav"),
        stems={"dx": "/dx.wav", "mx": "/mx.wav", "fx": "/fx.wav"},
        loudness_lufs=-14.0,
        true_peak_dbtp=-1.0,
        qa=[],
    )


def _dump(model: Any) -> JsonValue:
    out: JsonValue = model.model_dump(mode="json", by_alias=True)
    return out


class Rig:
    def __init__(self, tmp_path: Path, qa: FakeQA | None = None) -> None:
        private, _public = generate_keypair()
        self.fake = FakeFlamenco()
        self.qa = qa or FakeQA()
        self.tmp = tmp_path
        config = ds.DeviceShotConfig(
            flamenco_url="http://fake",
            shared=tmp_path / "shared",
            worker_tag=TAG,
            assets_dir=str(tmp_path / "a"),
        )
        self.inner_kinds: list[str] = []
        self.runner = fd.FinishingRunner(
            self._inner,
            all_departments(),
            config=config,
            client=self.fake,
            key=load_private_key(private),
            judge=Judge(),
            production_id="film",
            workdir=tmp_path / "finishing",
            options=ds.ExecutorOptions(clock=Ticks(), sleep=lambda _s: None, poll_interval=0.0),
            qa=self.qa,
        )
        kit = load_brand_kit(KIT).model_dump(mode="json")
        self.params: dict[str, JsonValue] = {ds.BRAND_KIT_PARAM: kit}

    def _inner(self, ctx: RunContext) -> RunResult:
        self.inner_kinds.append(ctx.spec.kind)
        return RunResult(output={"ok": True})

    def nodes(self, brief: Brief | None = None) -> dict[str, NodeSpec]:
        params = dict(self.params)
        params[BRIEF_PARAM] = _dump(brief or _brief())
        return {n.id: n for n in fd.finishing_nodes(params=params)}

    def call(self, spec: NodeSpec, inputs: dict[str, JsonValue]) -> RunResult:
        ctx = RunContext(spec=spec, tier=Tier.FINAL, key="k", inputs=inputs, attempt=1)
        return self.runner(ctx)

    def edit(
        self, nodes: dict[str, NodeSpec], *, music: bool = True, beats: list[float] | None = None
    ) -> JsonValue:
        inputs: dict[str, JsonValue] = {
            "animatic": _dump(_animatic()),
            "shots": _dump(_shots()),
            "voiceover": _dump(_vo()),
        }
        if music:
            inputs["music"] = _dump(_music(beats=beats))
        return self.call(nodes["edit"], inputs).output

    def render(self, nodes: dict[str, NodeSpec], beats: list[float] | None = None) -> RunResult:
        edit = self.edit(nodes, beats=beats)
        return self.call(nodes["render"], {"edit": edit, "mix": _dump(_mix(self.tmp))})

    def delivery(
        self,
        nodes: dict[str, NodeSpec],
        *,
        temp_vo: bool = False,
        commercial: bool = True,
        beats: list[float] | None = None,
    ) -> RunResult:
        master = self.render(nodes, beats).output
        inputs: dict[str, JsonValue] = {
            "render": master,
            "voiceover": _dump(_vo(temp_vo)),
            "mix": _dump(_mix(self.tmp)),
            "music": _dump(_music(commercial, beats)),
        }
        return self.call(nodes["delivery"], inputs)


def test_departments_are_registered_with_their_stage() -> None:
    depts = all_departments()
    for stage, cls in ((fd.EDIT, fd.Editor), (fd.RENDER, fd.Finisher), (fd.DELIVERY, fd.Delivery)):
        assert depts[STAGE_DEPARTMENTS[stage]] is cls
        assert stage in cls.produces
    assert list(fd.Editor.inputs) == [
        "animatic",
        "template_shots",
        "shots",
        "voiceover",
        "music",
        "references",
        "storyboard",
    ]
    assert list(fd.Finisher.inputs) == ["edit", "mix"]
    assert list(fd.Delivery.inputs) == ["render", "voiceover", "mix", "music"]
    assert fd.Finisher.uses_gpu and fd.Delivery.uses_gpu


def test_finishing_nodes_wire_inputs_from_the_given_ids() -> None:
    edit, render, delivery = fd.finishing_nodes("anim", "final", "vo", "bed", "mx", params={"p": 1})
    assert (edit.id, edit.kind, edit.inputs) == ("edit", "edit", ("anim", "final", "vo", "bed"))
    assert (render.id, render.inputs) == ("render", ("edit", "mx"))
    assert (delivery.id, delivery.inputs) == ("delivery", ("render", "vo", "mx", "bed"))
    assert edit.params == render.params == delivery.params == {"p": 1}
    e2, r2, d2 = fd.finishing_nodes(music_id=None, mix_id=None)
    assert e2.inputs == ("animatic", "shots", "voiceover")
    assert r2.inputs == ("edit",)
    assert d2.inputs == ("render", "voiceover")


def test_edit_conforms_the_animatic_to_the_rendered_shots(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    cut = fd.EditCut.model_validate(rig.edit(rig.nodes()))
    animatic = _animatic()
    before = animatic.tracks[0]
    after = next(t for t in cut.edit.tracks if t.id == "picture")
    shots = {s.shot_id: s for s in _shots().shots}
    for old, new in zip(before.clips(), after.clips(), strict=True):
        assert before.record_start(old.id) == after.record_start(new.id)
        assert old.duration == new.duration
        assert new.media_ref == shots[str(new.shot_id)].output_dir
        assert new.shot_hash == shots[str(new.shot_id)].recipe_hash
    vo = next(t for t in cut.edit.tracks if t.id == "vo")
    # line a: the animatic's first vo clip (frame 0); line b: the "cta" marker.
    assert [vo.record_start(c.id) for c in vo.clips()] == [0, 600]
    assert [c.media_ref for c in vo.clips()] == ["/vo/a.wav", "/vo/b.wav"]
    music = next(t for t in cut.edit.tracks if t.id == "music")
    assert [c.media_ref for c in music.clips()] == ["/music/bed.wav"]
    assert cut.edit.duration == animatic.duration
    assert rig.fake.submitted == []
    path = Path(cut.otio_ref)
    assert cut.otio_sha256 == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    back = read_otio(path)
    assert [c.shot_id for c in back.tracks[0].clips()] == SHOT_IDS


def test_edit_without_music_or_animatic(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    nodes = rig.nodes()
    cut = fd.EditCut.model_validate(rig.edit(nodes, music=False))
    assert "music" not in {t.id for t in cut.edit.tracks}
    with pytest.raises(ValueError, match="finishing_nodes"):
        rig.call(nodes["edit"], {"shots": _dump(_shots())})


def test_render_grades_every_shot_at_the_final_tier_with_the_end_card(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    result = rig.render(rig.nodes())
    master = fd.GradedMaster.model_validate(result.output)
    assert master.resolution == (1920, 1080)
    assert [s.shot_id for s in master.shots] == [*SHOT_IDS, fd.END_CARD_ID]
    assert master.shots[-1].template == "end-card"
    assert master.end_card.shot_id == fd.END_CARD_ID and master.end_card.passed
    assert master.end_card.copy_lines == ["Try it"]
    assert master.end_card.verdicts and all(v.ok for v in master.end_card.verdicts)
    assert len(rig.fake.submitted) == 7
    for recipe in rig.fake.recipes:
        tools = [s["tool"] for s in recipe["steps"]]
        assert "render.set_passes" in tools
        sizes = [s["args"] for s in recipe["steps"] if s["tool"] == "render.set_resolution"]
        assert sizes and all((a["width"], a["height"]) == (1920, 1080) for a in sizes)
    picture = master.edit.tracks[0]
    assert [c.shot_id for c in picture.clips()] == [*SHOT_IDS, fd.END_CARD_ID]
    assert picture.record_start(f"pic-{fd.END_CARD_ID}") == 6 * SHOT_FRAMES
    for clip, shot in zip(picture.clips(), master.shots, strict=True):
        assert clip.media_ref == shot.output_dir
    assert result.usage.gpu_minutes > 0
    assert result.usage.ai_usage[ds.VISION_QA_METRIC] == 7


def test_a_forbidden_word_in_the_cta_fails_before_any_submit(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    nodes = rig.nodes(_brief("A revolutionary dashboard"))
    edit = rig.edit(nodes)
    with pytest.raises(ValueError, match="forbidden word"):
        rig.call(nodes["render"], {"edit": edit})
    assert rig.fake.submitted == []


def test_delivery_writes_the_master_and_eight_qad_cutdowns(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    result = rig.delivery(rig.nodes())
    report = fd.DeliveryReport.model_validate(result.output)
    assert report.passed and report.temp_items == []
    assert report.master.kind == "master" and report.master.aspect == "16:9"
    assert report.master.resolution == (1920, 1080)
    combos = {(d.length_s, d.aspect) for d in report.cutdowns}
    assert combos == {(n, a) for n in (15, 30) for a in ("16:9", "9:16", "1:1", "4:5")}
    for d in (report.master, *report.cutdowns):
        assert d.qa_passed and d.resolution == RESOLUTIONS[d.aspect]
        path = Path(d.otio_ref)
        assert d.otio_sha256 == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        edit = read_otio(path)
        if d.kind == "cutdown":
            assert d.length_s is not None
            assert edit.duration == edit.rate.frames(int(d.length_s))
            clips = edit.tracks[0].clips()
            assert clips[0].shot_id == "s0" and clips[-1].shot_id == fd.END_CARD_ID
    # 7 graded 16:9 shots, then 7 shots re-rendered for each of 9:16, 1:1, 4:5.
    assert len(rig.fake.submitted) == 7 + 21
    sizes = {
        (a["width"], a["height"])
        for r in rig.fake.recipes[7:]
        for s in r["steps"]
        if s["tool"] == "render.set_resolution"
        for a in [s["args"]]
    }
    assert sizes == {RESOLUTIONS["9:16"], RESOLUTIONS["1:1"], RESOLUTIONS["4:5"]}
    calls = {c["render_id"]: c for c in rig.qa.calls}
    assert len(calls) == 9
    assert calls["film-master"]["audio"] == tmp_path / "mix.wav"
    assert calls["film-cutdown-15s-9x16"]["platform"] == "tiktok"
    assert calls["film-cutdown-30s-4x5"]["aspect"] == "4:5"
    assert result.usage.gpu_minutes > 0
    assert "bpy" not in sys.modules


def test_a_failing_deliverable_fails_the_node(tmp_path: Path) -> None:
    rig = Rig(tmp_path, FakeQA(fail="cutdown-15s-1x1"))
    with pytest.raises(ValueError, match="delivery QA failed: cutdown-15s-1x1"):
        rig.delivery(rig.nodes())


def test_temp_vo_and_non_commercial_music_are_listed_and_never_pass(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    report = fd.DeliveryReport.model_validate(
        rig.delivery(rig.nodes(), temp_vo=True, commercial=False).output
    )
    kinds = {(t.node, t.kind) for t in report.temp_items}
    assert kinds == {("voiceover", "temp-vo"), ("music", "non-commercial")}
    assert not report.passed


def test_other_kinds_pass_through_to_the_inner_runner(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    spec = NodeSpec(id="script", kind="script", department="writer")
    assert rig.call(spec, {}).output == {"ok": True}
    assert rig.inner_kinds == ["script"]


def test_cutdown_shots_hook_first_end_card_last() -> None:
    edit = _animatic()
    assert fd.cutdown_shots(edit, "s5", 15) == ["s0", "s1", "s5"]
    with pytest.raises(ValueError, match="longer master"):
        fd.cutdown_shots(edit, "s5", 60)


def test_edit_names_the_scene_builder_for_a_board_it_has_not_rendered(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    nodes = rig.nodes()
    shots = _shots()
    held = shots.model_copy(
        update={
            "shots": shots.shots[1:],
            "scene_builder": [SceneBuilderShot(shot_id=SHOT_IDS[0], description="slow orbit")],
        }
    )
    inputs: dict[str, JsonValue] = {
        "animatic": _dump(_animatic()),
        "shots": _dump(held),
        "voiceover": _dump(_vo()),
    }
    with pytest.raises(ValueError, match="planned for the scene builder"):
        rig.call(nodes["edit"], inputs)
    assert rig.fake.submitted == []


def test_edit_cuts_on_the_music_beats_and_keeps_vo_and_length(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    cut = fd.EditCut.model_validate(rig.edit(rig.nodes(), beats=BEATS))
    assert cut.editorial is not None
    beat_frames = {round(b * 24) for b in BEATS}
    picture = next(t for t in cut.edit.tracks if t.id == "picture")
    starts = [picture.record_start(c.id) for c in picture.clips()[1:]]
    assert all(min(abs(s - b) for b in beat_frames) <= 1 for s in starts)
    assert starts != [SHOT_FRAMES * n for n in range(1, len(SHOT_IDS))]  # really moved
    assert check_editorial(cut.edit, cut.editorial) == []
    assert cut.edit.duration == _animatic().duration
    vo = next(t for t in cut.edit.tracks if t.id == "vo")
    assert [vo.record_start(c.id) for c in vo.clips()] == [0, 600]
    for clip in vo.clips():
        assert clip.link is not None and abs(cut.edit.audio_offset(clip.id)) <= 12


def test_delivery_re_paces_the_cutdowns_on_the_beat(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    report = fd.DeliveryReport.model_validate(rig.delivery(rig.nodes(), beats=BEATS).output)
    assert len(report.cutdowns) == 8 and all(d.qa_passed for d in report.cutdowns)
    per_shot: dict[float, list[int]] = {}
    for d in report.cutdowns:
        assert d.length_s is not None
        edit = read_otio(Path(d.otio_ref))
        clips = edit.tracks[0].clips()
        assert edit.duration == edit.rate.frames(int(d.length_s))
        assert clips[0].shot_id == "s0" and clips[-1].shot_id == fd.END_CARD_ID
        assert all(c.duration % 12 == 0 for c in clips[:-1])  # whole beats, end card closes
        per_shot[d.length_s] = [c.duration for c in clips[:-1]]
    assert per_shot[15][0] < per_shot[30][0]
