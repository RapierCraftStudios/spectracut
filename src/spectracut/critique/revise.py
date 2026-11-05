# SPDX-License-Identifier: AGPL-3.0-or-later
"""The revise loop: generate → render → separate verifier → revise, with best-of-N and undo.

Folded in from VIGA (spike § Built on proven open source):

- the recipe is the full state: every attempt hands the generator the best recipe
  so far, never a partial edit history;
- a sliding window of the last ``window`` iterations (recipe, diff against the
  previous recipe, rendered frames, verdict) goes back with each revision request;
- each attempt generates ``best_of_n`` candidates and keeps the pairwise winner
  (BlenderAlchemy);
- undo: a revision that loses to the best so far is dropped and the best recipe
  stays the state;
- after ``1 + revision_budget`` attempts without a pass the artifact is escalated
  to a person, carrying the Critic's last notes.

How many candidates and Critic reviews an attempt gets comes from the quality
profile (:meth:`~spectracut.studio.profiles.QualityProfile.split_budget`, the
BlenderGym budget rule). :func:`best_of_n_runner` brings best-of-N to the
production graph, which keeps owning revision counts and rejection.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from spectracut.critique.critic import Candidate, StudioCritic
from spectracut.studio.gates import CriticVerdict
from spectracut.studio.graph import RunContext, Runner, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import NodeSpec
from spectracut.studio.profiles import QualityProfile


@dataclass(frozen=True)
class Iteration:
    """One attempt's winning candidate, as the loop remembers it."""

    attempt: int
    recipe: JsonValue
    """The full recipe of this candidate (the whole state, not a patch)."""
    diff: dict[str, JsonValue]
    """What changed against the previous iteration's recipe, by top-level key."""
    frames: tuple[Path, ...]
    verdict: CriticVerdict
    kept: bool
    """Whether it became the best so far; ``False`` means it was undone."""


@dataclass(frozen=True)
class RevisionRequest:
    """What the generator gets for one candidate."""

    attempt: int
    candidate: int
    """Index of this candidate within the attempt (best-of-N), from 0."""
    state: JsonValue
    """The best recipe so far (``None`` on the first attempt)."""
    notes: str
    """The Critic's notes on the best so far; empty on the first attempt."""
    window: tuple[Iteration, ...]
    """The most recent iterations, oldest first."""


Generate = Callable[[RevisionRequest], JsonValue]
"""``generate(request) -> recipe``: a department planning or revising a recipe."""

Render = Callable[[JsonValue], JsonValue]
"""``render(recipe) -> output``: the output JSON, with ``frames`` / ``sequence`` paths."""


@dataclass(frozen=True)
class ReviseOutcome:
    status: Literal["accepted", "escalated"]
    recipe: JsonValue
    """The best recipe found."""
    output: JsonValue
    verdict: CriticVerdict
    attempts: int
    notes: str
    """The Critic's notes on the best output; what a person sees on escalation."""
    history: tuple[Iteration, ...] = field(default=())
    """Every iteration, oldest first."""
    window: tuple[Iteration, ...] = field(default=())
    """The sliding window at the end of the loop."""


def recipe_diff(previous: JsonValue, current: JsonValue) -> dict[str, JsonValue]:
    """Top-level keys whose values changed, as ``{key: {"from": old, "to": new}}``."""
    if previous is None:
        return {}
    if isinstance(previous, Mapping) and isinstance(current, Mapping):
        out: dict[str, JsonValue] = {}
        for key in sorted(set(previous) | set(current)):
            old, new = previous.get(key), current.get(key)
            if old != new:
                out[key] = {"from": old, "to": new}
        return out
    return {} if previous == current else {"recipe": {"from": previous, "to": current}}


