# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lookdev kits: authored, parameterised assets expanded into harness build calls.

A kit (``assets/kits/<id>/kit.yaml``) declares pinned files (an HDRI) and named
entries: *assets* (glass tiers, a beam, a readout slab...) and *lighting* presets.
Each entry is a template of existing harness build-tool calls with ``${param}``
placeholders, typed params with defaults and ranges, and a provenance record.

The scene builder emits ``{"kit": "<entry>", "args": {...}}`` next to plain
``{"tool", "args"}`` calls; :meth:`Kit.expand_calls` turns each kit entry into its
build calls before :func:`~spectracut.studio.scene_builder.compile_calls`, so no
new harness tool or runtime op exists and every expanded call is validated by the
same typed tool models.

Placeholders: a string that is exactly ``${p}`` takes ``p``'s typed value; a
string with ``${p}`` inside takes ``str(value)``; ``${p|linear}`` turns a colour
param (``#rrggbb``) into a scene-linear RGB list; ``${file:<rel>}`` and
``${sha256:<rel>}`` are a pinned kit file's assets-dir path and sha256.

Placement: every entry takes the reserved params ``location`` (``[x, y, z]``) and
``scale`` (uniform). Expanded geometric arguments (``location``, ``points``,
``target``, ``offset``, ``dimensions``, ``bevel_width``, ``bevel_depth``,
``size``, ``extrude``, ``bevel``, ``distance``) are scaled about the entry origin
and ``location``/``points``/``target`` are then translated by ``location``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

import yaml
from pydantic import Field, JsonValue, ValidationError, model_validator

from spectracut.finishing.color import hex_to_linear
from spectracut.harness.errors import HarnessError
from spectracut.harness.tools import build_tools, get_tool
from spectracut.spec._base import NonEmptyStr, Slug, StrictModel

KITS_DIR = Path(__file__).resolve().parents[3] / "assets" / "kits"
"""Repo-root kits directory (a source checkout; kits are not packaged in the wheel)."""

STAGE_PREFIX = "kits"
"""Kit files stage at ``kits/<kit id>/<relpath>`` inside a render's assets dir."""

RESERVED = frozenset({"location", "scale"})
_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_SCALED = ("dimensions", "bevel_width", "bevel_depth", "size", "extrude", "bevel", "distance")
_SCALED_VECTORS = ("location", "target", "offset")
_TRANSLATED = ("location", "target")


class Provenance(StrictModel):
    """Where a kit entry or file comes from: original work or CC0, with author and source."""

    origin: Literal["original", "CC0"]
    author: NonEmptyStr
    source: NonEmptyStr


class KitParam(StrictModel):
    """One declared parameter of a kit entry."""

    type: Literal["number", "integer", "string", "color", "vec3"]
    default: JsonValue
    min: float | None = None
    max: float | None = None
    max_length: int = Field(default=63, ge=1, le=500)
    choices: tuple[str, ...] | None = None
    description: str = ""

    def coerce(self, value: Any, where: str) -> JsonValue:
        """``value`` checked against the declared type and range; :class:`ValueError` names it."""
        if self.type in ("number", "integer"):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"{where} must be a {self.type}")
            if self.type == "integer" and not float(value).is_integer():
                raise ValueError(f"{where} must be an integer")
            if self.min is not None and value < self.min:
                raise ValueError(f"{where} must be >= {self.min}")
            if self.max is not None and value > self.max:
                raise ValueError(f"{where} must be <= {self.max}")
            return int(value) if self.type == "integer" else float(value)
        if self.type == "string":
            if not isinstance(value, str) or not value:
                raise ValueError(f"{where} must be a non-empty string")
            if len(value) > self.max_length:
                raise ValueError(f"{where} must be at most {self.max_length} characters")
            if self.choices is not None and value not in self.choices:
                raise ValueError(f"{where} must be one of {list(self.choices)}")
            return value
        if self.type == "color":
            if not isinstance(value, str) or not _HEX.match(value):
                raise ValueError(f"{where} must be a '#rrggbb' palette swatch")
            return value
        if (
            not isinstance(value, list | tuple)
            or len(value) != 3
            or not all(isinstance(v, int | float) and not isinstance(v, bool) for v in value)
        ):
            raise ValueError(f"{where} must be [x, y, z]")
        return [float(v) for v in value]


