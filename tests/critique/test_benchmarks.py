# SPDX-License-Identifier: AGPL-3.0-or-later
"""Benchmark films: runner, ratings, regression check, fixtures, harness and agreement (#46)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spectracut.cli.app import app
from spectracut.critique import benchmarks as bm
from spectracut.providers.base import VisionScore

ROOT = Path(__file__).resolve().parents[2]
REPO_BENCH = ROOT / "benchmarks"
runner = CliRunner()


class FakeJudge:
    """Scores a frame by its file name; records every rubric prompt it sees."""

    name = "fake-critic"

    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores
        self.prompts: list[str] = []

    def score(self, frame_path: Path, rubric: str) -> VisionScore:
        self.prompts.append(rubric)
        return VisionScore(score=self.scores[Path(frame_path).name], notes="fake")


def _png(path: Path, color: tuple[int, int, int]) -> Path:
    scene = bm.Scene(background=color)
    return bm.write_png(path, scene, 8, 6)


@pytest.fixture
def bench(tmp_path: Path) -> Path:
    root = tmp_path / "benchmarks"
    (root / "rubrics").mkdir(parents=True)
    rubric = {
        "id": "ad",
        "title": "Ad",
        "criteria": [
            {"id": "legibility", "prompt": "Is text readable?", "weight": 3.0},
            {"id": "polish", "prompt": "Is it polished?", "weight": 1.0},
        ],
    }
    (root / "rubrics" / "ad.json").write_text(json.dumps(rubric))
    sdir = root / "sets" / "demo"
    piece = {
        "id": "spot",
        "capability": "short-ad",
        "rubric": "ad",
        "outputs": "outputs/spot",
        "reference": "references/spot.png",
        "fixture": True,
        "target": 0.6,
        "tolerance": 0.05,
    }
    (sdir).mkdir(parents=True)
    (sdir / "set.json").write_text(json.dumps({"id": "demo", "title": "Demo", "pieces": [piece]}))
    _png(sdir / "references" / "spot.png", (10, 10, 10))
    old = _png(sdir / "outputs" / "spot" / "take-001.png", (50, 0, 0))
    new = _png(sdir / "outputs" / "spot" / "take-002.png", (0, 50, 0))
    os.utime(old, ns=(1_000_000_000, 1_000_000_000))
    os.utime(new, ns=(2_000_000_000, 2_000_000_000))
    return root


# ------------------------------------------------------------------ R3: bench run


def test_run_scores_latest_output_per_rubric_and_writes_report(bench: Path) -> None:
    judge = FakeJudge({"take-001.png": 0.1, "take-002.png": 0.8})
    report = bm.run_set("demo", root=bench, judge=judge)
    [piece] = report.pieces
    assert piece.output == "outputs/spot/take-002.png"
    assert piece.score == pytest.approx(0.8)
    assert [c.criterion for c in piece.criteria] == ["legibility", "polish"]
    assert judge.prompts == ["Ad: Is text readable?", "Ad: Is it polished?"]
    assert piece.meets_target and piece.fixture and piece.judge == "fake-critic"
    out = bench / "sets" / "demo" / "results"
    assert bm.load_report("demo", bench) == report
    assert "| spot | ad | outputs/spot/take-002.png | 0.800 |" in (out / "report.md").read_text()
    assert len((out / "scores.jsonl").read_text().splitlines()) == 1


def test_weighted_score() -> None:
    class Split:
        name = "split"

        def score(self, frame_path: Path, rubric: str) -> VisionScore:
            return VisionScore(score=1.0 if "readable" in rubric else 0.0)

    rubric = bm.Rubric(
        id="r",
        title="R",
        criteria=[
            bm.Criterion(id="a", prompt="readable", weight=3.0),
            bm.Criterion(id="b", prompt="polish", weight=1.0),
        ],
    )
    _, score = bm.score_output(Path("x.png"), rubric, Split())
    assert score == 0.75


def test_cli_run_with_mock_critic(bench: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPECTRACUT_PROVIDERS", "mock")
    result = runner.invoke(app, ["bench", "run", "demo", "--benchmarks-dir", str(bench)])
    assert result.exit_code == 0, result.output
    assert "spot: critic=" in result.output
    report = json.loads((bench / "sets/demo/results/report.json").read_text())
    assert report["pieces"][0]["judge"] == "mock-vision"
    assert report["pieces"][0]["output"] == "outputs/spot/take-002.png"


def test_invalid_files_fail_with_path_and_field(bench: Path) -> None:
    (bench / "rubrics" / "ad.json").write_text(json.dumps({"id": "ad", "title": "Ad"}))
    with pytest.raises(bm.BenchmarkError, match=r"(?s)ad\.json.*criteria"):
        bm.load_rubric("ad", bench)
    set_json = bench / "sets/demo/set.json"
    data = json.loads(set_json.read_text())
    data["pieces"][0]["target"] = 1.5
    set_json.write_text(json.dumps(data))
    with pytest.raises(bm.BenchmarkError, match=r"(?s)set\.json.*pieces\.0\.target"):
        bm.load_set("demo", bench)
    result = runner.invoke(app, ["bench", "run", "demo", "--benchmarks-dir", str(bench)])
    assert result.exit_code == 2


# ------------------------------------------------------------------ R4: bench rate


def test_rate_is_stored_next_to_critic_score(bench: Path) -> None:
    bm.run_set("demo", root=bench, judge=FakeJudge({"take-001.png": 0.1, "take-002.png": 0.8}))
    result = runner.invoke(
        app,
        ["bench", "rate", "demo", "spot", "0.7", "--rater", "yash", "--benchmarks-dir", str(bench)],
    )
    assert result.exit_code == 0, result.output
    assert "human=0.700 critic=0.800" in result.output
    out = bench / "sets/demo/results"
    [line] = (out / "ratings.jsonl").read_text().splitlines()
    rating = bm.HumanRating.model_validate_json(line)
    report = bm.load_report("demo", bench)
    assert rating.output_hash == report.pieces[0].output_hash
    assert rating.output == "outputs/spot/take-002.png" and rating.rater == "yash"
    again = bm.run_set(
        "demo", root=bench, judge=FakeJudge({"take-002.png": 0.8, "take-001.png": 0})
    )
    assert again.pieces[0].human_score == 0.7 and again.pieces[0].human_ratings == 1


def test_rate_specific_output_and_rejects_bad_score(bench: Path) -> None:
    rating = bm.record_rating(
        "demo", "spot", 0.2, output=Path("outputs/spot/take-001.png"), root=bench
    )
    assert rating.output == "outputs/spot/take-001.png"
    result = runner.invoke(
        app, ["bench", "rate", "demo", "spot", "1.5", "--benchmarks-dir", str(bench)]
    )
    assert result.exit_code == 2
    result = runner.invoke(
        app, ["bench", "rate", "demo", "nope", "0.5", "--benchmarks-dir", str(bench)]
    )
    assert result.exit_code == 2 and "no piece" in result.output


# ------------------------------------------------------------------ R5: regression


def _baseline(bench: Path, score: float) -> None:
    (bench / "sets/demo/baseline.json").write_text(
        json.dumps({"set_id": "demo", "scores": {"spot": score}})
    )


@pytest.mark.parametrize(
    ("baseline", "score", "regressed"),
    [
        (0.8, 0.8, False),
        (0.8, 0.76, False),
        (0.8, 0.75, False),
        (0.8, 0.74, True),
        (0.5, 0.9, False),
    ],
)
def test_check_regression_tolerance(
    bench: Path, baseline: float, score: float, regressed: bool
) -> None:
    report = bm.run_set(
        "demo", root=bench, judge=FakeJudge({"take-001.png": 0, "take-002.png": score})
    )
    _baseline(bench, baseline)
    [result] = bm.check_regression(
        report, bm.load_baseline("demo", bench), bm.load_set("demo", bench)
    )
    assert result.regressed is regressed


def test_missing_piece_regresses(bench: Path) -> None:
    report = bm.run_set("demo", root=bench, judge=FakeJudge({"take-001.png": 0, "take-002.png": 1}))
    gone = report.model_copy(update={"pieces": []})
    _baseline(bench, 0.5)
    [result] = bm.check_regression(
        gone, bm.load_baseline("demo", bench), bm.load_set("demo", bench)
    )
    assert result.regressed and result.score is None


def test_cli_check_fails_on_drop_beyond_tolerance(
    bench: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPECTRACUT_PROVIDERS", "mock")
    args = ["bench", "run", "demo", "--write-baseline", "--benchmarks-dir", str(bench)]
    assert runner.invoke(app, args).exit_code == 0
    ok = runner.invoke(app, ["bench", "check", "demo", "--benchmarks-dir", str(bench)])
    assert ok.exit_code == 0, ok.output
    baseline = bm.load_baseline("demo", bench)
    _baseline(bench, min(1.0, baseline.scores["spot"] + 0.051))
    bad = runner.invoke(app, ["bench", "check", "demo", "--benchmarks-dir", str(bench)])
    assert bad.exit_code == 1
    assert "REGRESSED" in bad.output


# ------------------------------------------------------------------ R6: fixture references


def test_repo_fixture_set_uses_fixture_references() -> None:
    bset = bm.load_set("fixtures", REPO_BENCH)
    assert {p.id for p in bset.pieces} == {
        "device-launch",
        "app-walkthrough",
        "ad-30s",
        "title-sequence",
    }
    sdir = bm.set_dir("fixtures", REPO_BENCH)
    for piece in bset.pieces:
        assert piece.fixture, piece.id
        assert (sdir / piece.reference).is_file()
        bm.load_rubric(piece.rubric, REPO_BENCH)
        bm.latest_output(piece, sdir)
    assert "#47" in (REPO_BENCH / "README.md").read_text()


def test_repo_fixture_set_passes_its_baseline_with_mock_critic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPECTRACUT_PROVIDERS", "mock")
    result = runner.invoke(
        app,
        ["bench", "check", "fixtures", "--benchmarks-dir", str(REPO_BENCH), "--out", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "report.json").is_file()


# ------------------------------------------------------------------ R7 / R2: harness


def test_repo_harness_tasks_run_headless_at_tiny_resolution() -> None:
    tasks = bm.load_harness_tasks(REPO_BENCH)
    assert {t.source for t in tasks} == {"sceneactbench", "3dcodebench", "blenderworld"}
    for task in tasks:
        w, h = task.resolution
        assert w * h <= 64 * 64
        start, truth = bm.harness_self_check(task)
        assert truth.passed and truth.score == 1.0, task.id
        assert not start.passed, task.id
        assert "ground_truth" not in task.public()


def test_cli_harness_self_check_and_solutions(tmp_path: Path) -> None:
    ok = runner.invoke(app, ["bench", "harness", "--benchmarks-dir", str(REPO_BENCH)])
    assert ok.exit_code == 0, ok.output
    assert "64x36" in ok.output
    for task in bm.load_harness_tasks(REPO_BENCH):
        (tmp_path / f"{task.id}.json").write_text(task.start.model_dump_json())
    bad = runner.invoke(
        app,
        ["bench", "harness", "--benchmarks-dir", str(REPO_BENCH), "--solutions", str(tmp_path)],
    )
    assert bad.exit_code == 1 and "FAIL" in bad.output


def test_render_and_png_are_tiny_and_deterministic(tmp_path: Path) -> None:
    scene = bm.Scene(
        objects=[
            bm.SceneObject(id="a", shape="circle", center=(0.5, 0.5), size=0.5, color=(255, 0, 0))
        ]
    )
    pixels = bm.render(scene, 16, 9)
    assert len(pixels) == 16 * 9 and pixels[0] == (0, 0, 0) and pixels[8 + 16 * 4] == (255, 0, 0)
    a = bm.write_png(tmp_path / "a.png", scene, 16, 9).read_bytes()
    assert a.startswith(b"\x89PNG\r\n\x1a\n") and a == bm.png_bytes(pixels, 16, 9)


# ------------------------------------------------------------------ R8: agreement


def test_agreement_matches_hand_computed_blendergym_protocol() -> None:
    critic = {"a": 0.9, "b": 0.5, "c": 0.5, "d": 0.1}
    human = {"a": 0.8, "b": 0.6, "c": 0.2, "d": 0.2, "x": 1.0}
    # pairs (human-separated): ab agree, ac agree, ad agree, bc critic-tie,
    # bd agree; cd is a human tie (excluded).
    report = bm.agreement(critic, human)
    assert report.protocol == "blendergym-pairwise"
    assert (report.items, report.pairs, report.human_ties) == (4, 5, 1)
    assert (report.agreements, report.critic_ties) == (4, 1)
    assert report.agreement_rate == 0.8
    # ranks: critic a4 b2.5 c2.5 d1; human a4 b3 c1.5 d1.5 -> Pearson of ranks 3.75 / 4.5
    assert report.spearman == pytest.approx(0.8333, abs=1e-4)
    # tau-b: C=4, D=0, n0=6, ties critic=1, human=1 -> 4/5
    assert report.kendall_tau == pytest.approx(0.8, abs=1e-4)


def test_agreement_disagreement_and_empty() -> None:
    flipped = bm.agreement({"a": 0.1, "b": 0.9}, {"a": 0.9, "b": 0.1})
    assert flipped.agreement_rate == 0.0 and flipped.kendall_tau == -1.0
    empty = bm.agreement({}, {})
    assert empty.agreement_rate is None and empty.spearman is None and empty.kendall_tau is None


def test_agreement_reported_from_ratings(bench: Path) -> None:
    sdir = bench / "sets/demo"
    judge = FakeJudge({"take-001.png": 0.3, "take-002.png": 0.8})
    old = sdir / "outputs/spot/take-001.png"
    # Score the older take first by making it the latest, then the newer one.
    os.utime(old, ns=(3_000_000_000, 3_000_000_000))
    bm.run_set("demo", root=bench, judge=judge)
    bm.record_rating("demo", "spot", 0.2, output=old, root=bench)
    os.utime(old, ns=(1_000_000_000, 1_000_000_000))
    bm.record_rating("demo", "spot", 0.9, root=bench)
    report = bm.run_set("demo", root=bench, judge=judge)
    assert report.agreement.items == 2 and report.agreement.agreement_rate == 1.0
    assert "agreement (blendergym-pairwise): 1.000" in (sdir / "results/report.md").read_text()
    result = runner.invoke(app, ["bench", "agreement", "demo", "--benchmarks-dir", str(bench)])
    assert result.exit_code == 0 and json.loads(result.output)["pairs"] == 1
