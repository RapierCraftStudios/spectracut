# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Critic: vision rubric scoring, sequence review and pairwise choice at every gate.

:class:`StudioCritic` is a graph :data:`~spectracut.studio.gates.Critic`
(``critic(spec, output) -> CriticVerdict``), so it plugs into
:meth:`~spectracut.studio.graph.ProductionGraph.run` unchanged. For each output it:

1. pulls the reviewable artifact out of the node's JSON (:func:`extract_artifact`:
   ``frames``, ``contact_sheet`` and ``sequence`` paths);
2. scores each frame and the contact sheet with a
   :class:`~spectracut.providers.base.VisionJudge` against one prompt built from
   the department rubric, its taste anchors, the brief and the canon;
3. reviews a short frame sequence for motion problems with deterministic metrics:
   flicker (:func:`spectracut.qa.look.flicker`) and a static UI (no frame-to-frame
   change, :func:`motion`);
4. sends outputs with no frames (scripts, plans, mixes) to a text judge;
5. returns a verdict whose ``scores`` hold every per-frame and aggregate score and
   whose ``comment`` holds specific notes for the department's next attempt.

:meth:`StudioCritic.compare` chooses between two candidates (BlenderAlchemy): a
passing candidate beats a failing one, then a :class:`PairwiseJudge` decides when
one is configured, else the higher aggregate score wins and a tie keeps the
incumbent.

The Critic reads frames from disk only; it never imports bpy or renders.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import JsonValue

from spectracut.canon.schema import Canon
from spectracut.providers.base import VisionJudge, judge_usage
from spectracut.qa.common import Gray, downscale, load_gray, mean
from spectracut.qa.look import FLICKER_MAX, flicker
from spectracut.spec._base import StrictModel, canonical_json, content_hash
from spectracut.spec.brief import Brief, CreativeBrief
from spectracut.studio.gates import CriticVerdict, Finding, Severity
from spectracut.studio.node import NodeSpec
from spectracut.studio.profiles import CritiqueSettings, QualityProfile

if TYPE_CHECKING:
    from spectracut.studio.runner import DepartmentRunner, Judge

#: Mean absolute frame-to-frame luma change below which a sequence is a static UI.
STATIC_UI_MAX = 0.002

OVERALL = "overall"
"""Score key of the aggregate score in every verdict."""

CRITIC_REQUESTS = "critic_requests"
CRITIC_INPUT_TOKENS = "critic_input_tokens"
CRITIC_OUTPUT_TOKENS = "critic_output_tokens"
"""Ledger metrics for the Critic's own judge calls (the department's are separate)."""

TextJudge = Callable[[NodeSpec, str, JsonValue], CriticVerdict]
"""``text_judge(spec, prompt, output) -> CriticVerdict`` for outputs with no frames."""

Winner = Literal["a", "b"]


# --------------------------------------------------------------------------- artifacts


