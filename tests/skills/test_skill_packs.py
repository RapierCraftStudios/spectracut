# SPDX-License-Identifier: AGPL-3.0-or-later
"""Department skill packs v1: packs load, rubrics are benchmarked, failure corpus (#28)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spectracut.cli.app import app
from spectracut.critique import benchmarks as bm
from spectracut.providers.mock_vision import MockVisionJudge
from spectracut.skills import M2_DEPARTMENTS, SKILLS_DIR, pack_dir
from spectracut.spec._base import StrictModel
from spectracut.studio.department import SKILLS_ROOT, Department, SkillPack

ROOT = Path(__file__).resolve().parents[2]
REPO_BENCH = ROOT / "benchmarks"

EXPECTED = (
    "writer",
    "director",
    "art-director",
    "storyboard",
    "editor",
    "lighter",
    "scene-builder",
    "compositor",
    "motion-designer",
    "sound-designer",
    "mixer",
    "critic",
)

#: Failure topic -> words its corpus file must mention.
CORPUS = {
    "haze": ("haze",),
    "dim-screens": ("screen", "dim"),
    "static-ui": ("static", "ui"),
    "real-brand-leaks": ("brand", "logo"),
    "repeated-push-ins": ("push-in",),
}


class _Out(StrictModel):
    text: str


# ------------------------------------------------------------------ R2: packs load


def test_m2_departments_are_listed() -> None:
    assert M2_DEPARTMENTS == EXPECTED
    assert SKILLS_DIR == SKILLS_ROOT
    assert sorted(
        p.name
        for p in SKILLS_DIR.iterdir()
        # story/ holds the story departments' nested packs (tests/studio/test_discovery.py).
        if p.is_dir() and p.name not in ("__pycache__", "story")
    ) == sorted(EXPECTED)


@pytest.mark.parametrize("slug", EXPECTED)
def test_every_m2_pack_loads_and_validates(slug: str) -> None:
    pack = SkillPack.load(pack_dir(slug))
    assert pack.prompt and pack.rubric
    assert pack.failures, f"{slug}: no failure corpus"
    assert pack.anchors, f"{slug}: no taste anchors"
    for path in (*pack.failures, *pack.anchors):
        assert path.read_text(encoding="utf-8").strip(), path
    for section in ("## Studio practice", "## Recipes", "## Guardrails"):
        assert section in pack.prompt, f"{slug}: prompt.md lacks {section}"


@pytest.mark.parametrize("slug", EXPECTED)
def test_department_named_after_pack_uses_the_shipped_pack(slug: str) -> None:
    dept: type[Department] = type(
        f"Dept_{slug.replace('-', '_')}",
        (Department,),
        {"name": slug, "produces": frozenset({"note"}), "output": _Out},
    )
    assert dept.pack_path() == pack_dir(slug)
    assert dept.rubric() == SkillPack.load(pack_dir(slug)).rubric


def test_packs_hold_no_code_or_secrets() -> None:
    secret = re.compile(
        r"\bsk-[A-Za-z0-9]{10,}|api[_-]?key\s*[=:]|token\s*[=:]|BEGIN [A-Z ]*PRIVATE KEY"
    )
    for slug in EXPECTED:
        for path in pack_dir(slug).rglob("*"):
            if path.is_file():
                assert path.suffix in {".md", ".json"}, path
                assert not secret.search(path.read_text(encoding="utf-8")), path


# ------------------------------------------------------------------ R3: rubrics benchmarked


@pytest.mark.parametrize("slug", EXPECTED)
def test_pack_rubric_json_matches_rubric_md(slug: str) -> None:
    rubric = bm.Rubric.model_validate_json((pack_dir(slug) / "rubric.json").read_bytes())
    assert rubric.id == slug
    text = (pack_dir(slug) / "rubric.md").read_text(encoding="utf-8")
    for criterion in rubric.criteria:
        assert f"## {criterion.id} " in text, f"{slug}: {criterion.id} missing from rubric.md"


def test_load_rubric_falls_back_to_skill_pack(tmp_path: Path) -> None:
    rubric = bm.load_rubric("lighter", tmp_path)
    assert rubric.id == "lighter"
    assert {c.id for c in rubric.criteria} >= {"exposure", "clarity"}
    # The benchmarks directory still wins, and unknown ids still fail on its path.
    (tmp_path / "rubrics").mkdir()
    (tmp_path / "rubrics" / "lighter.json").write_text(
        json.dumps({"id": "lighter", "title": "Local", "criteria": [{"id": "x", "prompt": "?"}]})
    )
    assert bm.load_rubric("lighter", tmp_path).title == "Local"
    with pytest.raises(bm.BenchmarkError, match=r"rubrics/no-such-rubric\.json: file not found"):
        bm.load_rubric("no-such-rubric", tmp_path)


def test_every_pack_rubric_is_used_by_a_repo_benchmark_set() -> None:
    used: set[str] = set()
    for set_file in (REPO_BENCH / "sets").glob("*/set.json"):
        used |= {p.rubric for p in bm.load_set(set_file.parent.name, REPO_BENCH).pieces}
    assert set(EXPECTED) <= used
    local = {p.stem for p in (REPO_BENCH / "rubrics").glob("*.json")}
    assert not local & set(EXPECTED), "a benchmarks/rubrics file would shadow a pack rubric"


def test_benchmark_runner_scores_every_department_rubric(tmp_path: Path) -> None:
    report = bm.run_set("departments", root=REPO_BENCH, judge=MockVisionJudge(), out_dir=tmp_path)
    scored = {p.piece: p for p in report.pieces}
    assert set(scored) == set(EXPECTED)
    for slug, piece in scored.items():
        assert piece.rubric == slug
        rubric = bm.load_rubric(slug, REPO_BENCH)
        assert [c.criterion for c in piece.criteria] == [c.id for c in rubric.criteria]


def test_departments_set_passes_its_baseline_with_mock_critic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPECTRACUT_PROVIDERS", "mock")
    result = CliRunner().invoke(
        app,
        [
            "bench",
            "check",
            "departments",
            "--benchmarks-dir",
            str(REPO_BENCH),
            "--out",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output


# ------------------------------------------------------------------ R4: failure corpus


@pytest.mark.parametrize("topic", sorted(CORPUS))
def test_failure_corpus_covers_required_topics(topic: str) -> None:
    owners = [s for s in EXPECTED if (pack_dir(s) / "failures" / f"{topic}.md").is_file()]
    assert "critic" in owners and len(owners) >= 2, f"{topic}: owners {owners}"
    for slug in owners:
        text = (pack_dir(slug) / "failures" / f"{topic}.md").read_text(encoding="utf-8").lower()
        for word in CORPUS[topic]:
            assert word in text, f"{slug}/failures/{topic}.md does not mention {word!r}"
        for heading in ("## what went wrong", "## how to detect it", "## the fix"):
            assert heading in text, f"{slug}/failures/{topic}.md lacks {heading}"
