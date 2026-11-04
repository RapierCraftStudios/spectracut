# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entity and status names map one-to-one onto Kitsu's model, and the doc says so."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest

from spectracut.spec.kitsu import (
    KITSU_ENTITY,
    KITSU_REVIEW_STATUS,
    KITSU_STATUS,
    EntityKind,
    ReviewState,
    TaskStatus,
)

DOC = Path(__file__).resolve().parents[2] / "docs" / "engineering" / "contracts" / "schemas.md"

MAPS: list[tuple[type[StrEnum], Mapping[Any, object]]] = [
    (EntityKind, KITSU_ENTITY),
    (TaskStatus, KITSU_STATUS),
    (ReviewState, KITSU_REVIEW_STATUS),
]


@pytest.mark.parametrize(("enum", "mapping"), MAPS)
def test_mapping_is_total_and_injective(enum: type[StrEnum], mapping: Mapping[Any, object]) -> None:
    assert set(mapping) == set(enum)
    assert len(set(mapping.values())) == len(mapping)


def test_entity_names_cover_kitsu_model() -> None:
    assert {"Project", "Episode", "Sequence", "Shot", "Asset", "Task", "TaskType"} <= set(
        KITSU_ENTITY.values()
    )


@pytest.mark.parametrize(("enum", "mapping"), MAPS)
def test_mapping_is_documented(enum: type[StrEnum], mapping: Mapping[Any, object]) -> None:
    text = DOC.read_text(encoding="utf-8")
    for ours, theirs in mapping.items():
        assert f"| `{ours.value}` | `{theirs}` |" in text, (ours, theirs)
