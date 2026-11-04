# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail unless a release tag matches every published package version.

Usage: ``python scripts/check_release_version.py vX.Y.Z``. Compares the tag (without
its leading ``v``) with the versions of ``spectracut`` (pyproject.toml),
``spectracut-host`` (host/pyproject.toml) and ``@spectracut/capture``
(packages/capture/package.json). Exits 1 and lists every mismatch, or when the tag
is not ``vMAJOR.MINOR.PATCH``.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")
PYPROJECTS = ("pyproject.toml", "host/pyproject.toml")
PACKAGE_JSONS = ("packages/capture/package.json",)


def tag_version(tag: str) -> str:
    """Return ``X.Y.Z`` for a ``vX.Y.Z`` tag, or raise ``ValueError``."""
    match = TAG_RE.match(tag.strip())
    if match is None:
        raise ValueError(f"tag {tag!r} is not vMAJOR.MINOR.PATCH")
    return match.group(1)


def package_versions(root: Path = ROOT) -> dict[str, str]:
    """Map each released package to the version its manifest declares."""
    versions: dict[str, str] = {}
    for rel in PYPROJECTS:
        project = tomllib.loads((root / rel).read_text(encoding="utf-8"))["project"]
        versions[str(project["name"])] = str(project["version"])
    for rel in PACKAGE_JSONS:
        manifest = json.loads((root / rel).read_text(encoding="utf-8"))
        versions[str(manifest["name"])] = str(manifest["version"])
    return versions


def mismatches(tag: str, root: Path = ROOT) -> list[str]:
    """Human-readable mismatches between the tag and the package versions."""
    expected = tag_version(tag)
    return [
        f"{name} is {found}, tag {tag} expects {expected}"
        for name, found in package_versions(root).items()
        if found != expected
    ]


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: check_release_version.py vX.Y.Z", file=sys.stderr)
        return 1
    try:
        problems = mismatches(args[0])
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
