# SPDX-License-Identifier: AGPL-3.0-or-later
"""Surge XT rendering through Ardour: program construction (no tools) and a gated e2e."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from spectracut.audio.cues import PROCEDURAL
from spectracut.audio.procedural import SurgeXTGenerator, build_surge_lua
from spectracut.audio.procedural.surge import SURGE_KINDS
from spectracut.audio.procedural.synth import PROCEDURAL_KINDS
from spectracut.audio.recipe import SoundSpec

SR = 48_000


def _spec(kind: str = "surge_pluck", **params: float | str) -> SoundSpec:
    return SoundSpec(
        kind=kind, duration_seconds=1.0, params=dict(params), seed=3, provenance=PROCEDURAL
    )


def _lua(spec: SoundSpec) -> str:
    return build_surge_lua(spec, SR, Path("/tmp/s"), Path("/tmp/o"))


def test_surge_kinds_do_not_collide_with_existing_kinds() -> None:
    assert not set(SURGE_KINDS) & set(PROCEDURAL_KINDS)
    assert SurgeXTGenerator.kinds == frozenset(SURGE_KINDS)


def test_surge_program_hosts_the_lv2_instrument_with_numeric_params_only() -> None:
    lua = _lua(_spec(note=64, velocity=90, note_samples=12_000))
    assert "urn:surge-synth-team.org:surge-xt" in lua
    assert "local note, vel, hold = 64, 90, 12000" in lua
    assert "e:set_range(0, 48000)" in lua
    assert lua == _lua(_spec(note=64, velocity=90, note_samples=12_000))


def test_surge_program_defaults_come_from_the_kind() -> None:
    assert "local note, vel, hold = 48, 100, 38400" in _lua(_spec("surge_pad"))


@pytest.mark.parametrize(
    ("params", "match"),
    [
        ({"note": "60); os.exit()"}, "must be a number"),
        ({"note": 200}, "integer from 0 to 127"),
        ({"note": 60.5}, "integer from 0 to 127"),
        ({"velocity": 0}, "velocity"),
        ({"note_samples": 0}, "note_samples"),
        ({"note_samples": 10**9}, "note_samples"),
    ],
)
def test_surge_program_rejects_bad_params(params: dict[str, float | str], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _lua(_spec(**params))


def test_surge_program_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="cannot make"):
        _lua(_spec("riser"))


def test_generator_unavailable_without_ardour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    gen = SurgeXTGenerator()
    assert not gen.available()
    with pytest.raises(RuntimeError, match="ardour8-lua is not on PATH"):
        gen.generate(_spec(), SR)


@pytest.mark.e2e
@pytest.mark.skipif(
    os.environ.get("SPECTRACUT_E2E") != "1" or shutil.which("ardour8-lua") is None,
    reason="set SPECTRACUT_E2E=1 on a host with Ardour and Surge XT",
)
def test_surge_pluck_renders_exact_length_and_deterministically() -> None:
    spec = _spec()
    first = SurgeXTGenerator().generate(spec, SR)
    second = SurgeXTGenerator().generate(spec, SR)
    assert len(first) == 2 and all(len(c) == SR for c in first)
    assert max(abs(v) for v in first[0]) > 0.1
    assert [list(c) for c in first] == [list(c) for c in second]
