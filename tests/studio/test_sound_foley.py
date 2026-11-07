# SPDX-License-Identifier: AGPL-3.0-or-later
"""Foley in sound design (#316): every picture clip with real media is rendered on its own
and a video-to-audio model hears it; no picture renderer means no foley."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from spectracut.cli import studio as studio_mod
from spectracut.edit.timeline import Clip, Edit, Track
from spectracut.providers.local.registry import MockLocalProvider
from spectracut.studio.departments.sound import (
    SOUND_DEPARTMENTS,
    SOUND_DESIGN,
    SoundDesign,
    SoundRunner,
    sound_nodes,
)
from spectracut.studio.graph import RunContext, RunResult
from spectracut.studio.node import Tier

FPS = 24
BOUNDS = (0, 48, 96, 192)


@pytest.fixture(autouse=True)
def _mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPECTRACUT_PROVIDERS", "mock")


class SpyFoley:
    """The foley model: records each call's options and the picture it was given."""

    name = "spy-foley"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @contextmanager
    def batch(self) -> Iterator[None]:
        yield

    def generate(self, prompt: str, out_path: Path, **options: Any) -> Path:
        video = Path(options["video_path"])
        self.calls.append({"prompt": prompt, **options, "movie": video.read_text()})
        return MockLocalProvider("foley").generate(prompt, out_path, **options)


class Pictures:
    """The picture renderer: writes the one clip it was given as the movie's text."""

    def __init__(self) -> None:
        self.edits: list[Edit] = []

    def __call__(self, edit: Edit, out: Path) -> Path:
        self.edits.append(edit)
        (clip,) = [c for t in edit.tracks for c in t.clips()]
        out.write_text(f"{clip.id}:{clip.media_ref}")
        return out


def _edit(tmp: Path, real: tuple[int, ...]) -> Edit:
    picture = []
    for n, (a, b) in enumerate(zip(BOUNDS, BOUNDS[1:], strict=False)):
        ref = f"board:s{n}"
        if n in real:
            png = tmp / f"s{n}.png"
            png.write_bytes(b"png")
            ref = str(png)
        picture.append(Clip(id=f"pic-s{n}", shot_id=f"s{n}", media_ref=ref, source_out=b - a))
    return Edit(id="animatic", tracks=[Track(id="picture", kind="video", items=picture)])


def _design(
    tmp: Path, edit: Edit, picture: Pictures | None, foley: SpyFoley
) -> tuple[SoundDesign, RunResult]:
    runner = SoundRunner(
        lambda ctx: RunResult(output={}),
        {d.name: d for d in SOUND_DEPARTMENTS},
        workdir=tmp / "work",
        providers={"foley": foley},  # type: ignore[dict-item]
        picture=picture,
    )
    spec = next(n for n in sound_nodes() if n.id == SOUND_DESIGN)
    inputs: dict[str, JsonValue] = {spec.inputs[0]: edit.model_dump(mode="json")}
    result = runner(RunContext(spec=spec, tier=Tier.PREVIEW, key="k", inputs=inputs, attempt=1))
    return SoundDesign.model_validate(result.output), result


def test_sound_design_adds_a_foley_cue_per_picture_clip_with_real_media(tmp_path: Path) -> None:
    edit = _edit(tmp_path, real=(0, 2))
    pictures, foley = Pictures(), SpyFoley()
    design, result = _design(tmp_path, edit, pictures, foley)

    # the placeholder board (s1) has no picture to hear; s0 and s2 are rendered alone
    assert [[c.id for t in e.tracks for c in t.clips()] for e in pictures.edits] == [
        ["pic-s0"],
        ["pic-s2"],
    ]
    assert all(e.rate == edit.rate for e in pictures.edits)
    foley_cues = [c for c in design.recipe.cues if c.id.startswith("foley-")]
    assert [(c.id, c.bus, c.frame) for c in foley_cues] == [
        ("foley-pic-s0", "fx", 0),
        ("foley-pic-s2", "fx", 96),
    ]
    assert [c.layers[0].sound.kind for c in foley_cues] == ["foley", "foley"]
    assert [c.layers[0].sound.duration_seconds for c in foley_cues] == [2.0, 4.0]
    # one candidate each, each hearing its own shot's movie
    assert [c["movie"] for c in foley.calls] == [
        f"pic-s0:{tmp_path / 's0.png'}",
        f"pic-s2:{tmp_path / 's2.png'}",
    ]
    assert [c["duration_s"] for c in foley.calls] == [2.0, 4.0]
    rows = {r.cue_id: r for r in design.cues if r.cue_id.startswith("foley-")}
    assert set(rows) == {"foley-pic-s0", "foley-pic-s2"}
    assert {r.frame for r in rows.values()} == {0, 96}
    for row in rows.values():
        assert row.provenance.origin.startswith("spy-foley@")
        assert Path(row.media_ref).is_file()
    # the sfx cues on the cuts are still there
    assert {c.id for c in design.recipe.cues} - set(rows) and result.usage.gpu_minutes >= 0


def test_no_picture_renderer_means_no_foley(tmp_path: Path) -> None:
    edit = _edit(tmp_path, real=(0, 1, 2))
    foley = SpyFoley()
    design, _ = _design(tmp_path, edit, None, foley)
    assert foley.calls == []
    assert not [c for c in design.recipe.cues if c.id.startswith("foley-")]
    assert all(c.layers[0].sound.kind != "foley" for c in design.recipe.cues)


def test_placeholder_boards_render_no_picture_and_request_no_foley(tmp_path: Path) -> None:
    pictures, foley = Pictures(), SpyFoley()
    design, _ = _design(tmp_path, _edit(tmp_path, real=()), pictures, foley)
    assert pictures.edits == [] and foley.calls == []
    assert not [c for c in design.recipe.cues if c.id.startswith("foley-")]


def test_default_sound_deps_render_the_picture_with_the_animatic_encoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = Pictures()
    monkeypatch.setattr(studio_mod, "animatic_preview_factory", lambda: sentinel)

    class Production:
        def model_runtime(self) -> tuple[None, str]:
            return None, "no host"

    deps = studio_mod.default_sound(Production())  # type: ignore[arg-type]
    assert deps.picture is sentinel and deps.runtime_problem == "no host"
    assert studio_mod.SoundDeps().picture is None
