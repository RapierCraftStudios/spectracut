# SPDX-License-Identifier: AGPL-3.0-or-later
"""The edit-rule linter rejects back-to-back shots with the same setup (#26)."""

from __future__ import annotations

import pytest

from spectracut.edit.timeline import Clip, Edit, Gap, Track, Transition
from spectracut.formats.linter import EditRuleError, ShotLook, ensure_clean, lint

BASE = ShotLook(scale="ms", lens_mm=50, move="push")


def clip(shot: str) -> Clip:
    return Clip(id=f"c-{shot}", shot_id=shot, media_ref=f"{shot}.mov", source_out=48)


def edit(*items: Clip | Gap | Transition) -> Edit:
    return Edit(id="e", tracks=[Track(id="v1", items=list(items))])


def test_same_scale_lens_and_move_back_to_back_is_rejected() -> None:
    looks = {"a": BASE, "b": BASE}
    issues = lint(edit(clip("a"), clip("b")), looks)
    assert [(i.rule, i.clip_ids) for i in issues] == [("repeat-setup", ("c-a", "c-b"))]
    with pytest.raises(EditRuleError):
        ensure_clean(edit(clip("a"), clip("b")), looks)


@pytest.mark.parametrize("change", [{"scale": "cu"}, {"lens_mm": 35.0}, {"move": "static"}])
def test_differing_in_any_one_passes(change: dict[str, object]) -> None:
    looks = {"a": BASE, "b": BASE.model_copy(update=change)}
    assert lint(edit(clip("a"), clip("b")), looks) == []
    ensure_clean(edit(clip("a"), clip("b")), looks)


def test_non_adjacent_repeat_passes() -> None:
    looks = {"a": BASE, "b": BASE.model_copy(update={"scale": "ws"}), "c": BASE}
    assert lint(edit(clip("a"), clip("b"), clip("c")), looks) == []


def test_gap_or_transition_does_not_separate_shots() -> None:
    looks = {"a": BASE, "b": BASE}
    assert lint(edit(clip("a"), Gap(duration=12), clip("b")), looks)
    assert lint(edit(clip("a"), Transition(in_offset=6, out_offset=6), clip("b")), looks)


def test_nested_edit_is_linted() -> None:
    inner = edit(clip("a"), clip("b"))
    outer = Edit(id="o", tracks=[Track(id="v1", items=[Clip(id="scene", nested=inner)])])
    assert len(lint(outer, {"a": BASE, "b": BASE})) == 1


def test_audio_tracks_are_ignored() -> None:
    e = Edit(id="e", tracks=[Track(id="a1", kind="audio", items=[clip("a"), clip("b")])])
    assert lint(e, {"a": BASE, "b": BASE}) == []


def test_shot_without_look_raises() -> None:
    with pytest.raises(KeyError):
        lint(edit(clip("a")), {})
