# SPDX-License-Identifier: AGPL-3.0-or-later
"""The setup manifest (every component pinned) and the types a setup plan is made of.

``manifest.yaml`` lists each bundled component once and the tested *sets* of
pins; the first set is the baseline and each later one is the next tested set
``spectracut setup --upgrade`` moves to. Plans are data (:class:`Step`) run
through an injectable :class:`StepRunner`, so they are testable without hosts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import yaml
from pydantic import model_validator

from spectracut.spec._base import StrictModel

Role = Literal["control", "host", "storage"]
Os = Literal["linux", "windows"]
Shell = Literal["sh", "powershell"]
# What a step does: sets its timeout and whether it is retried (copy, write, probe, task).
StepKind = Literal["copy", "write", "probe", "task", "install", "run"]

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")


class Component(StrictModel):
    description: str
    roles: tuple[Role, ...]
    os: tuple[Os, ...]
    version_cmd: dict[Os, str]
    install: dict[Os, str]


class TestedSet(StrictModel):
    __test__ = False  # not a pytest class

    id: str
    versions: dict[str, str]


class Manifest(StrictModel):
    components: dict[str, Component]
    sets: tuple[TestedSet, ...]

    @model_validator(mode="after")
    def _every_component_pinned(self) -> Manifest:
        if not self.sets:
            raise ValueError("manifest needs at least one tested set")
        ids = [s.id for s in self.sets]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate tested set id")
        for s in self.sets:
            if set(s.versions) != set(self.components):
                diff = sorted(set(s.versions) ^ set(self.components))
                raise ValueError(f"set {s.id} must pin exactly the components: {diff}")
        for name, comp in self.components.items():
            if set(comp.install) != set(comp.os) or set(comp.version_cmd) != set(comp.os):
                raise ValueError(f"{name}: install and version_cmd must cover exactly its os list")
        return self

    def tested_set(self, set_id: str) -> TestedSet:
        for s in self.sets:
            if s.id == set_id:
                return s
        raise KeyError(set_id)

    def next_tested_set(self, current: str | None) -> TestedSet | None:
        """The set after ``current`` (the baseline when there is none); ``None`` at the end."""
        if current is None:
            return self.sets[0]
        ids = [s.id for s in self.sets]
        if current not in ids:
            return self.sets[0]
        i = ids.index(current) + 1
        return self.sets[i] if i < len(self.sets) else None


def load_manifest(path: Path | None = None) -> Manifest:
    raw = yaml.safe_load((path or MANIFEST_PATH).read_text(encoding="utf-8"))
    return Manifest.model_validate(raw)


@dataclass(frozen=True)
class HostFacts:
    """What a probe learned about one machine."""

    name: str
    os: Os
    address: str
    wired: bool
    free_gb: float
    # Windows: whether the SSH account is an Administrator, so it can register scheduled
    # tasks (read from its token, not CIM); its %USERPROFILE%; whether the worker task exists.
    admin: bool = True
    home: str = ""
    # Windows: the SSH account's ``DOMAIN\\name`` and SID, so setup fills the one-time task
    # script itself and can print the SHA-256 the elevated command checks (#190).
    account: str = ""
    sid: str = ""
    task: bool = True
    # Windows: drive letters in use as ``letter=root`` (UNC of a network drive, folder of
    # a subst drive, empty for a local volume), read via Get-PSDrive and subst, not CIM.
    drives: tuple[str, ...] = ()
    # Control plane: Manager files missing on disk (``manager-unit``, ``manager-config``,
    # ``job-types``), so setup rewrites them whatever its state records. Empty: all there.
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class Step:
    """One idempotent action on one host. ``stdin`` carries secrets, never the command line."""

    host: str
    id: str
    summary: str
    command: str = ""
    shell: Shell = "sh"
    stdin: str | None = field(default=None, repr=False)
    component: str | None = None
    version: str | None = None
    artifact: str | None = None  # file under <home>/profiles that receives the step's stdout
    kind: StepKind = "run"


class StepRunner(Protocol):
    def probe(self, host: str) -> HostFacts: ...

    def run(self, step: Step) -> str | None: ...
