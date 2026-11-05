# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut serve``: the local API and review page, on localhost only.

No accounts and no tokens: the page is for the person at this machine.
Productions are started and steered from the CLI and the MCP server.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Annotated

import typer

from spectracut.studio.store import ProductionStore

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7420


def is_loopback(host: str) -> bool:
    """True for ``localhost`` and loopback IP literals (``127.0.0.0/8``, ``::1``)."""
    name = host.strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def serve_cmd(
    host: Annotated[
        str, typer.Option("--host", help="Loopback address to bind (localhost only).")
    ] = DEFAULT_HOST,
    port: Annotated[int, typer.Option("--port", min=0, max=65535, help="Port.")] = DEFAULT_PORT,
    home: Annotated[
        Path | None,
        typer.Option("--home", help="SpectraCut home (default $SPECTRACUT_HOME or XDG data dir)."),
    ] = None,
) -> None:
    """Serve the local review page and API on localhost (no account needed)."""
    if not is_loopback(host):
        typer.echo(
            f"error: serve binds to localhost only; {host!r} is not a loopback address",
            err=True,
        )
        raise typer.Exit(2)
    import uvicorn

    from spectracut.api.app import create_app

    app = create_app(productions=ProductionStore(home))
    shown = f"[{host.strip('[]')}]" if ":" in host else host
    typer.echo(f"review page: http://{shown}:{port}/")
    uvicorn.run(app, host=host.strip("[]"), port=port, log_level="warning")
