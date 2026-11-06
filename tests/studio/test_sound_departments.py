# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sound departments: voiceover, sound design, music and the mix (#251)."""

from __future__ import annotations

import math
import re
import urllib.request
from array import array
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from spectracut.audio.ardour import BUILD_OK, EXPORT_OK, SOURCE_LINE
from spectracut.audio.events import edit_events
from spectracut.audio.render import Channels, read_wav, write_wav
from spectracut.audio.vo import VoiceOver
from spectracut.edit.timeline import Clip, Edit, Marker, Track
from spectracut.providers.elevenlabs import MockElevenLabs
from spectracut.qa.audio import integrated_loudness
from spectracut.studio import GateMode, GatePolicy, NodeSpec, ProductionGraph
from spectracut.studio.departments import STAGE_DEPARTMENTS, all_departments
from spectracut.studio.departments.sound import (
    MIX,
    MUSIC,
    SOUND_DEPARTMENTS,
    SOUND_DESIGN,
    VOICEOVER,
    Mix,
    MusicBed,
    SoundDesign,
    SoundRunner,
    VoiceoverTrack,
    is_temp_vo,
    sound_nodes,
)
from spectracut.studio.graph import RunContext, RunResult
from spectracut.studio.node import Tier

FPS = 24
SECONDS = 8
CUTS = (48, 96)
END_CARD = 144  # 6 s: the music's final hit lands here
PICKUP_S = 0.25

SCRIPT: dict[str, JsonValue] = {
    "concept_id": "launch",
    "lines": [
        {"beat": "hook", "vo": "Meet AlterLab."},
        {"beat": "silent", "on_screen": "AlterLab"},
        {"beat": "proof", "vo": "Your lab, rebuilt for speed."},
        {"beat": "cta", "vo": "Try it today."},
    ],
}
VO_TEXTS = ["Meet AlterLab.", "Your lab, rebuilt for speed.", "Try it today."]


def _edit() -> Edit:
    bounds = (0, *CUTS, SECONDS * FPS)
    picture = [
        Clip(id=f"pic-s{n}", shot_id=f"s{n}", media_ref=f"board:s{n}", source_out=b - a)
        for n, (a, b) in enumerate(zip(bounds, bounds[1:], strict=False))
    ]
    voice = [
        Clip(id=f"vo-s{n}", media_ref=f"temp:{n}", source_out=b - a, link=f"pic-s{n}")
        for n, (a, b) in enumerate(zip(bounds, bounds[1:], strict=False))
    ]
    return Edit(
        id="animatic",
        tracks=[
            Track(id="picture", kind="video", items=picture),
            Track(id="vo", kind="audio", items=voice),
        ],
        markers=[Marker(name="end-card", frame=END_CARD)],
    )


EDIT: dict[str, JsonValue] = _edit().model_dump(mode="json")


