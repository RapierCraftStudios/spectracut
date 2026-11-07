# SPDX-License-Identifier: AGPL-3.0-or-later
"""Product source: where the product lives, how to run it, and its front-end routes.

A ``product.yaml`` (for example ``brands/alterlab/product.yaml``) is loaded by
:func:`load_product_source` into a :class:`ProductSource`: the repo, the front-end
directory inside it, the framework, how to run it (a command and/or a URL), the
seed command for demo data, the demo login as **names of environment
variables** (never their values) with its sign-in route and selectors, and the
demo-data allow-list (:class:`DemoData`) captures are checked against. Every
model is closed, so a literal
``password:`` key is rejected rather than stored.

:func:`scan_nextjs_routes` reads a Next.js front end (App Router ``app/`` or
``src/app/`` and Pages Router ``pages/`` or ``src/pages/``) into a sorted list of
:class:`FrontendRoute` (path, page file, dynamic flag, imported components).
:func:`frontend_pages` turns the routes into ``{url, text}`` excerpts for the
product-understanding node, so the department itself never reads files.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, StringConstraints, ValidationError, model_validator

from spectracut.spec._base import NonEmptyStr, Slug, StrictModel

EnvVarName = Annotated[str, StringConstraints(pattern=r"^[A-Z_][A-Z0-9_]*$", max_length=128)]
"""Name of an environment variable (the value is resolved at capture time)."""

PAGE_SUFFIXES = (".tsx", ".ts", ".jsx", ".js", ".mdx")
_PAGES_SKIP = {"_app", "_document", "_error"}
_IMPORT = re.compile(r"""^\s*import\s[^'"]*?from\s+['"]([^'"]+)['"]""", re.MULTILINE)


class DemoLogin(StrictModel):
    """The demo account's credentials, as environment variable names only."""

    username_env: EnvVarName
    password_env: EnvVarName
    route: str = "/login"
    """Sign-in page of the front end."""
    username_selector: str = "input[type=email]"
    password_selector: str = "input[type=password]"
    submit_selector: str = "button[type=submit]"


class DemoData(StrictModel):
    """What demo data may appear in a capture: the allow-list the capture is checked against.

    Every email-shaped string a capture shows must be in ``emails`` (or be the demo
    login's email); every text under one of ``name_selectors`` must be in ``names``.
    """

    emails: list[str] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
    name_selectors: list[str] = Field(default_factory=list)
    """Selectors of elements that render a person's name (e.g. the account menu)."""


class ProductRun(StrictModel):
    """How to reach the running front end: a command that serves it and/or its URL."""

    command: str = ""
    url: str = ""
    max_runs: int | None = Field(default=None, gt=0)
    """The live demo's per-visitor run budget (e.g. 10); captures are cached and not repeated."""

    @model_validator(mode="after")
    def _one_way(self) -> ProductRun:
        if not self.command and not self.url:
            raise ValueError("run needs a command or a url")
        return self


class FrontendRoute(StrictModel):
    """One page of the front end: URL path, its page file and the components it imports."""

    path: NonEmptyStr
    """URL path, e.g. ``/dashboard/billing`` or ``/integrations/[platform]``."""
    file: NonEmptyStr
    """Page file, relative to the repo."""
    dynamic: bool = False
    """The path has a ``[param]`` or ``[...slug]`` segment."""
    components: list[str] = Field(default_factory=list)
    """Component modules the page imports (``@/components/...`` or relative)."""


class ProductSource(StrictModel):
    """Where the product lives and how to run and seed it. Holds no secrets."""

    id: Slug
    repo: Path
    """The product's repo (absolute once loaded)."""
    frontend_dir: Path = Path(".")
    """Front-end directory relative to ``repo`` (its ``app/``/``pages/`` are scanned)."""
    framework: Literal["nextjs"] = "nextjs"
    run: ProductRun
    seed_command: str = ""
    """Command that seeds the demo data (run from ``repo``)."""
    demo_login: DemoLogin | None = None
    demo_data: DemoData = DemoData()
    """Demo-data allow-list for captures."""
    routes: list[FrontendRoute] = Field(default_factory=list)
    """Route map override; empty means scan the front end."""
    dismiss: list[str] = Field(default_factory=list)
    """Selectors of a promo or banner's own close buttons, clicked in setup."""
    hide: list[str] = Field(default_factory=list)
    """Extra banner or promotional-nav selectors hidden in every capture."""

    @property
    def frontend(self) -> Path:
        return self.repo / self.frontend_dir


def load_product_source(path: Path) -> ProductSource:
    """Read a ``product.yaml``; ``~`` expands and a relative ``repo`` is from the file's dir.

    Raises :class:`ValueError` naming the file when it is unreadable, not a mapping
    or fails the schema.
    """
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read product source {path}: {exc.strerror or exc}") from None
    except yaml.YAMLError as exc:
        raise ValueError(f"product source {path} is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ValueError(f"product source {path} must be a mapping")
    if "repo" in raw:
        repo = Path(str(raw["repo"])).expanduser()
        raw["repo"] = str(repo if repo.is_absolute() else (path.parent / repo).resolve())
    try:
        return ProductSource.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"product source {path} is invalid: {exc}") from None


def _app_route(rel: Path) -> str | None:
    parts: list[str] = []
    for part in rel.parent.parts:
        if part.startswith(("_", "@")):
            return None  # private folder or parallel-route slot
        if part.startswith("(") and part.endswith(")"):
            continue  # route group
        parts.append(part)
    return "/" + "/".join(parts)


def _pages_route(rel: Path) -> str | None:
    parts = list(rel.with_suffix("").parts)
    if parts[0] == "api" or parts[-1] in _PAGES_SKIP or any(p.startswith("_") for p in parts):
        return None
    if parts[-1] == "index":
        parts = parts[:-1]
    return "/" + "/".join(parts)


def _components(page: Path) -> list[str]:
    try:
        text = page.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    found = [m for m in _IMPORT.findall(text) if "components" in m or m.startswith(".")]
    return sorted(set(found))


def scan_nextjs_routes(frontend: Path, repo: Path | None = None) -> list[FrontendRoute]:
    """The routes of the Next.js app at ``frontend``, sorted by path.

    ``file`` is relative to ``repo`` (default ``frontend``). Raises
    :class:`ValueError` if ``frontend`` is missing or has no ``app``/``pages`` dir.
    """
    frontend = Path(frontend)
    base = Path(repo) if repo is not None else frontend
    if not frontend.is_dir():
        raise ValueError(f"front-end directory {frontend} does not exist")
    routes: dict[str, FrontendRoute] = {}
    roots = 0
    for sub, router in (
        ("app", "app"),
        ("src/app", "app"),
        ("pages", "pages"),
        ("src/pages", "pages"),
    ):
        root = frontend / sub
        if not root.is_dir():
            continue
        roots += 1
        for page in sorted(root.rglob("*")):
            if page.suffix not in PAGE_SUFFIXES or not page.is_file():
                continue
            if "node_modules" in page.parts:
                continue
            rel = page.relative_to(root)
            if router == "app":
                if page.stem != "page":
                    continue
                route = _app_route(rel)
            else:
                route = _pages_route(rel)
            if route is None or route in routes:
                continue
            routes[route] = FrontendRoute(
                path=route,
                file=page.relative_to(base).as_posix(),
                dynamic="[" in route,
                components=_components(page),
            )
    if not roots:
        raise ValueError(f"front-end directory {frontend} has no app/ or pages/ directory")
    return [routes[k] for k in sorted(routes)]


def product_routes(source: ProductSource) -> list[FrontendRoute]:
    """The source's route override, else the scanned routes of its front end."""
    if source.routes:
        return list(source.routes)
    return scan_nextjs_routes(source.frontend, source.repo)


def frontend_pages(source: ProductSource, routes: Sequence[FrontendRoute]) -> list[dict[str, str]]:
    """``{url, text}`` excerpts of the front end, one per route, for product understanding."""
    pages: list[dict[str, str]] = []
    for route in routes:
        comps = ", ".join(route.components) or "none"
        pages.append(
            {
                "url": f"repo:{route.file}",
                "text": (
                    f"{source.id} front end ({source.framework}) route {route.path}"
                    f" (page {route.file}); components: {comps}"
                ),
            }
        )
    return pages
