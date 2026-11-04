# SPDX-License-Identifier: AGPL-3.0-or-later
"""Licensing documents exist, cross-link, and replace the old placeholders."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text()


def test_documents_exist() -> None:
    for name in ("CLA.md", "COMMERCIAL.md", "docs/licensing.md"):
        assert (ROOT / name).is_file(), name


def test_licensing_guide_covers_obligations() -> None:
    text = read("docs/licensing.md")
    for needle in (
        "AGPL-3.0-or-later",
        "Self-hosters",
        "SaaS and network operators",
        "section 13",
        "Buying a commercial licence",
        "COMMERCIAL.md",
        "CLA.md",
    ):
        assert needle in text, needle


def test_commercial_links_guide() -> None:
    assert "docs/licensing.md" in read("COMMERCIAL.md")


def test_placeholders_replaced() -> None:
    readme, contributing = read("README.md"), read("CONTRIBUTING.md")
    assert "coming with the governance issue" not in readme
    assert "added by the governance work" not in contributing
    assert "(COMMERCIAL.md)" in readme
    assert "(CLA.md)" in readme
    assert "(CLA.md)" in contributing
