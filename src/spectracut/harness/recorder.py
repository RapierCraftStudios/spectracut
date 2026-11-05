# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recipe recording: every harness call is logged; build calls become recipe steps.

Build tools (scene, camera, light, material) append :class:`RecipeStep` entries,
so the session's :class:`Recipe` replays the scene exactly. Query and meta tools
(inspect, preview, look, discover) go to the call log only: replaying them would
change nothing, or (``look.set_view``) would move the camera in later replays.
The recorder stores tool names and JSON arguments only, never environment values.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from spectracut.spec.recipe import Recipe, RecipeStep

CallKind = Literal["build", "query", "meta"]
DEFAULT_BLENDER_VERSION = "4.5"


@dataclass(frozen=True)
class Call:
    """One harness call as the client made it."""

    tool: str
    kind: CallKind
    args: dict[str, JsonValue]
    steps: tuple[int, ...] = ()
    """Indexes of the recipe steps this call recorded (build calls only)."""


@dataclass
class Recorder:
    """The replayable recipe and call log for one harness session."""

    recipe_id: str = "harness-session"
    shot_id: str = "harness-shot"
    blender_version: str = DEFAULT_BLENDER_VERSION
    steps: list[RecipeStep] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def record(
        self,
        tool: str,
        kind: CallKind,
        args: dict[str, JsonValue],
        steps: Sequence[tuple[str, dict[str, JsonValue]]] = (),
    ) -> Call:
        """Log a call; append ``steps`` (runtime tool name, args) to the recipe."""
        added: list[int] = []
        new = [
            RecipeStep(index=len(self.steps) + i, tool=name, args=step_args)
            for i, (name, step_args) in enumerate(steps)
        ]
        for step in new:
            self.steps.append(step)
            added.append(step.index)
        call = Call(tool=tool, kind=kind, args=args, steps=tuple(added))
        self.calls.append(call)
        return call

    def recipe(self) -> Recipe:
        return Recipe(
            id=self.recipe_id,
            shot_id=self.shot_id,
            blender_version=self.blender_version,
            steps=list(self.steps),
        )

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.recipe().model_dump_json(indent=2) + "\n", encoding="utf-8")
        return path


def load_recipe(path: Path) -> Recipe:
    """Load a recipe saved by :meth:`Recorder.save`."""
    return Recipe.model_validate_json(path.read_text(encoding="utf-8"))
