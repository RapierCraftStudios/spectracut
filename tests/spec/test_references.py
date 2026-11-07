# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reference set (#342): craft notes from references.md, keyframes from the local store."""

from __future__ import annotations

import builtins
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from spectracut.spec.references import (
    DEFAULT_REFERENCE_SET,
    ReferenceError,
    load_reference_set,
    parse_references,
)

REPO = Path(__file__).parents[2]
BENCHMARKS = REPO / "benchmarks"

BASE44_POINTS = {
    "base44-launch:kinetic-type-as-the-spine": "type",
    "base44-launch:real-ui-as-the-hero": "ui",
    "base44-launch:rhythm-of-light-and-dark": "palette",
}


def test_parses_the_four_references_with_their_measured_pace() -> None:
    refs = load_reference_set(DEFAULT_REFERENCE_SET, root=BENCHMARKS)
    assert refs.id == "references-v1"
    pace = {r.id: r.avg_shot_s for r in refs.references}
    assert pace == {
        "base44-launch": 1.3,
        "elevenagents-architect": 5.2,
        "nothing-headphone-xray": 33.0,
        "apple-macbook-pro": 2.6,
    }
    assert {r.id: r.cuts for r in refs.references}["base44-launch"] == 38


def test_base44_craft_points_have_stable_ids_and_aspects() -> None:
    refs = load_reference_set(root=BENCHMARKS)
    for point_id, aspect in BASE44_POINTS.items():
        point = refs.point(point_id)
        assert point is not None, point_id
        assert point.aspect == aspect
        assert point.text
    assert refs.point("base44-launch:pace") is not None
    assert refs.point("base44-launch:nope") is None
    assert [r.id for r in refs.cited(list(BASE44_POINTS))] == ["base44-launch"]
    assert refs.unknown(["base44-launch:nope", *BASE44_POINTS]) == ["base44-launch:nope"]


def test_keyframes_resolve_from_the_store_only_when_present(tmp_path: Path) -> None:
    empty = load_reference_set(root=BENCHMARKS, store=tmp_path / "missing")
    assert all(r.keyframes == [] for r in empty.references)

    store = tmp_path / "store"
    (store / "keyframes").mkdir(parents=True)
    frame = store / "keyframes" / "base44-launch.png"
    frame.write_bytes(b"png")
    found = {r.id: r.keyframes for r in load_reference_set(root=BENCHMARKS, store=store).references}
    assert found["base44-launch"] == [str(frame.resolve())]
    assert found["apple-macbook-pro"] == []


def test_never_reads_a_video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / "store"
    (store / "keyframes").mkdir(parents=True)
    (store / "base44-launch.mp4").write_bytes(b"video")
    opened: list[str] = []
    real_open = builtins.open

    def spy(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail(f"read {self}"))
    load_reference_set(root=BENCHMARKS, store=store)
    assert not [p for p in opened if p.endswith((".mp4", ".mov", ".webm"))]


def test_a_piece_without_notes_or_a_missing_set_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ReferenceError, match="cannot read"):
        load_reference_set("nope", root=BENCHMARKS)
    root = tmp_path / "bench"
    (root / "sets" / "s").mkdir(parents=True)
    (root / "sets" / "s" / "set.json").write_text(json.dumps({"pieces": [{"id": "ghost"}]}))
    shutil.copy(BENCHMARKS / "references.md", root / "references.md")
    with pytest.raises(ReferenceError, match="ghost"):
        load_reference_set("s", root=root)


def test_parse_ignores_rows_without_a_section() -> None:
    md = "| `x` | src | fmt | 1 s | 3 | 0.5 s | l | sha |\n"
    assert parse_references(md)["x"].craft == []


def test_reference_media_link_is_git_ignored() -> None:
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    out = subprocess.run(
        ["git", "check-ignore", "benchmarks/sets/references-v1/references"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    tracked = subprocess.run(
        ["git", "ls-files", "benchmarks"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    assert not [p for p in tracked if p.endswith((".mp4", ".mov", ".webm"))]