class FakeClock:
    """Each reading is 30 s after the last, so every metered session is 0.5 min."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 30.0
        return self.now


class CountingVoice(MockElevenLabs):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def synthesize(
        self, text: str, voice: str, settings: Mapping[str, Any], out_path: Path
    ) -> Path:
        self.calls.append(text)
        return super().synthesize(text, voice, settings, out_path)


class ClickMusic:
    """Local music model stand-in: a 0.25 s pickup, then 120 BPM kicks, a big hit
    three bars in (6.25 s) and a ring-out, so the cut needs real bar edits."""

    name = "click-music"
    rate = 8000

    @contextmanager
    def batch(self) -> Iterator[None]:
        yield

    def generate(self, prompt: str, out_path: Path, **options: Any) -> Path:
        n = int(float(options["duration_s"]) * self.rate)
        data = array("f", bytes(4 * n))
        for k in range(12):  # beats every 0.5 s up to the hit
            at = round((PICKUP_S + k * 0.5) * self.rate)
            self._kick(data, at, 0.9 if k % 4 == 0 else 0.4, 0.06)
        self._kick(data, round((PICKUP_S + 6.0) * self.rate), 1.0, 0.45)
        write_wav(Path(out_path), [data], self.rate, float_format=True)
        return Path(out_path)

    def _kick(self, data: array[float], start: int, amp: float, decay_s: float) -> None:
        for i in range(min(len(data) - start, round(decay_s * 3 * self.rate))):
            data[start + i] += (
                amp
                * math.exp(-i / (decay_s * self.rate))
                * math.sin(2 * math.pi * 90 * i / self.rate)
            )


class PassThrough:
    def stretch(self, channels: Channels, rate: int, ratio: float) -> Channels:
        return channels


class FakeArdour:
    """Stands in for ardour8-lua: the placeholder of every layer the build script
    creates (as tests/audio/test_recipe.py does), then a master at ``lufs``."""

    rate = 8000

    def __init__(self, lufs: float = -14.0) -> None:
        self.lufs = lufs

    def run(self, script: Path, workdir: Path, xdg: Path, marker: str) -> str:
        if marker == BUILD_OK:
            audiofiles = workdir / "session" / "interchange" / "session" / "audiofiles"
            audiofiles.mkdir(parents=True)
            lua = script.read_text(encoding="utf-8")
            match = re.search(r"create_session\(.*, (\d+)\)", lua)
            assert match is not None
            sr = int(match.group(1))
            lines = []
            for name, length in re.findall(r'new_layer\("([^"]+)", "[^"]*", \d+, (\d+),', lua):
                for ch, side in enumerate("LR"):
                    file = f"ph_{name}%{side}.wav"
                    silent = array("f", bytes(4 * int(length)))
                    write_wav(audiofiles / file, [silent], sr, float_format=True)
                    lines.append(f"{SOURCE_LINE}\t{name}\t{ch}\t{file}")
            return "\n".join([*lines, BUILD_OK])
        n = self.rate * 2
        tone = [math.sin(2 * math.pi * 997 * i / self.rate) for i in range(n)]
        gain = 10 ** ((self.lufs - integrated_loudness(self.rate, [tone, tone])) / 20)
        master = array("f", (x * gain for x in tone))
        for name in ("master", "dx", "mx", "fx"):
            chan = master if name == "master" else array("f", bytes(4 * n))
            write_wav(
                workdir / "export" / name / "mix.wav", [chan, chan], self.rate, float_format=False
            )
        return "SPECTRACUT_ROUTING_OK\nSPECTRACUT_AUTOMATION_OK\n" + EXPORT_OK


class Stub:
    """The script and the animatic come from earlier departments; everything else is sound."""

    def __init__(self, sound: SoundRunner) -> None:
        self.sound = sound

    def __call__(self, ctx: RunContext) -> RunResult:
        if ctx.spec.kind == "script":
            return RunResult(output=SCRIPT)
        if ctx.spec.kind == "animatic":
            return RunResult(output=EDIT)
        return self.sound(ctx)


def _unexpected(ctx: RunContext) -> RunResult:
    raise AssertionError(f"inner runner called for {ctx.spec.kind}")


@pytest.fixture(autouse=True)
def _mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPECTRACUT_PROVIDERS", "mock")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    def offline(*args: object, **kwargs: object) -> None:
        raise AssertionError("no network: the sound departments reached out")

    monkeypatch.setattr(urllib.request, "urlopen", offline)


def _runner(tmp: Path, *, configured: bool, lufs: float = -14.0) -> tuple[SoundRunner, Any]:
    voice = CountingVoice()
    runner = SoundRunner(
        _unexpected,
        {d.name: d for d in SOUND_DEPARTMENTS},
        workdir=tmp / "work",
        voice_over=VoiceOver(tmp / "vo", voice="narrator", final_provider=voice),
        elevenlabs_configured=lambda: configured,
        providers={"music": ClickMusic()},  # type: ignore[dict-item]
        ardour=FakeArdour(lufs),  # type: ignore[arg-type]
        stretcher=PassThrough(),
        clock=FakeClock(),
    )
    return runner, voice


def _ctx(node_id: str, inputs: dict[str, JsonValue]) -> RunContext:
    spec = next(n for n in sound_nodes() if n.id == node_id)
    return RunContext(spec=spec, tier=Tier.FINAL, key="k", inputs=inputs, attempt=1)


# ---- R1/R4: voiceover --------------------------------------------------------------


def test_voiceover_with_elevenlabs_renders_every_line_as_final(tmp_path: Path) -> None:
    runner, voice = _runner(tmp_path, configured=True)
    result = runner(_ctx(VOICEOVER, {"script": SCRIPT}))
    track = VoiceoverTrack.model_validate(result.output)
    assert [line.text for line in track.lines] == VO_TEXTS
    assert [line.beat for line in track.lines] == ["hook", "proof", "cta"]
    assert voice.calls == VO_TEXTS
    assert track.provider == "elevenlabs" and not track.temp_vo and track.mark is None
    assert not is_temp_vo(result.output)
    assert result.usage.elevenlabs_characters == sum(len(t) for t in VO_TEXTS)
    assert all(Path(line.media_ref).is_file() and line.duration_s > 0 for line in track.lines)


def test_voiceover_without_a_key_is_local_temp_vo(tmp_path: Path) -> None:
    runner, voice = _runner(tmp_path, configured=False)
    result = runner(_ctx(VOICEOVER, {"script": SCRIPT}))
    track = VoiceoverTrack.model_validate(result.output)
    assert voice.calls == []  # temp VO never reaches ElevenLabs
    assert track.provider == "local" and track.temp_vo and track.mark == "temp-vo"
    assert is_temp_vo(result.output)
    assert result.usage.elevenlabs_characters == 0
    assert result.usage.gpu_minutes == pytest.approx(0.5)
    assert len(track.lines) == 3


# ---- R2: sound design and music, locally, on the edit's timing ---------------------


def test_sound_design_cues_land_on_the_cuts_and_are_generated_locally(tmp_path: Path) -> None:
    runner, voice = _runner(tmp_path, configured=True)
    result = runner(_ctx(SOUND_DESIGN, {"animatic": EDIT}))
    design = SoundDesign.model_validate(result.output)
    cut_frames = sorted(e.frame for e in edit_events(_edit()) if e.kind == "cut")
    assert cut_frames == list(CUTS)
    frames = [c.frame for c in design.recipe.cues]
    assert all(f is not None for f in frames)  # placed on edit frames, not markers
    assert sorted({f for f in frames if f is not None}) == list(CUTS)
    starts = {c.id: (c.frame or 0) + c.offset_frames for c in design.recipe.cues}
    assert design.cues and {c.frame for c in design.cues} == set(starts.values())
    for cue in design.cues:
        assert cue.provenance.source == "generated"
        assert cue.provenance.origin.startswith("mock-sfx@")
        assert Path(cue.media_ref).is_file()
    assert voice.calls == []  # no ElevenLabs, no network
    assert result.usage.gpu_minutes > 0 and result.usage.elevenlabs_characters == 0


def test_music_is_cut_to_the_edit_length_and_end_card(tmp_path: Path) -> None:
    runner, voice = _runner(tmp_path, configured=True)
    result = runner(_ctx(MUSIC, {"animatic": EDIT}))
    music = MusicBed.model_validate(result.output)
    rate, channels = read_wav(Path(music.media_ref))
    assert abs(len(channels[0]) / rate - SECONDS) <= 1 / FPS
    assert music.duration_s == pytest.approx(SECONDS)
    assert music.end_card_s == pytest.approx(END_CARD / FPS)
    assert music.provenance.source == "generated" and "click-music" in music.provenance.origin
    assert voice.calls == [] and result.usage.gpu_minutes > 0


# ---- R3: stems and a loudness-checked mix ------------------------------------------


def _mix_inputs(tmp: Path) -> dict[str, JsonValue]:
    runner, _ = _runner(tmp / "prep", configured=True)
    return {
        "animatic": EDIT,
        VOICEOVER: runner(_ctx(VOICEOVER, {"script": SCRIPT})).output,
        SOUND_DESIGN: runner(_ctx(SOUND_DESIGN, {"animatic": EDIT})).output,
        MUSIC: runner(_ctx(MUSIC, {"animatic": EDIT})).output,
    }


def test_mix_renders_master_and_stems_and_passes_streaming_loudness(tmp_path: Path) -> None:
    runner, _ = _runner(tmp_path, configured=True)
    mix = Mix.model_validate(runner(_ctx(MIX, _mix_inputs(tmp_path))).output)
    assert Path(mix.master).is_file()
    assert set(mix.stems) == {"dx", "mx", "fx"}
    assert all(Path(p).is_file() for p in mix.stems.values())
    assert mix.delivery == "streaming"
    assert mix.loudness_lufs == pytest.approx(-14.0, abs=0.5)
    assert mix.true_peak_dbtp is not None and mix.true_peak_dbtp <= -1.0
    rows = {row.name: row.status for row in mix.qa}
    assert rows == {"audio.loudness": "pass", "audio.true_peak": "pass", "audio.clipping": "pass"}


@pytest.mark.parametrize("lufs", [-30.0, -4.0])
def test_a_mix_outside_streaming_loudness_fails(tmp_path: Path, lufs: float) -> None:
    runner, _ = _runner(tmp_path, configured=True, lufs=lufs)
    with pytest.raises(ValueError, match="mix fails audio QA: audio.loudness"):
        runner(_ctx(MIX, _mix_inputs(tmp_path)))


# ---- R4/R7: the node builder in a production graph ---------------------------------


def _graph(tmp: Path, *, configured: bool) -> ProductionGraph:
    nodes = [
        NodeSpec(id="script", kind="script", department="stub"),
        NodeSpec(id="animatic", kind="animatic", department="stub", inputs=("script",)),
        *sound_nodes(),
    ]
    gates = {n.kind: GateMode.AUTO for n in nodes}
    graph = ProductionGraph("launch", gates=GatePolicy(defaults=gates))
    graph.add_nodes(nodes)
    runner, _ = _runner(tmp, configured=configured)
    report = graph.run(Stub(runner))
    assert report.failed == [], {n: graph.state(n).error for n in report.failed}
    return graph


def test_sound_nodes_record_characters_and_gpu_minutes_in_the_ledger(tmp_path: Path) -> None:
    graph = _graph(tmp_path, configured=True)
    ledger = {e.node_id: e for e in graph.ledger.entries()}
    assert {VOICEOVER, SOUND_DESIGN, MUSIC, MIX} <= set(ledger)
    assert ledger[VOICEOVER].elevenlabs_characters == sum(len(t) for t in VO_TEXTS)
    assert ledger[SOUND_DESIGN].gpu_minutes > 0
    assert ledger[MUSIC].gpu_minutes > 0
    assert all(ledger[n].outcome == "accepted" for n in (VOICEOVER, SOUND_DESIGN, MUSIC, MIX))

    temp = _graph(tmp_path / "temp", configured=False)
    entry = next(e for e in temp.ledger.entries() if e.node_id == VOICEOVER)
    assert entry.elevenlabs_characters == 0 and entry.gpu_minutes > 0
    assert is_temp_vo(temp.output(VOICEOVER))


def test_sound_nodes_declare_the_inputs_their_departments_accept(tmp_path: Path) -> None:
    graph = _graph(tmp_path, configured=True)
    registry = all_departments()
    for node in sound_nodes():
        dept = registry[node.department]
        assert STAGE_DEPARTMENTS[node.kind] == dept.name and node.kind in dept.produces
        assert not dept.uses_gpu  # the CLI runner has no GPU leases (runner.py)
        kinds = {i: graph.spec(i).kind for i in node.inputs}
        dept.validate_inputs(node.id, {i: graph.output(i) for i in node.inputs}, kinds)
        assert set(kinds.values()) == set(dept.inputs)
