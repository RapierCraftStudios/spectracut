# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SPDX header check passes on the repo and catches files without a header."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_spdx.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=ROOT, capture_output=True, text=True
    )


def test_repo_sources_all_carry_header() -> None:
    result = run()
    assert result.returncode == 0, result.stderr


def test_missing_header_fails_and_names_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.ts"
    bad.write_text("export const x = 1;\n")
    result = run(str(bad))
    assert result.returncode == 1
    assert str(bad) in result.stderr


def test_header_in_first_lines_passes(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text("#!/usr/bin/env python\n# SPDX-License-Identifier: AGPL-3.0-or-later\n")
    assert run(str(good)).returncode == 0


def test_header_too_late_fails(tmp_path: Path) -> None:
    late = tmp_path / "late.py"
    late.write_text("\n" * 5 + "# SPDX-License-Identifier: AGPL-3.0-or-later\n")
    assert run(str(late)).returncode == 1


def test_non_source_files_are_ignored(tmp_path: Path) -> None:
    doc = tmp_path / "notes.md"
    doc.write_text("no header\n")
    assert run(str(doc)).returncode == 0
