# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measure a model repo's own CLI (peak VRAM of the whole card, wall seconds) as one results row.

Usage: ``run.ps1 tts run_cli.py <job> <model> <settings> <cwd> -- <exe> [args ...]``

Used for HeartMuLa (music) and HunyuanVideo-Foley (foley), whose inference entry
points are repo scripts with their own venvs. stdout/stderr tails go into ``extra``.
"""

from __future__ import annotations

import subprocess
import sys

from common import measure


def split_args(argv: list[str]) -> tuple[list[str], list[str]]:
    """``[job, model, settings, cwd, "--", exe, ...]`` -> (head, command)."""
    if "--" not in argv:
        raise SystemExit("usage: run_cli.py <job> <model> <settings> <cwd> -- <exe> [args ...]")
    i = argv.index("--")
    head, cmd = argv[:i], argv[i + 1 :]
    if len(head) != 4 or not cmd:
        raise SystemExit("usage: run_cli.py <job> <model> <settings> <cwd> -- <exe> [args ...]")
    return head, cmd


def main() -> None:
    (job, model, settings, cwd), cmd = split_args(sys.argv[1:])
    with measure(job, model, settings) as m:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace")
        m["exit"] = r.returncode
        m["stdout_tail"] = r.stdout[-600:]
        m["stderr_tail"] = r.stderr[-600:]
        if r.returncode != 0:
            raise RuntimeError(f"exit {r.returncode}: {r.stderr[-300:]}")


if __name__ == "__main__":
    main()
