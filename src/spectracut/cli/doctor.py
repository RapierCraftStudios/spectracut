# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut doctor``: which optional integrations are on, and are they healthy.

Each integration is off unless its ``[integrations.<name>]`` table in the local
config (``providers.config.config_path()``) has ``enabled = true``:

* ``tokenbaton``: ``tokenbaton --pick`` must exit 0.
* ``computeshare``: ``GET <router_url>/sandboxes`` must answer 2xx.
* ``forgedock``: ``forgedock`` and ``gh`` on ``PATH`` and ``gh auth status`` exits 0.

Enabled with a passing probe is ``healthy``; enabled with a failing probe is
``on`` with the reason. Integrations are optional, so ``doctor`` exits 0 even
when one is unhealthy. Every line goes through :func:`providers.base.redact`.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Annotated, Any, Literal

import typer

from spectracut.providers.base import redact
from spectracut.providers.config import config_path

INTEGRATIONS = ("tokenbaton", "computeshare", "forgedock")
PROBE_TIMEOUT_S = 3.0
NO_MANIFEST = "setup manifest: not installed (see spectracut setup)"

State = Literal["off", "on", "healthy"]
ProbeResult = tuple[bool, str]
"""``(ok, detail)``; ``detail`` explains a failure."""
Probe = Callable[[Mapping[str, Any]], ProbeResult]
"""Health check for one integration, given its config table."""


@dataclass(frozen=True)
class IntegrationReport:
    name: str
    state: State
    detail: str = ""

    def line(self) -> str:
        if self.state == "on":
            return redact(f"{self.name}: on (unhealthy: {self.detail or 'unknown'})")
        return redact(f"{self.name}: {self.state}")


def _run(argv: list[str]) -> ProbeResult:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, check=False
        )
    except FileNotFoundError:
        return False, f"{argv[0]} not found on PATH"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{' '.join(argv)}: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        reason = tail[-1] if tail else ""
        return False, f"{' '.join(argv)} exited {proc.returncode}" + (
            f": {reason}" if reason else ""
        )
    return True, ""


def probe_tokenbaton(cfg: Mapping[str, Any]) -> ProbeResult:
    return _run(["tokenbaton", "--pick"])


def probe_computeshare(cfg: Mapping[str, Any]) -> ProbeResult:
    router = str(cfg.get("router_url") or "").rstrip("/")
    if not router:
        return False, "router_url is not set"
    if not router.startswith(("http://", "https://")):
        return False, "router_url must be http(s)"
    try:
        with urllib.request.urlopen(f"{router}/sandboxes", timeout=PROBE_TIMEOUT_S) as resp:  # noqa: S310
            code = int(resp.status)
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"GET {router}/sandboxes: {getattr(exc, 'reason', exc)}"
    if 200 <= code < 300:
        return True, ""
    return False, f"GET {router}/sandboxes returned {code}"


def probe_forgedock(cfg: Mapping[str, Any]) -> ProbeResult:
    missing = [b for b in ("forgedock", "gh") if shutil.which(b) is None]
    if missing:
        return False, f"not on PATH: {', '.join(missing)}"
    return _run(["gh", "auth", "status"])


DEFAULT_PROBES: dict[str, Probe] = {
    "tokenbaton": probe_tokenbaton,
    "computeshare": probe_computeshare,
    "forgedock": probe_forgedock,
}


def load_config() -> dict[str, Any]:
    """The whole local config file, or ``{}`` if there is none."""
    path = config_path()
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc


def integration_reports(
    config: Mapping[str, Any], probes: Mapping[str, Probe] | None = None
) -> list[IntegrationReport]:
    """One report per known integration, probing only the enabled ones."""
    probes = {**DEFAULT_PROBES, **(probes or {})}
    tables = config.get("integrations", {})
    if not isinstance(tables, Mapping):
        tables = {}
    reports = []
    for name in INTEGRATIONS:
        table = tables.get(name)
        if not isinstance(table, Mapping) or table.get("enabled") is not True:
            reports.append(IntegrationReport(name, "off"))
            continue
        try:
            ok, detail = probes[name](table)
        except Exception as exc:  # a probe must never crash doctor
            ok, detail = False, f"probe error: {exc}"
        state: State = "healthy" if ok else "on"
        reports.append(IntegrationReport(name, state, "" if ok else redact(detail)))
    return reports


def component_lines() -> list[str]:
    """Bundled components from the setup manifest (#80); a seam until it exists."""
    if importlib.util.find_spec("spectracut.setup") is None:
        return [NO_MANIFEST]
    from spectracut import setup

    report = getattr(setup, "component_lines", None)
    return [redact(line) for line in report()] if callable(report) else [NO_MANIFEST]


def doctor(
    as_json: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")] = False,
) -> None:
    """Report optional integrations (off/on/healthy) and bundled components."""
    try:
        config = load_config()
    except ValueError as exc:
        typer.echo(redact(f"error: {exc}"), err=True)
        raise typer.Exit(2) from exc
    reports = integration_reports(config)
    components = component_lines()
    if as_json:
        payload = {
            "integrations": [{**asdict(r), "detail": redact(r.detail)} for r in reports],
            "components": components,
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    for r in reports:
        typer.echo(r.line())
    typer.echo("components:")
    for line in components:
        typer.echo(f"  {line}")


__all__ = ["DEFAULT_PROBES", "IntegrationReport", "doctor", "integration_reports"]