@dataclass(frozen=True)
class CritiqueArtifact:
    """The visual parts of a node's output the Critic can look at."""

    frames: tuple[Path, ...] = ()
    contact_sheet: Path | None = None
    sequence: tuple[Path, ...] = ()
    """A short run of consecutive frames, reviewed for motion problems."""

    @property
    def visual(self) -> bool:
        return bool(self.frames or self.contact_sheet is not None or self.sequence)

    def representative(self) -> Path | None:
        """The one image that stands for the artifact in a pairwise comparison."""
        if self.contact_sheet is not None:
            return self.contact_sheet
        if self.frames:
            return self.frames[0]
        if self.sequence:
            return self.sequence[len(self.sequence) // 2]
        return None


def _paths(value: JsonValue) -> tuple[Path, ...]:
    if isinstance(value, str):
        return (Path(value),)
    if isinstance(value, list):
        return tuple(Path(v) for v in value if isinstance(v, str))
    return ()


def extract_artifact(output: JsonValue) -> CritiqueArtifact:
    """The ``frames``, ``contact_sheet`` and ``sequence`` paths of a JSON output."""
    if not isinstance(output, Mapping):
        return CritiqueArtifact()
    sheet = output.get("contact_sheet")
    return CritiqueArtifact(
        frames=_paths(output.get("frames")),
        contact_sheet=Path(sheet) if isinstance(sheet, str) else None,
        sequence=_paths(output.get("sequence")),
    )


# --------------------------------------------------------------------------- motion


def motion(frames: Sequence[Gray]) -> float:
    """Mean absolute per-pixel luma change between consecutive frames (0 = frozen)."""
    small = [downscale(f) for f in frames]
    steps: list[float] = []
    for a, b in zip(small, small[1:], strict=False):
        if len(a.pixels) != len(b.pixels):
            continue
        steps.append(mean([abs(x - y) for x, y in zip(a.pixels, b.pixels, strict=True)]))
    return mean(steps) if steps else 0.0


class MotionReview(StrictModel):
    """Motion problems found in a short frame sequence."""

    flicker: float
    motion: float
    faults: tuple[Literal["flicker", "static_ui", "unreadable"], ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.faults


def review_sequence(
    frames: Sequence[Gray] | Sequence[Path],
    *,
    flicker_max: float = FLICKER_MAX,
    static_max: float = STATIC_UI_MAX,
) -> MotionReview:
    """Flicker and static-UI review of a frame sequence (frames or PNG paths)."""
    grays: list[Gray] = []
    for f in frames:
        if isinstance(f, Gray):
            grays.append(f)
            continue
        g = load_gray(Path(f).read_bytes()) if Path(f).is_file() else None
        if g is None:
            return MotionReview(
                flicker=0.0,
                motion=0.0,
                faults=("unreadable",),
                notes=(f"sequence frame {Path(f).name} is missing or not a readable PNG",),
            )
        grays.append(g)
    if len(grays) < 2:
        return MotionReview(flicker=0.0, motion=0.0)
    flick, move = round(flicker(grays), 4), round(motion(grays), 4)
    faults: list[Literal["flicker", "static_ui", "unreadable"]] = []
    notes: list[str] = []
    if flick > flicker_max:
        faults.append("flicker")
        notes.append(
            f"sequence flickers: frame-to-frame brightness varies by {flick:.3f} "
            f"(max {flicker_max:.3f}); hold exposure and lighting steady across frames"
        )
    if move < static_max:
        faults.append("static_ui")
        notes.append(
            f"sequence is static: frames change by {move:.4f} (min {static_max:.4f}); "
            "the UI must visibly respond (cursor, typing, transition) during the shot"
        )
    return MotionReview(flicker=flick, motion=move, faults=tuple(faults), notes=tuple(notes))


# --------------------------------------------------------------------------- pairwise


class PairwiseVerdict(StrictModel):
    winner: Winner
    notes: str = ""


@runtime_checkable
class PairwiseJudge(Protocol):
    """Picks the better of two images against a rubric (BlenderAlchemy)."""

    name: str

    def compare(self, a: Path, b: Path, rubric: str) -> PairwiseVerdict: ...


@dataclass(frozen=True)
class Candidate:
    """One reviewed output: what a department produced and the Critic's verdict on it."""

    output: JsonValue
    verdict: CriticVerdict

    @property
    def score(self) -> float:
        return self.verdict.scores.get(OVERALL, 1.0 if self.verdict.passed else 0.0)


# --------------------------------------------------------------------------- the Critic


def critique_prompt(
    rubric: str,
    anchors: Sequence[str] = (),
    *,
    brief: Brief | CreativeBrief | None = None,
    canon: Canon | None = None,
    kind: str = "",
) -> str:
    """The judging prompt: rubric, taste anchors, brief and canon, in that order."""
    parts = [f"Score this {kind or 'artifact'} from 0 to 1 against the rubric.", "", rubric]
    if anchors:
        parts += ["", "Taste anchors (the target quality):"]
        parts += [f"- {a.strip()}" for a in anchors if a.strip()]
    if brief is not None:
        parts += ["", "Brief:", canonical_json(brief).decode("utf-8")]
    if canon is not None:
        parts += ["", "Canon (must stay consistent):", canonical_json(canon).decode("utf-8")]
    return "\n".join(parts)


def _add_usage(total: dict[str, int], tokens: Mapping[str, int]) -> None:
    """Add one judge call's ``judge_usage`` to ``total`` under the ``critic_*`` metrics."""
    if not tokens:
        return
    for metric, amount in (
        (CRITIC_REQUESTS, 1),
        (CRITIC_INPUT_TOKENS, tokens["input_tokens"]),
        (CRITIC_OUTPUT_TOKENS, tokens["output_tokens"]),
    ):
        total[metric] = total.get(metric, 0) + amount


def _normalise(verdict: CriticVerdict) -> CriticVerdict:
    """Ensure a verdict carries an overall score and notes."""
    scores = dict(verdict.scores)
    if OVERALL not in scores:
        scores[OVERALL] = round(mean(list(scores.values())), 4) if scores else float(verdict.passed)
    comment = verdict.comment.strip() or (
        "passes the rubric" if verdict.passed else "fails the rubric; revise against it"
    )
    return CriticVerdict(
        passed=verdict.passed, scores=scores, comment=comment, usage=dict(verdict.usage)
    )


class StudioCritic:
    """The Critic as a graph ``Critic``: vision scoring, sequence review, pairwise choice."""

    def __init__(
        self,
        vision: VisionJudge,
        *,
        rubric_for: Callable[[NodeSpec], str],
        anchors_for: Callable[[NodeSpec], Sequence[str]] = lambda _spec: (),
        brief: Brief | CreativeBrief | None = None,
        canon: Canon | None = None,
        text_judge: TextJudge | None = None,
        pairwise: PairwiseJudge | None = None,
        profile: QualityProfile | None = None,
        settings: CritiqueSettings | None = None,
        extract: Callable[[JsonValue], CritiqueArtifact] = extract_artifact,
    ) -> None:
        self.vision = vision
        self.rubric_for = rubric_for
        self.anchors_for = anchors_for
        self.brief = brief
        self.canon = canon
        self.text_judge = text_judge
        self.pairwise = pairwise
        self.profile = profile
        if settings is None:
            settings = profile.critique if profile is not None else CritiqueSettings()
        self.settings = settings
        self.extract = extract
        self._cache: dict[tuple[str, str], CriticVerdict] = {}
        self._lock = threading.Lock()

    def prompt(self, spec: NodeSpec) -> str:
        return critique_prompt(
            self.rubric_for(spec),
            self.anchors_for(spec),
            brief=self.brief,
            canon=self.canon,
            kind=spec.kind,
        )

    def verify_samples(self, spec: NodeSpec) -> int:
        if self.profile is None:
            return 1
        return self.profile.split_budget(spec.kind).verify_samples

    def __call__(self, spec: NodeSpec, output: JsonValue) -> CriticVerdict:
        return self.review(spec, output)

    def review(self, spec: NodeSpec, output: JsonValue) -> CriticVerdict:
        """Score ``output`` and write notes; identical outputs are reviewed once."""
        key = (spec.id, content_hash(output))
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            # The judge's tokens were charged when the verdict was first made.
            return hit.model_copy(update={"usage": {}}) if hit.usage else hit
        artifact = self.extract(output)
        prompt = self.prompt(spec)
        if artifact.visual:
            verdict = self._review_visual(spec, artifact, prompt)
        elif self.text_judge is not None:
            verdict = _normalise(self.text_judge(spec, prompt, output))
        else:
            verdict = CriticVerdict(
                passed=True,
                scores={OVERALL: 1.0},
                comment=f"{spec.kind}: no frames to review and no text judge configured; "
                "accepted on its schema check",
            )
        with self._lock:
            self._cache[key] = verdict
        return verdict

    def _score(
        self, path: Path, prompt: str, samples: int, usage: dict[str, int]
    ) -> tuple[float, str]:
        values: list[float] = []
        notes: list[str] = []
        for _ in range(samples):
            vs = self.vision.score(path, prompt)
            _add_usage(usage, judge_usage(self.vision))
            values.append(min(1.0, max(0.0, float(vs.score))))
            if vs.notes and vs.notes not in notes:
                notes.append(vs.notes)
        return round(mean(values), 4), "; ".join(notes)

    def _review_visual(
        self, spec: NodeSpec, artifact: CritiqueArtifact, prompt: str
    ) -> CriticVerdict:
        samples = self.verify_samples(spec)
        scores: dict[str, float] = {}
        lines: list[str] = []
        usage: dict[str, int] = {}
        images: list[tuple[str, Path]] = [
            (f"frame-{i}", p) for i, p in enumerate(artifact.frames, start=1)
        ]
        if artifact.contact_sheet is not None:
            images.append(("contact-sheet", artifact.contact_sheet))
        if not images and artifact.sequence:
            images.append(("sequence-mid", artifact.sequence[len(artifact.sequence) // 2]))
        for label, path in images:
            value, note = self._score(path, prompt, samples, usage)
            scores[label] = value
            lines.append(f"{label} {value:.2f}: {note or 'no notes'}")
        overall = round(mean(list(scores.values())), 4) if scores else 1.0
        motion_review = review_sequence(artifact.sequence) if artifact.sequence else None
        if motion_review is not None and len(artifact.sequence) >= 2:
            scores["flicker"] = motion_review.flicker
            scores["motion"] = motion_review.motion
            lines.extend(motion_review.notes)
        scores[OVERALL] = overall
        faults = motion_review is not None and not motion_review.ok
        passed = overall >= self.settings.pass_score and not faults
        if overall < self.settings.pass_score:
            lines.insert(
                0,
                f"{spec.kind} scores {overall:.2f}, below the pass mark "
                f"{self.settings.pass_score:.2f}; address the frame notes against the rubric",
            )
        head = "pass" if passed else "revise"
        return CriticVerdict(
            passed=passed, scores=scores, comment=f"{head}: " + " | ".join(lines), usage=usage
        )

    # ---- as a panel reviewer ---------------------------------------------------

    def findings(self, spec: NodeSpec, output: JsonValue) -> list[Finding]:
        """The score verdict as findings: a failing review is one HIGH finding, else none."""
        verdict = self.review(spec, output)
        if verdict.passed:
            return []
        return [
            Finding(
                reviewer="visual-craft",
                severity=Severity.HIGH,
                text=verdict.comment or f"{spec.kind} fails its rubric",
            )
        ]

    def as_review_call(self) -> Callable[[NodeSpec, str, JsonValue], list[Finding]]:
        """A panel ``ReviewCall`` backed by this Critic (its own rubric applies)."""
        return lambda spec, _rubric, output: self.findings(spec, output)

    # ---- choosing --------------------------------------------------------------

    def candidate(self, spec: NodeSpec, output: JsonValue) -> Candidate:
        return Candidate(output=output, verdict=self.review(spec, output))

    def compare(self, spec: NodeSpec, a: Candidate, b: Candidate) -> PairwiseVerdict:
        """Which of incumbent ``a`` and challenger ``b`` is better; ties keep ``a``."""
        if a.verdict.passed != b.verdict.passed:
            winner: Winner = "a" if a.verdict.passed else "b"
            return PairwiseVerdict(winner=winner, notes="only one candidate passes")
        fa, fb = self.extract(a.output).representative(), self.extract(b.output).representative()
        if self.pairwise is not None and fa is not None and fb is not None:
            return self.pairwise.compare(fa, fb, self.prompt(spec))
        winner = "b" if b.score > a.score else "a"
        return PairwiseVerdict(winner=winner, notes=f"by score: a {a.score:.2f}, b {b.score:.2f}")

    def best(self, spec: NodeSpec, candidates: Sequence[Candidate]) -> Candidate:
        """The winner of a pairwise run through ``candidates`` (the first holds ties)."""
        if not candidates:
            raise ValueError("best() needs at least one candidate")
        champion = candidates[0]
        for challenger in candidates[1:]:
            if self.compare(spec, champion, challenger).winner == "b":
                champion = challenger
        return champion


def critic_for_runner(
    runner: DepartmentRunner,
    vision: VisionJudge,
    *,
    judge: Judge | None = None,
    pairwise: PairwiseJudge | None = None,
    profile: QualityProfile | None = None,
) -> StudioCritic:
    """A :class:`StudioCritic` that takes rubric and taste anchors from each skill pack."""

    def rubric_for(spec: NodeSpec) -> str:
        return runner.department(spec.department).rubric()

    def anchors_for(spec: NodeSpec) -> tuple[str, ...]:
        pack = runner.department(spec.department).pack()
        return tuple(p.read_text(encoding="utf-8") for p in pack.anchors)

    text_judge: TextJudge | None = None
    if judge is not None:
        bound = judge

        def text_judge(spec: NodeSpec, prompt: str, output: JsonValue) -> CriticVerdict:
            return bound(runner.department(spec.department), prompt, output)

    return StudioCritic(
        vision,
        rubric_for=rubric_for,
        anchors_for=anchors_for,
        brief=runner.brief,
        canon=runner.canon,
        text_judge=text_judge,
        pairwise=pairwise,
        profile=profile,
    )
