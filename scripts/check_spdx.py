# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail if a tracked source file lacks the AGPL-3.0-or-later SPDX header.

Usage: ``python scripts/check_spdx.py [PATH ...]``. With no paths, checks every
file git tracks or would track (not ignored). Exits 1 and lists offending files
when any header is missing.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

HEADER = "SPDX-License-Identifier: AGPL-3.0-or-later"
SOURCE_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
HEAD_LINES = 5


def tracked_files(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout.decode()
    return [root / name for name in out.split("\0") if name]


def has_header(path: Path) -> bool:
    with path.open(encoding="utf-8", errors="replace") as fh:
        for _, line in zip(range(HEAD_LINES), fh, strict=False):
            if HEADER in line:
                return True
    return False


def missing_headers(paths: Iterable[Path]) -> list[Path]:
    return [p for p in paths if p.suffix in SOURCE_SUFFIXES and p.is_file() and not has_header(p)]


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    paths = [Path(a) for a in args] if args else tracked_files(Path.cwd())
    missing = missing_headers(paths)
    for p in missing:
        print(f"missing SPDX header: {p}", file=sys.stderr)
    if missing:
        print(f"{len(missing)} file(s) lack '{HEADER}'", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