_RESERVED_PARAMS = {
    "location": KitParam(type="vec3", default=[0.0, 0.0, 0.0], description="entry origin"),
    "scale": KitParam(type="number", default=1.0, min=0.01, max=100, description="uniform"),
}


class KitCall(StrictModel):
    tool: NonEmptyStr
    args: dict[str, JsonValue] = Field(default_factory=dict)


class KitEntry(StrictModel):
    """A kit asset or lighting preset: a template of build calls with typed params."""

    kind: Literal["asset", "lighting"] = "asset"
    summary: NonEmptyStr
    params: dict[str, KitParam] = Field(default_factory=dict)
    calls: Annotated[list[KitCall], Field(min_length=1)]
    provenance: Provenance


class KitFile(StrictModel):
    """A sha256-pinned file shipped with the kit and its licence sidecar."""

    path: NonEmptyStr
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    license: NonEmptyStr
    provenance: Provenance

    @model_validator(mode="after")
    def _relative(self) -> KitFile:
        for rel in (self.path, self.license):
            parts = PurePosixPath(rel).parts
            if PurePosixPath(rel).is_absolute() or ".." in parts:
                raise ValueError(f"{rel!r} must be relative to the kit dir")
        return self


class Kit(StrictModel):
    """A lookdev kit: pinned files plus named assets and lighting presets."""

    id: Slug
    title: NonEmptyStr
    summary: str = ""
    files: tuple[KitFile, ...] = ()
    entries: dict[str, KitEntry]
    root: str = Field(default="", exclude=True)
    """The kit dir on disk (set by :func:`load_kit`; not serialised)."""

    def staged_path(self, rel: str) -> str:
        """``rel`` (a kit file) as a relpath inside the render's assets dir."""
        return f"{STAGE_PREFIX}/{self.id}/{rel}"

    def file(self, rel: str) -> KitFile:
        for f in self.files:
            if f.path == rel:
                return f
        raise KeyError(rel)

    def files_to_stage(self) -> dict[str, Path]:
        """Assets-dir relpath -> source file for every pinned file and its licence sidecar."""
        root = Path(self.root)
        out: dict[str, Path] = {}
        for f in self.files:
            out[self.staged_path(f.path)] = root / f.path
            out[self.staged_path(f.license)] = root / f.license
        return out

    def prompt_summary(self) -> dict[str, JsonValue]:
        """Entry name -> kind, summary and params (type, default, range) for the builder."""
        summary: dict[str, JsonValue] = {}
        for name, entry in self.entries.items():
            params: dict[str, JsonValue] = {}
            for pname, p in {**entry.params, **_RESERVED_PARAMS}.items():
                spec: dict[str, JsonValue] = {"type": p.type, "default": p.default}
                if p.min is not None:
                    spec["min"] = p.min
                if p.max is not None:
                    spec["max"] = p.max
                if p.choices is not None:
                    spec["choices"] = list(p.choices)
                if p.description:
                    spec["description"] = p.description
                params[pname] = spec
            summary[name] = {"kind": entry.kind, "summary": entry.summary, "params": params}
        return summary

    def expand(self, name: str, args: Mapping[str, Any]) -> list[dict[str, JsonValue]]:
        """One kit entry's build calls with ``args`` over the declared defaults.

        Raises :class:`HarnessError` (``tool.unknown`` for an unknown entry,
        ``args.invalid`` for an undeclared, mistyped or out-of-range arg).
        """
        entry = self.entries.get(name)
        if entry is None:
            raise HarnessError(
                "tool.unknown", f"no kit entry {name!r} (have: {', '.join(self.entries)})"
            )
        declared = {**entry.params, **_RESERVED_PARAMS}
        unknown = sorted(set(args) - set(declared))
        if unknown:
            raise HarnessError(
                "args.invalid",
                f"undeclared args {unknown}; {name} takes {sorted(declared)}",
            )
        values: dict[str, JsonValue] = {}
        try:
            for pname, p in declared.items():
                values[pname] = p.coerce(args.get(pname, p.default), pname)
        except ValueError as exc:
            raise HarnessError("args.invalid", str(exc)) from None
        location = values["location"]
        scale = values["scale"]
        assert isinstance(location, list) and isinstance(scale, float)
        calls: list[dict[str, JsonValue]] = []
        for call in entry.calls:
            expanded = {k: self._substitute(v, values) for k, v in call.args.items()}
            calls.append({"tool": call.tool, "args": _place(expanded, location, scale)})
        return calls

    def expand_calls(self, calls: Sequence[JsonValue]) -> list[JsonValue]:
        """Expand every ``{"kit": name, "args"}`` item; ``{tool, args}`` items pass through.

        Every problem is collected first, then one :class:`HarnessError` lists them as
        ``details.problems = [{index, tool: "kit:<name>", message}]`` against the
        original call index.
        """
        out: list[JsonValue] = []
        problems: list[dict[str, JsonValue]] = []
        for i, call in enumerate(calls):
            if not isinstance(call, Mapping) or "kit" not in call:
                out.append(call)
                continue
            label: str | None = None
            try:
                if set(call) - {"kit", "args"}:
                    raise HarnessError("args.invalid", "a kit entry is exactly {kit, args}")
                name, args = call.get("kit"), call.get("args") or {}
                if not isinstance(name, str):
                    raise HarnessError("args.invalid", "kit must be a string")
                label = f"kit:{name}"
                if not isinstance(args, Mapping):
                    raise HarnessError("args.invalid", "args must be an object")
                out.extend(self.expand(name, args))
            except HarnessError as exc:
                problems.append({"index": i, "tool": label, "message": exc.message})
        if problems:
            lines = [f"calls[{p['index']}] {p['tool'] or 'kit'}: {p['message']}" for p in problems]
            raise HarnessError("args.invalid", "; ".join(lines), problems=problems)
        return out

    def _substitute(self, value: JsonValue, values: Mapping[str, JsonValue]) -> JsonValue:
        if isinstance(value, list):
            return [self._substitute(v, values) for v in value]
        if isinstance(value, dict):
            return {k: self._substitute(v, values) for k, v in value.items()}
        if not isinstance(value, str):
            return value
        whole = _PLACEHOLDER.fullmatch(value)
        if whole:
            return self._resolve(whole.group(1), values)
        return _PLACEHOLDER.sub(lambda m: str(self._resolve(m.group(1), values)), value)

    def _resolve(self, token: str, values: Mapping[str, JsonValue]) -> JsonValue:
        if token.startswith("file:"):
            return self.staged_path(self.file(token[5:]).path)
        if token.startswith("sha256:"):
            return self.file(token[7:]).sha256
        name, _, flt = token.partition("|")
        value = values[name]
        if flt == "linear":
            return list(hex_to_linear(str(value)))
        return value


