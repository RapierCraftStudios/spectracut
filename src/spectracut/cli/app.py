# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``spectracut`` command-line entry point."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import typer

from spectracut.cli.auth import auth_app
from spectracut.cli.bench import bench_app
from spectracut.cli.doctor import doctor
from spectracut.cli.render import hosts_app, render, status
from spectracut.cli.studio import studio_app

app = typer.Typer(no_args_is_help=True, help="SpectraCut: an agent-run CGI studio.")
app.add_typer(auth_app, name="auth")
app.add_typer(bench_app, name="bench")
app.add_typer(studio_app, name="studio")
app.add_typer(hosts_app, name="hosts")
app.command("render")(render)
app.command("status")(status)
app.command("doctor")(doctor)


def _installed_version() -> str:
    try:
        return version("spectracut")
    except PackageNotFoundError:
        return "0.0.0"


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"spectracut {_installed_version()}")
        raise typer.Exit()


@app.callback()
def _root(
    show_version: bool = typer.Option(
        False,
        "--version",
        help="Print the installed spectracut version and exit.",
        callback=_print_version,
        is_eager=True,
    ),
) -> None:
    """SpectraCut: an agent-run CGI studio."""


def main() -> None:
    app()


if __name__ == "__main__":
    main()
