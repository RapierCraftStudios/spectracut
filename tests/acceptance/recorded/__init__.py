# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recorded providers and fakes for the honest M2 acceptance run (#253).

:class:`RecordedProvider` replays one committed JSON response per department output
(``<schema title>.json`` beside this file) and reports Claude usage, as the signed-in
Claude CLI does. :func:`fake_farm` is a Flamenco Manager whose jobs complete at once and
:func:`fake_sound` stands in for the local audio stack (Ardour, the time stretcher).
:func:`run_film` drives ``studio new``/``studio run``/``studio approve`` through the
CLI, so every node is built by ``default_runner`` and the real departments, and the
session fixture :func:`launch_film` runs the AlterLab brief that way once (import it
into a test module to use it).
"""

from __future__ import annotations

import json
import math
import re
from array import array
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from spectracut.audio.ardour import BUILD_OK, EXPORT_OK, SOURCE_LINE
from spectracut.audio.render import Channels, write_wav
from spectracut.audio.vo import VoiceOver
from spectracut.cli import studio as studio_mod
from spectracut.cli.app import app
from spectracut.critique.critic import CRITIC_REQUESTS
from spectracut.providers.base import Completion, VisionScore
from spectracut.providers.elevenlabs import MockElevenLabs
from spectracut.qa.audio import integrated_loudness
from spectracut.queue.flamenco import WorkerTag
from spectracut.queue.signing import generate_keypair, load_private_key
from spectracut.studio import device_shot as ds
from spectracut.studio.departments.sound import DEFAULT_VOICE
from spectracut.studio.graph import ProductionGraph
from spectracut.studio.node import NodeStatus
from spectracut.studio.runner import CONTEXT_METRIC
from spectracut.studio.store import ProductionStore

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BRIEF = ROOT / "productions" / "alterlab-launch" / "brief.json"
KIT = ROOT / "brands" / "alterlab" / "brand-kit.yaml"
TAG = "3070laptopgpu"


class RecordedProvider:
    """Replays the recorded response for each requested output schema (by its title)."""

    name = "claude-recorded"

    def __init__(self, recorded: Path = HERE) -> None:
        self.recorded = recorded
        self.calls: list[str] = []

    def complete(self, system: str, prompt: str, *, schema: dict[str, Any]) -> Completion:
        title = str(schema.get("title", ""))
        path = self.recorded / f"{title}.json"
        if not path.is_file():
            raise AssertionError(f"no recorded response for schema {title!r} ({path})")
        self.calls.append(title)
        output = json.loads(path.read_text(encoding="utf-8"))
        usage = {
            "claude_requests": 1,
            "claude_input_tokens": (len(system) + len(prompt)) // 4,
            "claude_output_tokens": len(json.dumps(output)) // 4,
        }
        return Completion(output=output, usage=usage)


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


class PassingJudge:
    name = "mock"
    last_usage: dict[str, int] = {}

    def score(self, frame_path: Path, rubric: str) -> VisionScore:
        return VisionScore(score=0.9)


class Ticks:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 90.0
        return self.t


def fake_farm(
    shared: Path, client: FakeFlamenco
) -> Callable[[studio_mod.Production], studio_mod.DeviceShotDeps]:
    """A ``device_shot_factory`` on ``client``."""
    private, _public = generate_keypair()

    def factory(production: studio_mod.Production) -> studio_mod.DeviceShotDeps:
        config = ds.DeviceShotConfig(
            flamenco_url="http://fake",
            shared=shared / "shared",
            worker_tag=TAG,
            assets_dir=str(shared / "assets"),
        )
        return studio_mod.DeviceShotDeps(
            config=config,
            client=client,
            key=load_private_key(private),
            judge=PassingJudge(),
            options=ds.ExecutorOptions(clock=Ticks(), sleep=lambda _s: None, poll_interval=0.0),
        )

    return factory


class PassThrough:
    def stretch(self, channels: Channels, rate: int, ratio: float) -> Channels:
        return channels


class FakeArdour:
    """Stands in for ardour8-lua: a placeholder per layer, then a -14 LUFS master."""

    rate = 8000

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
                    write_wav(
                        audiofiles / file,
                        [array("f", bytes(4 * int(length)))],
                        sr,
                        float_format=True,
                    )
                    lines.append(f"{SOURCE_LINE}\t{name}\t{ch}\t{file}")
            return "\n".join([*lines, BUILD_OK])
        n = self.rate * 2
        tone = [math.sin(2 * math.pi * 997 * i / self.rate) for i in range(n)]
        gain = 10 ** ((-14.0 - integrated_loudness(self.rate, [tone, tone])) / 20)
        master = array("f", (x * gain for x in tone))
        for name in ("master", "dx", "mx", "fx"):
            chan = master if name == "master" else array("f", bytes(4 * n))
            write_wav(
                workdir / "export" / name / "mix.wav", [chan, chan], self.rate, float_format=False
            )
        return "SPECTRACUT_ROUTING_OK\nSPECTRACUT_AUTOMATION_OK\n" + EXPORT_OK


class CountingVoice(MockElevenLabs):
    """The ElevenLabs stand-in: an offline voice that records every line it speaks."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def synthesize(
        self, text: str, voice: str, settings: Mapping[str, Any], out_path: Path
    ) -> Path:
        self.calls.append(text)
        return super().synthesize(text, voice, settings, out_path)