def _place(args: dict[str, JsonValue], location: list[Any], scale: float) -> dict[str, JsonValue]:
    """Scale geometric args about the entry origin, then translate positions by ``location``."""
    out = dict(args)

    def vec(v: JsonValue, translate: bool) -> JsonValue:
        if not (isinstance(v, list) and len(v) == 3 and all(_num(c) for c in v)):
            return v
        return [
            round(float(c) * scale + (float(o) if translate else 0.0), 6)  # type: ignore[arg-type]
            for c, o in zip(v, location, strict=True)
        ]

    for key in _SCALED:
        v = out.get(key)
        if _num(v):
            out[key] = round(float(v) * scale, 6)  # type: ignore[arg-type]
        elif key == "dimensions" and key in out:
            out[key] = vec(v, False)
    for key in _SCALED_VECTORS:
        if key in out:
            out[key] = vec(out[key], key in _TRANSLATED)
    points = out.get("points")
    if isinstance(points, list):
        out["points"] = [vec(p, True) for p in points]
    return out


def _num(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _placeholders(value: JsonValue) -> list[str]:
    if isinstance(value, list):
        return [t for v in value for t in _placeholders(v)]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _placeholders(v)]
    return _PLACEHOLDER.findall(value) if isinstance(value, str) else []


def _check(kit: Kit) -> list[str]:
    """Every problem with a parsed kit: tools, placeholders, files, default expansion."""
    problems: list[str] = []
    root = Path(kit.root)
    files = {f.path for f in kit.files}
    for f in kit.files:
        path, sidecar = root / f.path, root / f.license
        if not path.is_file():
            problems.append(f"file {f.path}: missing")
            continue
        if _sha256(path) != f.sha256:
            problems.append(f"file {f.path}: sha256 {_sha256(path)} != pinned {f.sha256}")
        if not sidecar.is_file():
            problems.append(f"file {f.path}: licence sidecar {f.license} missing")
            continue
        try:
            record = json.loads(sidecar.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f"file {f.path}: licence sidecar is not JSON ({exc})")
            continue
        if not isinstance(record, dict) or record.get("sha256") != f.sha256:
            problems.append(f"file {f.path}: licence sidecar sha256 does not match the pin")
        if not isinstance(record, dict) or not isinstance(record.get("license"), dict):
            problems.append(f"file {f.path}: licence sidecar has no license record")
    tools = {t.name for t in build_tools()}
    for name, entry in kit.entries.items():
        clash = RESERVED & set(entry.params)
        if clash:
            problems.append(f"entry {name}: params {sorted(clash)} are reserved")
        declared = set(entry.params) | RESERVED
        for i, call in enumerate(entry.calls):
            where = f"entry {name} calls[{i}] {call.tool}"
            if call.tool not in tools:
                problems.append(f"{where}: unknown tool (only harness build tools)")
            for token in _placeholders(call.args):
                if token.startswith(("file:", "sha256:")):
                    rel = token.split(":", 1)[1]
                    if rel not in files:
                        problems.append(f"{where}: ${{{token}}} is not a pinned kit file")
                    continue
                pname, _, flt = token.partition("|")
                if pname not in declared:
                    problems.append(f"{where}: ${{{token}}} is not a declared param")
                elif flt and (flt != "linear" or entry.params[pname].type != "color"):
                    problems.append(f"{where}: ${{{token}}}: only color params take |linear")
        for pname, p in entry.params.items():
            try:
                p.coerce(p.default, f"default of {pname}")
            except ValueError as exc:
                problems.append(f"entry {name}: {exc}")
    if problems:
        return problems
    for name in kit.entries:
        try:
            for i, expanded in enumerate(kit.expand(name, {})):
                tool_name, args = str(expanded["tool"]), expanded["args"]
                try:
                    get_tool(tool_name).parse(args if isinstance(args, Mapping) else None)
                except HarnessError as exc:
                    message = exc.message.removeprefix(f"{tool_name}: ")
                    problems.append(f"entry {name} calls[{i}] {tool_name}: {message}")
        except (HarnessError, KeyError) as exc:
            problems.append(f"entry {name}: {exc}")
    return problems


