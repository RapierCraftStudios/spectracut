# SPDX-License-Identifier: AGPL-3.0-or-later
"""On-disk production store, so separate CLI calls share one production graph.

Layout under the SpectraCut home (``$SPECTRACUT_HOME``, else
``$XDG_DATA_HOME/spectracut``, else ``~/.local/share/spectracut``)::

    productions/<id>/production.json   metadata (profile, brief hash, created_at)
    productions/<id>/brief.json        the brief the production was made from
    productions/<id>/graph.json        GraphSnapshot

Every file is written atomically (temp file plus ``os.replace``). Nothing
secret is ever written here.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, ValidationError

from spectracut.spec._base import ContentHash, Slug, StrictModel, canonical_json, content_hash
from spectracut.spec.brief import Brief, CreativeBrief
from spectracut.studio.graph import GraphSnapshot, ProductionGraph

HOME_ENV = "SPECTRACUT_HOME"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def spectracut_home() -> Path:
    """The directory SpectraCut keeps local state in."""
    explicit = os.environ.get(HOME_ENV)
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "spectracut"


def write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a temp file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class ProductionError(Exception):
    """A production is missing, already exists, or its files are unreadable."""


class ProductionMeta(StrictModel):
    id: Slug
    profile: Slug | None = None
    brief_kind: Literal["brief", "creative_brief"]
    brief_hash: ContentHash
    created_at: AwareDatetime


class ProductionStore:
    """Productions under ``<home>/productions``."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(home) if home is not None else spectracut_home()
        self.root = self.home / "productions"

    def path(self, production_id: str) -> Path:
        if not _SLUG.match(production_id) or len(production_id) > 128:
            raise ProductionError(f"production id must be a slug, got {production_id!r}")
        return self.root / production_id

    def exists(self, production_id: str) -> bool:
        return (self.path(production_id) / "graph.json").is_file()

    def create(
        self,
        production_id: str,
        brief: Brief | CreativeBrief,
        graph: ProductionGraph,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> Path:
        """Store a new production; refuses to replace one unless ``force``."""
        path = self.path(production_id)
        if graph.production_id != production_id:
            raise ProductionError(
                f"graph is for production {graph.production_id!r}, not {production_id!r}"
            )
        if self.exists(production_id) and not force:
            raise ProductionError(
                f"production {production_id!r} already exists at {path} (use --force)"
            )
        meta = ProductionMeta(
            id=production_id,
            profile=graph.profile.name if graph.profile is not None else None,
            brief_kind="creative_brief" if isinstance(brief, CreativeBrief) else "brief",
            brief_hash=content_hash(brief),
            created_at=now or datetime.now(UTC),
        )
        write_atomic(path / "brief.json", canonical_json(brief))
        write_atomic(path / "production.json", canonical_json(meta))
        self.save(production_id, graph)
        return path

    def save(self, production_id: str, graph: ProductionGraph) -> None:
        write_atomic(self.path(production_id) / "graph.json", canonical_json(graph.snapshot()))

    def meta(self, production_id: str) -> ProductionMeta:
        return self._read(production_id, "production.json", ProductionMeta)

    def load(self, production_id: str) -> tuple[ProductionGraph, ProductionMeta]:
        meta = self.meta(production_id)
        snap = self._read(production_id, "graph.json", GraphSnapshot)
        try:
            graph = ProductionGraph.from_snapshot(snap)
        except ValueError as exc:
            raise ProductionError(f"production {production_id!r}: {exc}") from exc
        return graph, meta

    def brief(self, production_id: str) -> Brief | CreativeBrief:
        meta = self.meta(production_id)
        if meta.brief_kind == "creative_brief":
            return self._read(production_id, "brief.json", CreativeBrief)
        return self._read(production_id, "brief.json", Brief)

    def list(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            p.name
            for p in self.root.iterdir()
            if _SLUG.match(p.name) and (p / "graph.json").is_file()
        )

    def _read[M: StrictModel](self, production_id: str, name: str, model: type[M]) -> M:
        path = self.path(production_id) / name
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise ProductionError(f"no production {production_id!r} in {self.root}") from None
        except OSError as exc:
            raise ProductionError(f"cannot read {path}: {exc}") from exc
        try:
            return model.model_validate_json(data)
        except ValidationError as exc:
            raise ProductionError(f"{path} is invalid: {exc.error_count()} error(s)") from exc
