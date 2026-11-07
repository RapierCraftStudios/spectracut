# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reference comparison (#342): measured pace and type, frame scores, the no-copy guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from spectracut.critique.references import (
    MAX_CARD_WORDS,
    REFERENCE_RUBRIC,
    assert_not_reference,
    compare_frames,
    measure_pace,
    measure_storyboard,
    pace_range,
)
from spectracut.providers.base import ProviderError, VisionScore
from spectracut.providers.mock_vision import MockVisionJudge
from spectracut.spec.references import ReferenceSet, load_reference_set
from spectracut.storyboard.models import Storyboard

BENCHMARKS = Path(__file__).parents[2] / "benchmarks"
KINETIC = "base44-launch:kinetic-type-as-the-spine"
CALM = "elevenagents-architect:calm-pace"


def _refs(store: Path) -> ReferenceSet:
    return load_reference_set(root=BENCHMARKS, store=store)


def _board(duration: float, texts: list[str], cited: list[str]) -> Storyboard:
    return Storyboard.model_validate(
        {
            "boards": [
                {"shot_id": f"s{i}", "beat": "b", "action": "a", "text": t, "duration_s": duration}
                for i, t in enumerate(texts)
            ],
            "craft_refs": cited,
        }
    )


def test_pace_range_widens_the_cited_references(tmp_path: Path) -> None:
    refs = _refs(tmp_path)
    assert pace_range(refs, [KINETIC]) == (0.975, 1.625)
    assert pace_range(refs, [KINETIC, CALM]) == (0.975, 6.5)
    assert pace_range(refs, []) is None
    assert measure_pace([1.0], refs, []) is None


def test_base44_paced_boards_pass(tmp_path: Path) -> None:
    measured = measure_storyboard(
        _board(1.3, ["Build", "With your name on it"], [KINETIC]), _refs(tmp_path)
    )
    assert measured.ok and measured.pace is not None and measured.pace.score == 1.0
    assert measured.cited == [KINETIC]


def test_five_second_boards_fail_the_base44_pace(tmp_path: Path) -> None:
    measured = measure_storyboard(_board(5.0, ["Build"], [KINETIC]), _refs(tmp_path))
    assert not measured.ok
    assert measured.pace is not None and not measured.pace.ok and measured.pace.score < 1
    assert "outside the cited references' range" in measured.problems[0]


def test_a_nine_word_card_fails_the_type_bound(tmp_path: Path) -> None:
    text = "one two three four five six seven eight nine"
    measured = measure_storyboard(_board(1.3, ["Build", text], [KINETIC]), _refs(tmp_path))
    assert measured.max_card_words == 9 > MAX_CARD_WORDS
    assert len(measured.problems) == 1 and "one idea per card" in measured.problems[0]


def _store_with_keyframe(tmp_path: Path) -> Path:
    store = tmp_path / "store"
    (store / "keyframes").mkdir(parents=True)
    (store / "keyframes" / "base44-launch.png").write_bytes(b"reference keyframe")
    return store


def _frames(tmp_path: Path) -> list[Path]:
    out = []
    for i in range(2):
        f = tmp_path / "out" / f"f{i}.png"
        f.parent.mkdir(exist_ok=True)
        f.write_bytes(f"frame {i}".encode())
        out.append(f)
    return out


def test_compare_frames_scores_every_frame_and_records_the_mean(tmp_path: Path) -> None:
    refs = _refs(_store_with_keyframe(tmp_path))
    frames = _frames(tmp_path)
    result = compare_frames(frames, refs, [KINETIC], MockVisionJudge())
    assert [f.frame for f in result.frames] == [str(f) for f in frames]
    assert all(f.reference == "base44-launch" for f in result.frames)
    mean = round(sum(f.score for f in result.frames) / 2, 3)
    assert result.score == mean and result.judge == "mock-vision"
    assert result.references == ["base44-launch"] and result.note == ""

    pace = measure_pace([1.3], refs, [KINETIC])
    with_pace = compare_frames(frames, refs, [KINETIC], MockVisionJudge(), pace=pace)
    assert with_pace.score == round((sum(f.score for f in result.frames) + 1.0) / 3, 3)


def test_rubric_covers_pace_type_ui_and_look_and_forbids_copying() -> None:
    for word in ("pace", "type", "UI legibility", "look", "craft only", "must not copy"):
        assert word in REFERENCE_RUBRIC


def test_no_keyframes_or_no_judge_records_none_with_a_note(tmp_path: Path) -> None:
    frames = _frames(tmp_path)
    bare = compare_frames(frames, _refs(tmp_path / "empty"), [KINETIC], MockVisionJudge())
    assert bare.score is None and bare.frames == [] and "no local keyframes" in bare.note
    no_judge = compare_frames(frames, _refs(_store_with_keyframe(tmp_path)), [KINETIC], None)
    assert no_judge.score is None and "no reference judge" in no_judge.note
    uncited = compare_frames(frames, _refs(tmp_path / "empty"), [], MockVisionJudge())
    assert uncited.score is None and "no reference craft points" in uncited.note


class _Broken:
    name = "broken"

    def compare(self, frame_path: Path, reference_path: Path, rubric: str) -> VisionScore:
        raise ProviderError("broken", "offline")


def test_a_failing_judge_is_a_note_not_a_failure(tmp_path: Path) -> None:
    refs = _refs(_store_with_keyframe(tmp_path))
    result = compare_frames(_frames(tmp_path), refs, [KINETIC], _Broken())
    assert result.score is None and "offline" in result.note


def test_assert_not_reference_rejects_copies_and_store_paths(tmp_path: Path) -> None:
    store = _store_with_keyframe(tmp_path)
    refs = _refs(store)
    copy = tmp_path / "out" / "render.png"
    copy.parent.mkdir()
    copy.write_bytes(b"reference keyframe")
    with pytest.raises(ValueError, match="copy of reference keyframe"):
        assert_not_reference(copy, refs, store)
    with pytest.raises(ValueError, match="inside the reference store"):
        assert_not_reference(store / "keyframes" / "x.png", refs, store)
    own = tmp_path / "out" / "own.png"
    own.write_bytes(b"original render")
    assert_not_reference(own, refs, store)
    assert_not_reference(tmp_path / "out" / "missing.png", None, store)
