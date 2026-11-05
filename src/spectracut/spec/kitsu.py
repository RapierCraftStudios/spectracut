# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entity and status names, mapped one-to-one onto the Kitsu/Zou data model.

SpectraCut mirrors Kitsu's model in its own tables (no Kitsu server needed) and
adds content hashes and dirty state. These maps are the contract for an optional
Kitsu sync; ``tests/spec/test_kitsu_mapping.py`` asserts they are bijective.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class EntityKind(StrEnum):
    PRODUCTION = "production"
    EPISODE = "episode"
    SEQUENCE = "sequence"
    SHOT = "shot"
    ASSET = "asset"
    ASSET_TYPE = "asset_type"
    TASK = "task"
    TASK_TYPE = "task_type"
    TASK_STATUS = "task_status"
    CASTING = "casting"
    VERSION = "version"
    COMMENT = "comment"


class TaskStatus(StrEnum):
    TODO = "todo"
    WIP = "wip"
    WFA = "wfa"
    RETAKE = "retake"
    DONE = "done"


class ReviewState(StrEnum):
    PENDING = "pending"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"


KITSU_ENTITY: Final[dict[EntityKind, str]] = {
    EntityKind.PRODUCTION: "Project",
    EntityKind.EPISODE: "Episode",
    EntityKind.SEQUENCE: "Sequence",
    EntityKind.SHOT: "Shot",
    EntityKind.ASSET: "Asset",
    EntityKind.ASSET_TYPE: "AssetType",
    EntityKind.TASK: "Task",
    EntityKind.TASK_TYPE: "TaskType",
    EntityKind.TASK_STATUS: "TaskStatus",
    EntityKind.CASTING: "EntityLink",
    EntityKind.VERSION: "PreviewFile",
    EntityKind.COMMENT: "Comment",
}
"""SpectraCut entity -> Zou model name."""

KITSU_STATUS: Final[dict[TaskStatus, str]] = {
    TaskStatus.TODO: "todo",
    TaskStatus.WIP: "wip",
    TaskStatus.WFA: "wfa",
    TaskStatus.RETAKE: "retake",
    TaskStatus.DONE: "done",
}
"""SpectraCut task status -> Kitsu default task status short name."""

KITSU_REVIEW_STATUS: Final[dict[ReviewState, TaskStatus]] = {
    ReviewState.PENDING: TaskStatus.WFA,
    ReviewState.CHANGES_REQUESTED: TaskStatus.RETAKE,
    ReviewState.APPROVED: TaskStatus.DONE,
}
"""Review state on a gate -> the Kitsu task status that expresses it."""
