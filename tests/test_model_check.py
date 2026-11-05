# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static checks for the local model selection (issue #49).

The real measurement runs on legion (``scripts/model_check/legion_smoke.sh``); these tests keep
the doc, the scripts and the pure helpers in ``scripts/model_check/common.py`` consistent without
a GPU.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "engineering" / "local-model-selection.md"
SCRIPTS = ROOT / "scripts" / "model_check"
SPDX = "SPDX-License-Identifier: AGPL-3.0-or-later"

sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402
import dl_queue  # noqa: E402
import run_cli  # noqa: E402

JOBS = [
    "Sound effects",
    "Video-to-audio foley",
    "Music",
    "Temp TTS",
    "Omni audio critic",
    "Image",
]
PICK_COLUMNS = [
    "Job",
    "Pick",
    "Fallback",
    "Licence",
    "Weights source",
    "VRAM (legion)",
    "Seconds per output",
    "Reason it won",
]
MEASURED_CARDS = ("legion 8 GB", "12 GB (info)", "8 GB emulated")
NON_COMMERCIAL = re.compile(r"\bNC\b|non-commercial|CC-BY-NC", re.IGNORECASE)


def _section(text: str, heading: str) -> str:
    m = re.search(rf"^#{{2,3}} {re.escape(heading)}\n(.*?)(?=^#{{2,3}} |\Z)", text, re.M | re.S)
    assert m, f"missing section {heading!r}"
    return m.group(1)


def _first_table(body: str) -> list[dict[str, str]]:
    lines = [ln for ln in body.splitlines() if ln.startswith("|")]
    rows: list[list[str]] = []
    for ln in lines:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if rows and len(cells) != len(rows[0]):
            break
        rows.append(cells)
    assert len(rows) >= 2 and set(rows[1][0]) <= set("-:"), "no markdown table"
    head = rows[0]
    return [dict(zip(head, r, strict=True)) for r in rows[2:]]


@pytest.fixture(scope="module")
def doc() -> str:
    assert DOC.is_file()
    return DOC.read_text(encoding="utf-8")


def test_doc_is_complete(doc: str) -> None:
    assert not re.search(r"\b[A-Z0-9]+_(CARD|VRAM|SECS|NOTES)\b", doc), "unfilled placeholder"
    assert "legion" in _section(doc, "Hosts")
    assert "informational" in _section(doc, "Hosts")


@pytest.mark.parametrize("job", JOBS)
def test_each_job_lists_candidates_with_measurements(doc: str, job: str) -> None:
    rows = _first_table(_section(doc, job))
    assert 2 <= len(rows) <= 4, f"{job}: expected 2-4 candidate rows, got {len(rows)}"
    for r in rows:
        assert r["Model"] and r["Licence"] and r["Weights source"], r
        assert not NON_COMMERCIAL.search(r["Licence"]), f"{job}: NC model in candidates: {r}"
        if r["Card"] in MEASURED_CARDS:
            assert re.search(r"\d", r["Peak VRAM"]), r
            assert re.search(r"\d", r["Seconds per output"]), r
        else:
            assert r["Card"] == "not measured", r
            assert len(r["Quality notes"]) > 20, f"{job}: unmeasured row needs the blocker: {r}"


def test_measured_on_legion(doc: str) -> None:
    measured = [
        (job, r["Model"])
        for job in JOBS
        for r in _first_table(_section(doc, job))
        if r["Card"] == "legion 8 GB"
    ]
    jobs = {job for job, _ in measured}
    assert {"Temp TTS", "Omni audio critic", "Image"} <= jobs, measured
    tts = [m for job, m in measured if job == "Temp TTS"]
    assert len(tts) >= 2, measured
    support = _first_table(_section(doc, "ASR / alignment and CLAP (supporting models)"))
    assert all(r["Card"] == "legion 8 GB" for r in support if "12 GB" not in r["Card"]), support


