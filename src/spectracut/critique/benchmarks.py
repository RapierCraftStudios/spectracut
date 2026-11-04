# SPDX-License-Identifier: AGPL-3.0-or-later
"""Benchmark films: sets, rubrics, scoring runner, human ratings, regression and agreement.

Layout under a benchmarks directory (default ``./benchmarks``)::

    rubrics/<rubric>.json          weighted criteria the Critic scores
    sets/<set>/set.json            the pieces of a benchmark set
    sets/<set>/baseline.json       accepted scores; a drop beyond tolerance is a regression
    sets/<set>/results/            report.json, report.md, scores.jsonl, ratings.jsonl
    harness/<task>.json            headless harness tasks with hidden ground truth

The Critic is any :class:`~spectracut.providers.base.VisionJudge`. Harness tasks are
declarative scenes rendered by a tiny pure-Python rasteriser, so they run in CI
without Blender (bpy never runs in the control plane).
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
import zlib
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from spectracut.providers.base import VisionJudge
from spectracut.spec._base import (
    ContentHash,
    NonEmptyStr,
    Slug,
    StrictModel,
    UnitFloat,
    canonical_json,
    content_hash,
    ensure_unique_ids,
)

BENCHMARKS_DIR_ENV = "SPECTRACUT_BENCHMARKS_DIR"
PosFloat = Annotated[float, Field(gt=0.0)]
Pixel = tuple[int, int, int]


class BenchmarkError(ValueError):
    """A benchmark file is missing or invalid; the message names the file."""


# --------------------------------------------------------------------------- formats


class Criterion(StrictModel):
    id: Slug
    prompt: NonEmptyStr
    weight: PosFloat = 1.0


class Rubric(StrictModel):
    """What the Critic (and a person) scores a piece on."""

    id: Slug
    title: NonEmptyStr
    criteria: list[Criterion] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Rubric:
        ensure_unique_ids(self.criteria, "criterion")
        return self


def _relative(value: str) -> str:
    p = Path(value)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"path must be relative to the set directory: {value!r}")
    return value


class BenchmarkPiece(StrictModel):
    """One reference piece: where its outputs land, its rubric and its target."""

    id: Slug
    capability: Slug
    rubric: Slug
    outputs: NonEmptyStr
    """Directory (relative to the set) holding rendered outputs; the newest is scored."""
    pattern: NonEmptyStr = "*.png"
    reference: NonEmptyStr
    """Reference piece (relative to the set) the output is judged against."""
    fixture: bool = False
    """True while the reference is a stand-in for the owner's reference video (#47)."""
    target: UnitFloat
    tolerance: UnitFloat = 0.05

    @model_validator(mode="after")
    def _paths(self) -> BenchmarkPiece:
        _relative(self.outputs)
        _relative(self.reference)
        return self


class BenchmarkSet(StrictModel):
    id: Slug
    title: NonEmptyStr
    pieces: list[BenchmarkPiece] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> BenchmarkSet:
        ensure_unique_ids(self.pieces, "piece")
        return self

    def piece(self, piece_id: str) -> BenchmarkPiece:
        for p in self.pieces:
            if p.id == piece_id:
                return p
        raise BenchmarkError(f"set {self.id!r} has no piece {piece_id!r}")


class CriterionScore(StrictModel):
    criterion: Slug
    score: UnitFloat
    notes: str = ""


class PieceScore(StrictModel):
    """The Critic's score of one output, with the human ratings stored next to it."""

    piece: Slug
    rubric: Slug
    output: str
    output_hash: ContentHash
    fixture: bool
    judge: str
    criteria: list[CriterionScore]
    score: UnitFloat
    target: UnitFloat
    meets_target: bool
    human_score: UnitFloat | None = None
    human_ratings: int = Field(default=0, ge=0)


class HumanRating(StrictModel):
    set_id: Slug
    piece: Slug
    output: str
    output_hash: ContentHash
    score: UnitFloat
    rater: Slug
    notes: str = ""
    rated_at: datetime


class CriticRecord(StrictModel):
    """One Critic score in ``scores.jsonl``: the history used for calibration."""

    set_id: Slug
    piece: Slug
    output_hash: ContentHash
    score: UnitFloat
    judge: str
    scored_at: datetime