def load_kit(path: Path | str) -> Kit:
    """Read a ``kit.yaml`` (or a kit dir holding one) and validate it.

    Refuses (``config.invalid``) an entry or file without provenance, a template
    tool that is not a harness build tool, a placeholder that is not a declared
    param or pinned file, a missing or mis-pinned file or licence sidecar, and an
    entry whose default expansion fails its tool's argument schema.
    """
    path = Path(path)
    if path.is_dir():
        path = path / "kit.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise HarnessError("config.invalid", f"kit {path}: {exc}") from None
    if not isinstance(raw, dict):
        raise HarnessError("config.invalid", f"kit {path}: expected a mapping")
    return _validated({**raw, "root": str(path.parent)}, str(path))


def _validated(raw: Mapping[str, Any], label: str) -> Kit:
    try:
        kit = Kit.model_validate(raw)
    except ValidationError as exc:
        lines = [
            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()
        ]
        raise HarnessError("config.invalid", f"kit {label}: " + "; ".join(lines)) from None
    problems = _check(kit)
    if problems:
        raise HarnessError("config.invalid", f"kit {label}: " + "; ".join(problems))
    return kit


def kit_from(value: object) -> Kit | None:
    """A :class:`Kit` from a kit, a kit.yaml path, a kit JSON object (``root`` set) or ``None``."""
    if value is None or isinstance(value, Kit):
        return value
    if isinstance(value, str | Path):
        return load_kit(value)
    if isinstance(value, Mapping):
        return _validated(value, str(value.get("id", "(object)")))
    raise HarnessError("config.invalid", f"kit must be a kit.yaml path, got {type(value).__name__}")


__all__ = [
    "KITS_DIR",
    "STAGE_PREFIX",
    "Kit",
    "KitCall",
    "KitEntry",
    "KitFile",
    "KitParam",
    "Provenance",
    "kit_from",
    "load_kit",
]
