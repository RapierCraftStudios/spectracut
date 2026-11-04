# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``spectracut`` command-line entry point."""

from __future__ import annotations

import typer

from spectracut.cli.auth import auth_app
from spectracut.cli.bench import bench_app

app = typer.Typer(no_args_is_help=True, help="SpectraCut: an agent-run CGI studio.")
app.add_typer(auth_app, name="auth")
app.add_typer(bench_app, name="bench")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