def fake_sound(
    voice: CountingVoice,
) -> Callable[[studio_mod.Production], studio_mod.SoundDeps]:
    """A ``sound_factory``: final VO through ``voice``, a fake Ardour and stretcher."""

    def factory(production: studio_mod.Production) -> studio_mod.SoundDeps:
        workdir = production.store.path(production.id) / "sound" / "vo"
        return studio_mod.SoundDeps(
            voice_over=VoiceOver(workdir, voice=DEFAULT_VOICE, final_provider=voice),
            elevenlabs_configured=lambda: True,
            ardour=FakeArdour(),  # type: ignore[arg-type]
            stretcher=PassThrough(),
        )

    return factory


INTERNAL_METRICS = frozenset(
    {
        CONTEXT_METRIC,
        CRITIC_REQUESTS,
        ds.VISION_QA_METRIC,
        ds.VISION_QA_INPUT_TOKENS,
        ds.VISION_QA_OUTPUT_TOKENS,
        "image_exploration_requests",
    }
)
"""Service-neutral ai_usage metrics the pipeline itself counts (prompt context size,
critic, vision-QA and image-exploration calls), not usage of an external service."""


def external_ai_usage(ai_usage: Mapping[str, int]) -> dict[str, int]:
    """The metrics of ``ai_usage`` that name an external service."""
    return {k: v for k, v in ai_usage.items() if k not in INTERNAL_METRICS}


@dataclass
class Film:
    """The settled launch film and the fakes it ran on."""

    home: Path
    store: ProductionStore
    graph: ProductionGraph
    outputs: list[str]
    rerun: str
    """Output of one more ``studio run`` after the film settled."""
    ledger_entries: int
    """Ledger entries before that run."""
    provider: RecordedProvider
    farm: FakeFlamenco
    voice: CountingVoice


def invoke(args: Iterable[str]) -> str:
    result = CliRunner().invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def run_film(home: Path, production_id: str, *, brief: Path = BRIEF, kit: Path = KIT) -> list[str]:
    """``studio new`` the standard production (unless it exists, so a live run resumes),
    then run it, approving each owner gate as it is reached, until nothing is waiting.
    Returns each ``studio run``'s output."""
    store = ProductionStore(home)
    if not store.exists(production_id):
        invoke(
            [
                "studio", "new", str(brief),
                "--profile", "standard",
                "--brand-kit", str(kit),
                "--id", production_id,
                "--home", str(home),
            ]
        )  # fmt: skip
    outputs: list[str] = []
    graph, _ = store.load(production_id)
    for _ in range(2 * len(graph.order()) + 2):
        outputs.append(invoke(["studio", "run", production_id, "--home", str(home)]))
        graph, _ = store.load(production_id)
        statuses = {n: graph.status(n) for n in graph.order()}
        failed = {n: graph.state(n).error for n, s in statuses.items() if s is NodeStatus.FAILED}
        assert not failed, (failed, outputs[-1])
        waiting = [n for n, s in statuses.items() if s is NodeStatus.AWAITING_APPROVAL]
        if not waiting:
            return outputs
        for nid in waiting:
            invoke(["studio", "approve", production_id, nid, "--home", str(home)])
    raise AssertionError(f"{production_id} never settled: {outputs[-1]}")


LAUNCH_ID = "alterlab-launch"


@pytest.fixture(scope="session")
def launch_film(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Film]:
    """``studio new`` + ``studio run``/``approve`` of the AlterLab brief to delivery.

    Only the external dependencies are injected (the text provider, the Flamenco farm and
    the audio stack); every node is built by ``default_runner`` and the real departments.
    """
    tmp = tmp_path_factory.mktemp("launch-film")
    home = tmp / "home"
    provider, farm, voice = RecordedProvider(), FakeFlamenco(), CountingVoice()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SPECTRACUT_HOME", str(home))
        mp.setenv("SPECTRACUT_CONFIG", str(tmp / "config.toml"))
        mp.setenv("SPECTRACUT_PROVIDERS", "mock")
        mp.delenv("ELEVENLABS_API_KEY", raising=False)
        mp.setattr("spectracut.qa.copy_ocr.available_reader", lambda: None)
        mp.setattr(studio_mod, "text_provider_factory", lambda: provider)
        mp.setattr(studio_mod, "device_shot_factory", fake_farm(tmp, farm))
        mp.setattr(studio_mod, "sound_factory", fake_sound(voice))
        outputs = run_film(home, LAUNCH_ID)
        store = ProductionStore(home)
        settled, _ = store.load(LAUNCH_ID)
        rerun = invoke(["studio", "run", LAUNCH_ID, "--home", str(home)])
        graph, _ = store.load(LAUNCH_ID)
        yield Film(home, store, graph, outputs, rerun, len(settled.ledger), provider, farm, voice)