class AgreementReport(StrictModel):
    """BlenderGym verifier agreement between the Critic and people."""

    protocol: Literal["blendergym-pairwise"] = "blendergym-pairwise"
    items: int
    pairs: int
    """Pairs where people prefer one output (human ties are excluded)."""
    human_ties: int
    agreements: int
    critic_ties: int
    """Pairs people separate but the Critic scores equal (counted as disagreement)."""
    agreement_rate: float | None
    spearman: float | None
    kendall_tau: float | None


class BenchmarkReport(StrictModel):
    set_id: Slug
    set_hash: ContentHash
    created_at: datetime
    pieces: list[PieceScore]
    agreement: AgreementReport


class Baseline(StrictModel):
    set_id: Slug
    scores: dict[Slug, UnitFloat]


class RegressionResult(StrictModel):
    piece: Slug
    baseline: UnitFloat
    score: UnitFloat | None
    tolerance: UnitFloat
    regressed: bool


# --------------------------------------------------------------------------- loading


def benchmarks_dir(path: Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(os.environ.get(BENCHMARKS_DIR_ENV) or "benchmarks")


def _load[M: BaseModel](model: type[M], path: Path) -> M:
    if not path.is_file():
        raise BenchmarkError(f"{path}: file not found")
    try:
        return model.model_validate_json(path.read_bytes())
    except ValidationError as exc:
        raise BenchmarkError(f"{path}: {exc}") from exc


def set_dir(set_id: str, root: Path | None = None) -> Path:
    return benchmarks_dir(root) / "sets" / set_id


def results_dir(set_id: str, root: Path | None = None) -> Path:
    return set_dir(set_id, root) / "results"


def load_set(set_id: str, root: Path | None = None) -> BenchmarkSet:
    bset = _load(BenchmarkSet, set_dir(set_id, root) / "set.json")
    if bset.id != set_id:
        raise BenchmarkError(f"{set_dir(set_id, root) / 'set.json'}: id {bset.id!r} != {set_id!r}")
    return bset


def load_rubric(rubric_id: str, root: Path | None = None) -> Rubric:
    path = benchmarks_dir(root) / "rubrics" / f"{rubric_id}.json"
    rubric = _load(Rubric, path)
    if rubric.id != rubric_id:
        raise BenchmarkError(f"{path}: id {rubric.id!r} != {rubric_id!r}")
    return rubric


def load_baseline(set_id: str, root: Path | None = None) -> Baseline:
    return _load(Baseline, set_dir(set_id, root) / "baseline.json")


def _read_jsonl[M: BaseModel](model: type[M], path: Path) -> list[M]:
    if not path.is_file():
        return []
    out: list[M] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                out.append(model.model_validate_json(line))
            except ValidationError as exc:
                raise BenchmarkError(f"{path}:{n}: {exc}") from exc
    return out


def _append_jsonl(path: Path, records: Iterable[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(canonical_json(r).decode("utf-8") + "\n")


def file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def latest_output(piece: BenchmarkPiece, sdir: Path) -> Path:
    """Newest file matching the piece's pattern; equal mtimes break by name."""
    files = [p for p in (sdir / piece.outputs).glob(piece.pattern) if p.is_file()]
    if not files:
        raise BenchmarkError(
            f"piece {piece.id!r}: no outputs match {piece.outputs}/{piece.pattern}"
        )
    return max(files, key=lambda p: (p.stat().st_mtime_ns, p.name))


# --------------------------------------------------------------------------- scoring


def _now() -> datetime:
    return datetime.now(UTC)


def score_output(
    output: Path, rubric: Rubric, judge: VisionJudge
) -> tuple[list[CriterionScore], float]:
    criteria: list[CriterionScore] = []
    total = weight = 0.0
    for c in rubric.criteria:
        verdict = judge.score(output, f"{rubric.title}: {c.prompt}")
        value = min(1.0, max(0.0, float(verdict.score)))
        criteria.append(CriterionScore(criterion=c.id, score=value, notes=verdict.notes))
        total += c.weight * value
        weight += c.weight
    return criteria, round(total / weight, 4)


def _human_means(ratings: Sequence[HumanRating]) -> dict[str, list[float]]:
    by_hash: dict[str, list[float]] = {}
    for r in ratings:
        by_hash.setdefault(r.output_hash, []).append(r.score)
    return by_hash


def run_set(
    set_id: str,
    *,
    root: Path | None = None,
    judge: VisionJudge | None = None,
    out_dir: Path | None = None,
) -> BenchmarkReport:
    """Score each piece's latest output per its rubric; write report.json and report.md."""
    if judge is None:
        from spectracut.providers.base import select_vision_judge

        judge = select_vision_judge()
    bset = load_set(set_id, root)
    sdir = set_dir(set_id, root)
    out = Path(out_dir) if out_dir is not None else results_dir(set_id, root)
    ratings = _human_means(_read_jsonl(HumanRating, out / "ratings.jsonl"))
    judge_name = str(getattr(judge, "name", type(judge).__name__))
    now = _now()
    pieces: list[PieceScore] = []
    for piece in bset.pieces:
        rubric = load_rubric(piece.rubric, root)
        output = latest_output(piece, sdir)
        criteria, score = score_output(output, rubric, judge)
        digest = file_hash(output)
        human = ratings.get(digest, [])
        pieces.append(
            PieceScore(
                piece=piece.id,
                rubric=rubric.id,
                output=output.relative_to(sdir).as_posix(),
                output_hash=digest,
                fixture=piece.fixture,
                judge=judge_name,
                criteria=criteria,
                score=score,
                target=piece.target,
                meets_target=score >= piece.target,
                human_score=round(sum(human) / len(human), 4) if human else None,
                human_ratings=len(human),
            )
        )
    _append_jsonl(
        out / "scores.jsonl",
        (
            CriticRecord(
                set_id=set_id,
                piece=p.piece,
                output_hash=p.output_hash,
                score=p.score,
                judge=judge_name,
                scored_at=now,
            )
            for p in pieces
        ),
    )
    report = BenchmarkReport(
        set_id=set_id,
        set_hash=content_hash(bset),
        created_at=now,
        pieces=pieces,
        agreement=set_agreement(set_id, root=root, out_dir=out),
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (out / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def render_markdown(report: BenchmarkReport) -> str:
    lines = [
        f"# Benchmark report: {report.set_id}",
        "",
        f"Created {report.created_at.isoformat()}; set {report.set_hash}.",
        "",
        "| piece | rubric | output | critic | human | target | meets | fixture |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for p in report.pieces:
        human = f"{p.human_score:.3f} (n={p.human_ratings})" if p.human_score is not None else "-"
        lines.append(
            f"| {p.piece} | {p.rubric} | {p.output} | {p.score:.3f} | {human} | "
            f"{p.target:.3f} | {'yes' if p.meets_target else 'no'} | "
            f"{'yes' if p.fixture else 'no'} |"
        )
    a = report.agreement
    rate = f"{a.agreement_rate:.3f}" if a.agreement_rate is not None else "n/a"
    lines += [
        "",
        f"Critic-vs-human agreement ({a.protocol}): {rate} over {a.pairs} pairs "
        f"({a.items} rated outputs, {a.human_ties} human ties excluded, "
        f"{a.critic_ties} critic ties).",
        "",
    ]
    return "\n".join(lines)


def load_report(set_id: str, root: Path | None = None) -> BenchmarkReport:
    return _load(BenchmarkReport, results_dir(set_id, root) / "report.json")


def write_baseline(report: BenchmarkReport, root: Path | None = None) -> Path:
    baseline = Baseline(set_id=report.set_id, scores={p.piece: p.score for p in report.pieces})
    path = set_dir(report.set_id, root) / "baseline.json"
    path.write_text(baseline.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- ratings


def record_rating(
    set_id: str,
    piece_id: str,
    score: float,
    *,
    rater: str = "owner",
    output: Path | None = None,
    notes: str = "",
    root: Path | None = None,
    out_dir: Path | None = None,
) -> HumanRating:
    """Append a person's rating of an output to ``ratings.jsonl`` beside the Critic's scores."""
    bset = load_set(set_id, root)
    piece = bset.piece(piece_id)
    sdir = set_dir(set_id, root)
    path = Path(output) if output is not None else latest_output(piece, sdir)
    if not path.is_absolute() and not path.is_file():
        path = sdir / path
    if not path.is_file():
        raise BenchmarkError(f"output not found: {path}")
    try:
        shown = path.resolve().relative_to(sdir.resolve()).as_posix()
    except ValueError:
        shown = str(path)
    try:
        rating = HumanRating(
            set_id=set_id,
            piece=piece.id,
            output=shown,
            output_hash=file_hash(path),
            score=score,
            rater=rater,
            notes=notes,
            rated_at=_now(),
        )
    except ValidationError as exc:
        raise BenchmarkError(str(exc)) from exc
    out = Path(out_dir) if out_dir is not None else results_dir(set_id, root)
    _append_jsonl(out / "ratings.jsonl", [rating])
    return rating


# --------------------------------------------------------------------------- regression


def check_regression(
    report: BenchmarkReport, baseline: Baseline, bset: BenchmarkSet
) -> list[RegressionResult]:
    """A piece regresses when ``baseline - score > tolerance`` or it was not scored."""
    scores = {p.piece: p.score for p in report.pieces}
    results: list[RegressionResult] = []
    for piece_id, base in sorted(baseline.scores.items()):
        tolerance = bset.piece(piece_id).tolerance
        score = scores.get(piece_id)
        regressed = score is None or (base - score) > tolerance + 1e-9
        results.append(
            RegressionResult(
                piece=piece_id, baseline=base, score=score, tolerance=tolerance, regressed=regressed
            )
        )
    return results


# --------------------------------------------------------------------------- agreement


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    n = len(x)
    if n < 2:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def agreement(critic: dict[str, float], human: dict[str, float]) -> AgreementReport:
    """BlenderGym verifier agreement over outputs scored by both the Critic and people.

    Every pair of outputs people separate counts once: the Critic agrees when it
    prefers the same output. Human ties are excluded; Critic ties count as
    disagreement. Spearman and Kendall tau-b give the rank correlation.
    """
    keys = sorted(set(critic) & set(human))
    c = [critic[k] for k in keys]
    h = [human[k] for k in keys]
    pairs = agree = human_ties = critic_ties = 0
    concordant = discordant = tie_c = tie_h = 0
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            dh = h[i] - h[j]
            dc = c[i] - c[j]
            if dh == 0:
                human_ties += 1
                tie_h += 1
                if dc == 0:
                    tie_c += 1
                continue
            pairs += 1
            if dc == 0:
                critic_ties += 1
                tie_c += 1
            elif (dc > 0) == (dh > 0):
                agree += 1
                concordant += 1
            else:
                discordant += 1
    total = len(keys) * (len(keys) - 1) // 2
    denom = math.sqrt((total - tie_c) * (total - tie_h)) if total else 0.0
    tau = round((concordant - discordant) / denom, 4) if denom else None
    rho = _pearson(_ranks(c), _ranks(h))
    return AgreementReport(
        items=len(keys),
        pairs=pairs,
        human_ties=human_ties,
        agreements=agree,
        critic_ties=critic_ties,
        agreement_rate=round(agree / pairs, 4) if pairs else None,
        spearman=round(rho, 4) if rho is not None else None,
        kendall_tau=tau,
    )


def set_agreement(
    set_id: str, *, root: Path | None = None, out_dir: Path | None = None
) -> AgreementReport:
    """Agreement over every output of a set with both a Critic score and a human rating."""
    out = Path(out_dir) if out_dir is not None else results_dir(set_id, root)
    critic: dict[str, float] = {}
    for rec in _read_jsonl(CriticRecord, out / "scores.jsonl"):
        critic[rec.output_hash] = rec.score  # latest Critic score per output
    human = {
        k: sum(v) / len(v)
        for k, v in _human_means(_read_jsonl(HumanRating, out / "ratings.jsonl")).items()
    }
    return agreement(critic, human)


# --------------------------------------------------------------------------- harness


class SceneObject(StrictModel):
    id: Slug
    shape: Literal["rect", "circle"]
    center: tuple[UnitFloat, UnitFloat]
    size: UnitFloat
    color: tuple[int, int, int]

    @model_validator(mode="after")
    def _rgb(self) -> SceneObject:
        if any(not 0 <= v <= 255 for v in self.color):
            raise ValueError("color channels must be 0-255")
        return self


class Camera(StrictModel):
    pan: tuple[float, float] = (0.0, 0.0)
    zoom: PosFloat = 1.0


class Scene(StrictModel):
    """A declarative scene: the only thing a harness solver produces."""

    background: tuple[int, int, int] = (0, 0, 0)
    light: Annotated[float, Field(ge=0.0, le=4.0)] = 1.0
    camera: Camera = Camera()
    objects: list[SceneObject] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> Scene:
        ensure_unique_ids(self.objects, "object")
        return self


class HarnessTask(StrictModel):
    """A headless task ported from a Blender benchmark harness.

    The solver sees :meth:`public` (start scene and instruction); the
    ``ground_truth`` scene stays hidden and is only used to score.
    """

    id: Slug
    source: Literal["sceneactbench", "3dcodebench", "blenderworld"]
    instruction: NonEmptyStr
    resolution: tuple[Annotated[int, Field(ge=4, le=256)], Annotated[int, Field(ge=4, le=256)]] = (
        64,
        36,
    )
    threshold: UnitFloat = 0.9
    start: Scene
    ground_truth: Scene

    def public(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"ground_truth"})


class HarnessResult(StrictModel):
    task: Slug
    resolution: tuple[int, int]
    pixel: UnitFloat
    params: UnitFloat
    score: UnitFloat
    threshold: UnitFloat
    passed: bool


def render(scene: Scene, width: int, height: int) -> list[Pixel]:
    """Rasterise a scene at ``width`` x ``height`` (row-major RGB)."""
    pixels: list[Pixel] = []
    zoom = scene.camera.zoom
    px, py = scene.camera.pan
    aspect = width / height
    for y in range(height):
        for x in range(width):
            u = ((x + 0.5) / width - 0.5) / zoom + 0.5 + px
            v = ((y + 0.5) / height - 0.5) / zoom + 0.5 + py
            color = scene.background
            for obj in scene.objects:
                dx = (u - obj.center[0]) * aspect
                dy = v - obj.center[1]
                half = obj.size / 2
                inside = (
                    abs(dx) <= half and abs(dy) <= half
                    if obj.shape == "rect"
                    else dx * dx + dy * dy <= half * half
                )
                if inside:
                    color = obj.color
            pixels.append(
                (
                    min(255, round(color[0] * scene.light)),
                    min(255, round(color[1] * scene.light)),
                    min(255, round(color[2] * scene.light)),
                )
            )
    return pixels


def png_bytes(pixels: Sequence[Pixel], width: int, height: int) -> bytes:
    """Encode RGB pixels as a PNG with the standard library only."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    raw = b"".join(
        b"\x00" + bytes(c for p in pixels[y * width : (y + 1) * width] for c in p)
        for y in range(height)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _param_score(candidate: Scene, truth: Scene) -> float:
    terms: list[float] = [
        1 - min(1.0, abs(candidate.light - truth.light)),
        1 - min(1.0, abs(candidate.camera.zoom - truth.camera.zoom)),
        1 - min(1.0, math.dist(candidate.camera.pan, truth.camera.pan)),
    ]
    found = {o.id: o for o in candidate.objects}
    for obj in truth.objects:
        got = found.get(obj.id)
        if got is None or got.shape != obj.shape:
            terms.append(0.0)
            continue
        terms.append(1 - min(1.0, math.dist(got.center, obj.center) / math.sqrt(2)))
        terms.append(1 - abs(got.size - obj.size))
        terms.append(1 - math.dist(got.color, obj.color) / math.dist((0, 0, 0), (255, 255, 255)))
    extra = len(set(found) - {o.id for o in truth.objects})
    terms.extend([0.0] * extra)
    return sum(terms) / len(terms)


def score_harness(
    task: HarnessTask, candidate: Scene, resolution: tuple[int, int] | None = None
) -> HarnessResult:
    """Score a candidate scene against the task's hidden ground truth at tiny resolution."""
    w, h = resolution or task.resolution
    got = render(candidate, w, h)
    want = render(task.ground_truth, w, h)
    diff = sum(
        abs(a - b) for pa, pb in zip(got, want, strict=True) for a, b in zip(pa, pb, strict=True)
    )
    pixel = 1 - diff / (255 * 3 * w * h)
    params = _param_score(candidate, task.ground_truth)
    score = round(0.5 * pixel + 0.5 * params, 4)
    return HarnessResult(
        task=task.id,
        resolution=(w, h),
        pixel=round(pixel, 4),
        params=round(params, 4),
        score=score,
        threshold=task.threshold,
        passed=score >= task.threshold,
    )


def load_harness_tasks(root: Path | None = None) -> list[HarnessTask]:
    tasks = [
        _load(HarnessTask, p) for p in sorted((benchmarks_dir(root) / "harness").glob("*.json"))
    ]
    ensure_unique_ids(tasks, "harness task")
    return tasks


def load_solution(path: Path) -> Scene:
    return _load(Scene, path)


def harness_self_check(
    task: HarnessTask, resolution: tuple[int, int] | None = None
) -> tuple[HarnessResult, HarnessResult]:
    """Score the start scene (must fail) and the ground truth (must pass)."""
    return score_harness(task, task.start, resolution), score_harness(
        task, task.ground_truth, resolution
    )


def write_png(path: Path, scene: Scene, width: int, height: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes(render(scene, width, height), width, height))
    return path
