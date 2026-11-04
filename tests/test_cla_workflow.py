# SPDX-License-Identifier: AGPL-3.0-or-later
"""The CLA workflow checks signatures, never runs PR code, and is report-only by default."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "cla.yml"
SIGN_PHRASE = "I have read the CLA Document and I hereby sign the CLA"


def load() -> dict[Any, Any]:
    data = yaml.safe_load(WORKFLOW.read_text())
    assert isinstance(data, dict)
    return data


def steps() -> list[dict[str, Any]]:
    return list(load()["jobs"]["cla"]["steps"])


def test_triggers() -> None:
    triggers = load().get(True) or load().get("on")
    assert isinstance(triggers, dict)
    assert "pull_request_target" in triggers
    assert "issue_comment" in triggers


def test_uses_cla_assistant_with_cla_document() -> None:
    step = next(s for s in steps() if s.get("id") == "cla")
    assert step["uses"].startswith("contributor-assistant/github-action@")
    assert "CLA.md" in step["with"]["path-to-document"]
    assert step["with"]["custom-pr-sign-comment"] == SIGN_PHRASE


def test_sign_phrase_matches_cla_document() -> None:
    assert SIGN_PHRASE in (ROOT / "CLA.md").read_text()
    assert SIGN_PHRASE in WORKFLOW.read_text()


def test_never_checks_out_pr_code() -> None:
    assert all("actions/checkout" not in s.get("uses", "") for s in steps())


def test_report_only_until_cla_enforce() -> None:
    step = next(s for s in steps() if s.get("id") == "cla")
    assert "vars.CLA_ENFORCE" in str(step["continue-on-error"])
    notice = next(s for s in steps() if "Report-only" in s.get("name", ""))
    assert "steps.cla.outcome == 'failure'" in notice["if"]
    assert "::warning::" in notice["run"]


def test_minimal_permissions_and_secrets() -> None:
    perms = load()["permissions"]
    assert set(perms) == {"actions", "contents", "pull-requests", "statuses"}
    text = WORKFLOW.read_text()
    assert text.count("secrets.") == 1
    assert "secrets.GITHUB_TOKEN" in text
