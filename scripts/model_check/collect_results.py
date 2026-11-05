# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fetch ``results.jsonl`` from legion and print it as a markdown table.

Usage (dev box): ``python scripts/model_check/collect_results.py [outdir] [--job JOB]``.
legion is the only model host (owner decision on #49).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from common import read_results, results_table

HOSTS = {"legion": "C:/spectracut/models/out/results.jsonl"}


def main() -> None:
    args = sys.argv[1:]
    job = args[args.index("--job") + 1] if "--job" in args else None
    out = Path(args[0]) if args and not args[0].startswith("--") else Path(".")
    rows = []
    for host, remote in HOSTS.items():
        dst = out / f"{host}.jsonl"
        subprocess.run(["scp", "-q", f"{host}:{remote}", str(dst)], check=False)
        if dst.exists():
            rows += read_results(dst.read_text(encoding="utf-8-sig").splitlines())
    print(results_table(r for r in rows if job is None or r.get("job") == job))


if __name__ == "__main__":
    main()