class ReviseLoop:
    """Revise one artifact until the Critic passes it or the budget is spent."""

    def __init__(
        self,
        generate: Generate,
        render: Render,
        critic: StudioCritic,
        spec: NodeSpec,
        *,
        revision_budget: int,
        best_of_n: int = 1,
        window: int = 3,
    ) -> None:
        if revision_budget < 0 or best_of_n < 1 or window < 1:
            raise ValueError("revision_budget >= 0, best_of_n >= 1 and window >= 1 required")
        self.generate = generate
        self.render = render
        self.critic = critic
        self.spec = spec
        self.revision_budget = revision_budget
        self.best_of_n = best_of_n
        self.window = window

    @classmethod
    def from_profile(
        cls,
        generate: Generate,
        render: Render,
        critic: StudioCritic,
        spec: NodeSpec,
        profile: QualityProfile,
    ) -> ReviseLoop:
        """Budget, best-of-N and window for ``spec``'s stage from ``profile``."""
        return cls(
            generate,
            render,
            critic,
            spec,
            revision_budget=profile.revision_budget.get(spec.kind, 0),
            best_of_n=profile.split_budget(spec.kind).candidates,
            window=profile.critique.window,
        )

    def run(self) -> ReviseOutcome:
        recent: deque[Iteration] = deque(maxlen=self.window)
        history: list[Iteration] = []
        best: tuple[JsonValue, Candidate] | None = None
        previous: JsonValue = None
        attempts = 1 + self.revision_budget
        for attempt in range(1, attempts + 1):
            state = best[0] if best is not None else None
            notes = best[1].verdict.comment if best is not None else ""
            pool: list[tuple[JsonValue, Candidate]] = []
            for n in range(self.best_of_n):
                request = RevisionRequest(
                    attempt=attempt, candidate=n, state=state, notes=notes, window=tuple(recent)
                )
                recipe = self.generate(request)
                pool.append((recipe, self.critic.candidate(self.spec, self.render(recipe))))
            winner = self.critic.best(self.spec, [c for _, c in pool])
            recipe = next(r for r, c in pool if c is winner)
            kept = best is None or self.critic.compare(self.spec, best[1], winner).winner == "b"
            if kept:
                best = (recipe, winner)
            it = Iteration(
                attempt=attempt,
                recipe=recipe,
                diff=recipe_diff(previous, recipe),
                frames=self.critic.extract(winner.output).frames
                or self.critic.extract(winner.output).sequence,
                verdict=winner.verdict,
                kept=kept,
            )
            previous = recipe
            recent.append(it)
            history.append(it)
            assert best is not None
            if best[1].verdict.passed:
                return self._outcome("accepted", best, attempt, history, recent)
        assert best is not None
        return self._outcome("escalated", best, attempts, history, recent)

    @staticmethod
    def _outcome(
        status: Literal["accepted", "escalated"],
        best: tuple[JsonValue, Candidate],
        attempts: int,
        history: list[Iteration],
        recent: deque[Iteration],
    ) -> ReviseOutcome:
        recipe, cand = best
        return ReviseOutcome(
            status=status,
            recipe=recipe,
            output=cand.output,
            verdict=cand.verdict,
            attempts=attempts,
            notes=cand.verdict.comment,
            history=tuple(history),
            window=tuple(recent),
        )


def _add_usage(a: Usage, b: Usage) -> Usage:
    ai = dict(a.ai_usage)
    for k, v in b.ai_usage.items():
        ai[k] = ai.get(k, 0) + v
    return Usage(
        gpu_minutes=a.gpu_minutes + b.gpu_minutes,
        ai_usage=ai,
        elevenlabs_characters=a.elevenlabs_characters + b.elevenlabs_characters,
    )


def best_of_n_runner(runner: Runner, critic: StudioCritic, profile: QualityProfile) -> Runner:
    """A graph ``Runner`` that builds ``best_of_n`` candidates per attempt and keeps the winner.

    Usage of every candidate is summed into the result, so the ledger records what
    best-of-N cost. The winner's review is cached by the Critic, so the graph's gate
    does not score it twice.
    """

    def run(ctx: RunContext) -> RunResult:
        n = profile.split_budget(ctx.spec.kind).candidates
        if n <= 1:
            return runner(ctx)
        results = [runner(ctx) for _ in range(n)]
        cands = [critic.candidate(ctx.spec, r.output) for r in results]
        winner = critic.best(ctx.spec, cands)
        usage = Usage()
        for r in results:
            usage = _add_usage(usage, r.usage)
        chosen = next(r for r, c in zip(results, cands, strict=True) if c is winner)
        return RunResult(output=chosen.output, usage=usage)

    return run