def test_unmeasured_picks_are_listed_as_open_runs(doc: str) -> None:
    """A pick or fallback without a legion number must say so and have an open run listed."""
    rows = _first_table(_section(doc, "Picks"))
    open_runs = {r["Job"] for r in _first_table(_section(doc, "Still to measure on legion"))}
    run_name = {"Video-to-audio foley": "Foley", "Omni audio critic": "Omni critic"}
    for r in rows:
        if "not measured" in r["VRAM (legion)"] or "emulated" in r["VRAM (legion)"]:
            job = run_name.get(r["Job"], r["Job"])
            assert job in open_runs, f"{r['Job']}: unmeasured pick without an open run"


def test_picks_table(doc: str) -> None:
    rows = _first_table(_section(doc, "Picks"))
    assert list(rows[0]) == PICK_COLUMNS
    jobs = [r["Job"] for r in rows]
    for job in JOBS:
        assert job in jobs, f"no pick row for {job}"
    for r in rows:
        for col in PICK_COLUMNS:
            if col == "Fallback" and r["Job"] in {"ASR / alignment", "CLAP"}:
                continue
            assert r[col] and r[col] != "-", f"{r['Job']}: empty {col}"
        assert not NON_COMMERCIAL.search(r["Licence"]), r
        assert "Stable Audio" not in r["Pick"] + r["Fallback"], "gated model picked"


def test_image_to_3d_and_motion_point_to_followup(doc: str) -> None:
    body = _section(doc, "Image-to-3D and motion")
    assert "#125" in body


def test_owner_action_for_stable_audio(doc: str) -> None:
    body = _section(doc, "Owner actions")
    assert "Stable Audio Open" in body and "HF_TOKEN" in body


def _script_files() -> list[Path]:
    return sorted(p for p in SCRIPTS.iterdir() if p.suffix in {".py", ".ps1", ".sh"})


def test_scripts_carry_spdx_header() -> None:
    files = _script_files()
    assert (SCRIPTS / "legion_smoke.sh") in files
    for p in files:
        head = p.read_text(encoding="utf-8").splitlines()[:3]
        assert any(SPDX in ln for ln in head), p.name


def test_no_hf_token_literal() -> None:
    for p in [*_script_files(), DOC]:
        assert not re.search(r"hf_[A-Za-z0-9]{20,}", p.read_text(encoding="utf-8")), p.name


def test_parse_nvidia_smi() -> None:
    assert common.parse_nvidia_smi("1055, 8192\n") == (1055, 8192)


def test_build_and_append_result(tmp_path: Path) -> None:
    rec = common.build_record(
        "tts", "Kokoro-82M", "voice=af_heart", host="Legion", ok=True, seconds=1.234,
        peak_vram_mib=1055, extra={"path": "out/kokoro.wav"},
    )  # fmt: skip
    for key in common.RESULT_KEYS:
        assert key in rec
    assert rec["seconds"] == 1.23
    out = tmp_path / "out" / "results.jsonl"
    common.append_result(out, rec)
    common.append_result(out, {**rec, "job": "image"})
    lines = out.read_text(encoding="utf-8").splitlines()
    assert [json.loads(ln)["job"] for ln in lines] == ["tts", "image"]
    rows = common.read_results(["\ufeff" + lines[0], "noise", lines[1]])
    assert len(rows) == 2
    table = common.results_table(rows)
    assert "| tts | Kokoro-82M | Legion |" in table and "| 1055 |" in table


def test_build_record_refuses_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "not-a-real-token-123")
    with pytest.raises(ValueError, match="HF_TOKEN"):
        common.build_record(
            "sfx", "x", "token=not-a-real-token-123", host="h", ok=True, seconds=1,
            peak_vram_mib=1,
        )  # fmt: skip


def test_run_cli_split_args() -> None:
    head, cmd = run_cli.split_args(["foley", "m", "s", "C:/r", "--", "python", "infer.py"])
    assert head == ["foley", "m", "s", "C:/r"] and cmd == ["python", "infer.py"]
    with pytest.raises(SystemExit):
        run_cli.split_args(["foley", "m", "--", "python"])


def test_dl_queue_select() -> None:
    assert dl_queue.select(dl_queue.QUEUE, ["Qwen3-TTS"]) == ["Qwen/Qwen3-TTS-12Hz-0.6B-Base"]
    assert len(dl_queue.select(dl_queue.QUEUE, [])) == len(dl_queue.QUEUE)
