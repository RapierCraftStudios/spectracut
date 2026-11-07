# SPDX-License-Identifier: AGPL-3.0-or-later
"""UI capture: the product's real screens as layered stills and interaction clips.

The ``ui-capture`` department (node kind ``ui_capture``, the producer's
``product-ui`` asset) turns the screen map of the ``product`` node
(:class:`~spectracut.studio.departments.discovery.ProductNotes`) into captures:
one clip (a frame-locked PNG sequence), its last frame as a still, the still's
DOM layers, and provenance, for every non-interlude mapping on every device of
the node (laptop 16:10 and phone 19.5:9 by default).

The department class is declarative only: departments never touch the network
or run processes. :class:`CaptureRunner` does the work: it seeds the demo data,
starts the product's dev server (or uses its URL), logs in with unrecorded setup
actions that type the credentials from environment variables inside the capture
CLI, drives each flow through the ``@spectracut/capture`` CLI
(:class:`~spectracut.queue.executors.capture.CaptureExecutor`), fails the node when
a capture shows an email or name that is not on the product's demo allow-list,
and re-captures to prove the frames are deterministic.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar, Final, Literal

from pydantic import Field, JsonValue

from spectracut.queue.executors.capture import CaptureExecutor
from spectracut.queue.queue import Job
from spectracut.spec._base import NonEmptyStr, Slug, StrictModel
from spectracut.spec.product import DemoData, ProductSource
from spectracut.studio.department import (
    SKILLS_ROOT,
    CostEstimate,
    Department,
    DepartmentOutputError,
)
from spectracut.studio.departments.discovery import (
    PRODUCT,
    ProductNotes,
    ScreenMapping,
    ScreenStep,
)
from spectracut.studio.departments.screens import CaptureFrame, DeviceId, ScreenState, Sha256
from spectracut.studio.graph import RunContext, Runner, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import NodeSpec

__all__ = [
    "CAPTURE_DEPARTMENTS",
    "DEVICES",
    "UI_CAPTURE",
    "CaptureFrame",
    "CaptureProvenance",
    "CaptureRunner",
    "DeviceProfile",
    "ScreenCapture",
    "UICaptureDepartment",
    "UICaptures",
    "capture_key",
    "capture_node",
    "check_demo_data",
    "steps_to_actions",
]

UI_CAPTURE: Final = "ui_capture"
"""Node id and kind of the capture node (the producer's ``product-ui`` asset)."""

CAPTURE_CMD_ENV: Final = "SPECTRACUT_CAPTURE_CMD"
"""Overrides the capture CLI command (shlex-split)."""

RIG_PACKAGE: Final = "@spectracut/capture"
REPO_ROOT: Final = Path(__file__).resolve().parents[4]

MOVE_MS: Final = 600
TYPE_BPM: Final = 120
TYPE_SUBDIVISION: Final = 4
SCROLL_VELOCITY: Final = 1200.0
END_HOLD_MS: Final = 500
LOGIN_SETTLE_MS: Final = 1000
READY_TIMEOUT_S: Final = 120.0



class DeviceProfile(StrictModel):
    """A capture device: CSS viewport and pixel ratio."""

    id: DeviceId
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    device_scale_factor: float = Field(gt=0)


DEVICES: Final[Mapping[str, DeviceProfile]] = {
    "laptop": DeviceProfile(id="laptop", width=1512, height=982, device_scale_factor=2),
    "phone": DeviceProfile(id="phone", width=393, height=852, device_scale_factor=3),
}
"""Laptop at the MacBook Pro display aspect (3024x1964 px) and phone at the iPhone
aspect (1179x2556 px), the device kit's screens (#362)."""
COLOR_SCHEME: Final = "dark"
"""Captures run in the product's dark scheme (#362); cookie banners stay hidden by the
capture package's default clean rules."""


class CaptureProvenance(StrictModel):
    """Where a capture came from and how it was checked."""

    product_id: Slug
    repo_commit: str = ""
    """``git rev-parse HEAD`` of the product repo; empty when it is not a git repo."""
    url: NonEmptyStr
    steps: list[ScreenStep]
    device: DeviceId
    spec_sha256: Sha256
    """sha256 of the capture spec (without its output dir)."""
    frames_sha256: Sha256
    """sha256 over the ordered frame hashes."""
    rig: NonEmptyStr
    demo_check: Literal["passed"]
    deterministic: bool
    """A second capture gave the same frames (``False`` when it was not run)."""


class ScreenCapture(StrictModel):
    """One mapped flow on one device: clip, still, still layers and provenance."""

    key: NonEmptyStr
    """``proof:<claim>`` or ``beat:<beat>``."""
    proof_point: str = ""
    beat: str = ""
    route: NonEmptyStr
    url: NonEmptyStr
    device: DeviceId
    fps: int = Field(gt=0)
    clip: list[CaptureFrame] = Field(min_length=1)
    """The clip as a frame sequence, in order."""
    still: CaptureFrame
    """The last frame."""
    layers: list[CaptureFrame] = Field(default_factory=list)
    """DOM layers of the still (transparent PNGs, back to front)."""
    manifest: NonEmptyStr
    events: NonEmptyStr
    text: NonEmptyStr
    provenance: CaptureProvenance
    screen: ScreenState | None = Field(default=None, exclude_if=lambda v: v is None)
    """The capture composited into device chrome (absent on older outputs)."""


class UICaptures(StrictModel):
    """Every capture of the product for the production."""

    product_id: Slug
    devices: list[DeviceId] = Field(min_length=1)
    captures: list[ScreenCapture] = Field(default_factory=list)


def capture_key(mapping: ScreenMapping) -> str:
    """``proof:<claim>`` or ``beat:<beat>`` for a screen mapping."""
    return f"proof:{mapping.proof_point}" if mapping.proof_point else f"beat:{mapping.beat}"


def _devices(value: object) -> list[str]:
    if isinstance(value, list) and all(isinstance(d, str) for d in value):
        return [str(d) for d in value]
    return list(DEVICES)


class UICaptureDepartment(Department):
    """Captures the product's screens per the screen map (built by :class:`CaptureRunner`)."""

    name = "ui-capture"
    produces = frozenset({UI_CAPTURE})
    inputs: ClassVar[Mapping[str, type[ProductNotes]]] = {PRODUCT: ProductNotes}
    output = UICaptures
    skill_pack = SKILLS_ROOT / "motion-designer"
    cost = CostEstimate()

    @classmethod
    def check_output(
        cls,
        node_id: str,
        output: JsonValue,
        inputs: Mapping[str, JsonValue],
        kinds: Mapping[str, str],
    ) -> None:
        """Every non-interlude screen of the product input has a capture on every device."""
        caps = UICaptures.model_validate(output)
        have = {(c.key, c.device) for c in caps.captures}
        for input_id, value in inputs.items():
            if kinds.get(input_id, input_id) != PRODUCT:
                continue
            notes = ProductNotes.model_validate(value)
            for mapping in notes.screens:
                if mapping.interlude:
                    continue
                key = capture_key(mapping)
                for device in caps.devices:
                    if (key, device) not in have:
                        raise DepartmentOutputError(
                            cls.name, node_id, f"no {device} capture for {key!r}"
                        )

    @classmethod
    def check_params(cls, node_id: str, output: JsonValue, params: Mapping[str, JsonValue]) -> None:
        """The captures cover the devices the node asked for."""
        caps = UICaptures.model_validate(output)
        wanted = _devices(params.get("devices"))
        missing = sorted(set(wanted) - set(caps.devices))
        if missing:
            raise DepartmentOutputError(
                cls.name, node_id, f"captures miss devices: {', '.join(missing)}"
            )


CAPTURE_DEPARTMENTS: tuple[type[Department], ...] = (UICaptureDepartment,)


def capture_node(
    source: ProductSource,
    product_id: str = PRODUCT,
    *,
    devices: Sequence[str] = ("laptop", "phone"),
    fps: int = 30,
    verify_determinism: bool = True,
) -> NodeSpec:
    """The ``ui_capture`` node that captures the screen map of node ``product_id``."""
    unknown = [d for d in devices if d not in DEVICES]
    if unknown or not devices:
        raise ValueError(f"capture devices must be some of {sorted(DEVICES)}, got {devices!r}")
    if fps <= 0:
        raise ValueError(f"capture fps must be positive, got {fps}")
    return NodeSpec(
        id=UI_CAPTURE,
        kind=UI_CAPTURE,
        department=UICaptureDepartment.name,
        inputs=(product_id,),
        params={
            "product": source.model_dump(mode="json"),
            "devices": list(devices),
            "fps": fps,
            "verify_determinism": verify_determinism,
        },
    )


# ------------------------------------------------------------- screen steps -> DSL

_CSS_SELECTOR = re.compile(r"^(?:[#.\[]|text=)|^[A-Za-z][\w-]*\[")
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|sec|secs|seconds?)?\s*$", re.IGNORECASE)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def is_selector(target: str) -> bool:
    """``target`` reads as a selector (``#id``, ``.class``, ``[attr]``, ``tag[...]``, ``text=``)."""
    return bool(_CSS_SELECTOR.match(target.strip()))


def _selector(target: str) -> str:
    target = target.strip()
    return target if is_selector(target) else f"text={target}"


def _url(base_url: str, route: str) -> str:
    route = route.strip()
    if re.search(r"\[[^\]]*\]", route):
        raise ValueError(f"route {route!r} still has a [param] segment; fill it in the screen map")
    if route.startswith(("http://", "https://")):
        return route
    return base_url.rstrip("/") + "/" + route.lstrip("/")


def _wait_ms(step: ScreenStep) -> int:
    for raw in (step.value, step.target):
        m = _DURATION.match(raw or "")
        if m:
            amount = float(m.group(1))
            unit = (m.group(2) or "ms").lower()
            return round(amount if unit == "ms" else amount * 1000)
    return 1000


def _move(target: str) -> dict[str, JsonValue]:
    return {
        "do": "move",
        "to": {"selector": _selector(target)},
        "durationMs": MOVE_MS,
        "easing": "easeInOutCubic",
    }


def steps_to_actions(steps: Sequence[ScreenStep], base_url: str) -> list[dict[str, JsonValue]]:
    """Translate screen-map steps into capture DSL actions, ending with a 500 ms hold.

    Raises :class:`ValueError` for a ``visit`` route that still has a ``[param]``.
    """
    actions: list[dict[str, JsonValue]] = []
    for step in steps:
        if step.action == "visit":
            actions.append({"do": "goto", "url": _url(base_url, step.target)})
        elif step.action == "hover":
            actions.append(_move(step.target))
        elif step.action == "click":
            actions += [_move(step.target), {"do": "click"}]
        elif step.action == "type":
            actions += [_move(step.target), {"do": "click"}]
            if step.value:
                actions.append(
                    {
                        "do": "type",
                        "text": step.value,
                        "bpm": TYPE_BPM,
                        "subdivision": TYPE_SUBDIVISION,
                    }
                )
        elif step.action == "scroll":
            found = _NUMBER.search(step.value or "")
            scroll: dict[str, JsonValue] = {
                "do": "scroll",
                "velocity": float(found.group()) if found else SCROLL_VELOCITY,
            }
            if is_selector(step.target) and not step.target.startswith("text="):
                scroll["at"] = {"selector": step.target.strip()}
            actions.append(scroll)
        else:
            actions.append({"do": "wait", "ms": _wait_ms(step)})
    actions.append({"do": "wait", "ms": END_HOLD_MS})
    return actions


def _main_selector(steps: Sequence[ScreenStep]) -> str | None:
    """The first click/type/hover target that is a CSS selector (layer source)."""
    for step in steps:
        if step.action in ("click", "type", "hover"):
            target = step.target.strip()
            if is_selector(target) and not target.startswith("text="):
                return target
    return None


def login_actions(source: ProductSource, base_url: str) -> list[dict[str, JsonValue]]:
    """Unrecorded setup that signs in the demo account, typing from env var names only."""
    login = source.demo_login
    if login is None:
        return []
    type_args: dict[str, JsonValue] = {"bpm": 480, "subdivision": 4, "secret": True}
    return [
        {"do": "goto", "url": _url(base_url, login.route)},
        _move(login.username_selector),
        {"do": "click"},
        {"do": "type", "textEnv": login.username_env, **type_args},
        _move(login.password_selector),
        {"do": "click"},
        {"do": "type", "textEnv": login.password_env, **type_args},
        _move(login.submit_selector),
        {"do": "click"},
        {"do": "wait", "ms": LOGIN_SETTLE_MS},
    ]


# ------------------------------------------------------------------ demo data

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def _texts(record: Mapping[str, Any]) -> list[str]:
    texts = record.get("texts")
    return [t for t in texts if isinstance(t, str)] if isinstance(texts, list) else []


def check_demo_data(
    text_records: Sequence[Mapping[str, Any]], allow: DemoData, login_email: str = ""
) -> list[str]:
    """Problems with the recorded text: emails and names not on the demo allow-list.

    Every email-shaped string in the visible text must be in ``allow.emails`` or be
    the env-resolved ``login_email``; every text under ``allow.name_selectors``
    must be in ``allow.names``. An email is reported by its domain only.
    """
    emails = {e.casefold() for e in allow.emails}
    if login_email:
        emails.add(login_email.casefold())
    names = {n.strip().casefold() for n in allow.names}
    problems: list[str] = []
    seen: set[str] = set()
    for record in text_records:
        for text in _texts(record):
            for email in _EMAIL.findall(text):
                if email.casefold() in emails or email.casefold() in seen:
                    continue
                seen.add(email.casefold())
                domain = email.rsplit("@", 1)[-1]
                problems.append(f"email at {domain} is not on the demo allow-list")
        by_selector = record.get("selectors")
        if not isinstance(by_selector, Mapping):
            continue
        for sel in allow.name_selectors:
            found = by_selector.get(sel)
            for name in found if isinstance(found, list) else []:
                if not isinstance(name, str) or name.strip().casefold() in names:
                    continue
                if f"name:{name}" in seen:
                    continue
                seen.add(f"name:{name}")
                problems.append(f"name {name!r} under {sel} is not on the demo allow-list")
    return problems


# --------------------------------------------------------------------- runner


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _slug(key: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", key.casefold()).strip("-")[:60] or "capture"
    return f"{base}-{hashlib.sha1(key.encode()).hexdigest()[:8]}"  # noqa: S324 - dir name


def default_capture_command(env: Mapping[str, str] | None = None) -> list[str]:
    """``SPECTRACUT_CAPTURE_CMD``, else ``spectracut-capture`` on PATH, else the built CLI."""
    env = os.environ if env is None else env
    override = env.get(CAPTURE_CMD_ENV, "").strip()
    if override:
        return shlex.split(override)
    found = shutil.which("spectracut-capture")
    if found:
        return [found]
    return ["node", str(REPO_ROOT / "packages" / "capture" / "dist" / "cli.js")]


def rig_version() -> str:
    """``@spectracut/capture@<version>`` from the package's package.json."""
    try:
        meta = json.loads((REPO_ROOT / "packages" / "capture" / "package.json").read_text())
        return f"{RIG_PACKAGE}@{meta.get('version', 'unknown')}"
    except (OSError, ValueError):
        return f"{RIG_PACKAGE}@unknown"


def git_commit(repo: Path) -> str:
    """``git rev-parse HEAD`` of ``repo``; empty when it is not a git repo."""
    try:
        done = subprocess.run(  # noqa: S603 - argv list, no shell
            ["git", "-C", str(repo), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def url_answers(url: str) -> bool:
    """The URL answers an HTTP request (any status)."""
    try:
        with urllib.request.urlopen(url, timeout=2):  # noqa: S310 - the product's own URL
            return True
    except urllib.error.HTTPError:
        return True
    except (OSError, ValueError):
        return False


def _stop(proc: subprocess.Popen[bytes], grace: float = 10.0) -> None:
    """Stop the dev server's process group: SIGTERM, then SIGKILL."""
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except OSError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


Launcher = Callable[[list[str], Path], "subprocess.Popen[bytes]"]
CommandRunner = Callable[[list[str], Path], int]


def _launch(argv: list[str], cwd: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - argv list, no shell
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _run_command(argv: list[str], cwd: Path) -> int:
    done = subprocess.run(  # noqa: S603 - argv list, no shell
        argv, cwd=cwd, capture_output=True, check=False
    )
    return done.returncode


class CaptureRunner:
    """Builds ``ui_capture`` nodes by running the product and the capture CLI.

    Other kinds pass through to ``inner``. ``executor_factory`` builds the
    :class:`CaptureExecutor` from the command; ``launcher`` starts the dev server
    (own process group), ``run_command`` runs the seed command, ``probe`` checks the
    URL answers, ``commit`` reads the repo's git commit; all are injectable for tests.
    """

    def __init__(
        self,
        inner: Runner,
        departments: Mapping[str, type[Department]],
        *,
        workdir: Path,
        source: ProductSource | None = None,
        executor_factory: Callable[[Sequence[str]], CaptureExecutor] | None = None,
        command: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        launcher: Launcher = _launch,
        run_command: CommandRunner = _run_command,
        probe: Callable[[str], bool] = url_answers,
        commit: Callable[[Path], str] = git_commit,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        ready_timeout: float = READY_TIMEOUT_S,
    ) -> None:
        self.inner = inner
        self.departments = departments
        self.workdir = Path(workdir)
        self.source = source
        self.env: Mapping[str, str] = os.environ if env is None else env
        self.command = list(command) if command is not None else None
        self.executor_factory = executor_factory or (lambda cmd: CaptureExecutor(command=cmd))
        self.launcher = launcher
        self.run_command = run_command
        self.probe = probe
        self.commit = commit
        self.now = now
        self.sleep = sleep
        self.ready_timeout = ready_timeout

    def __call__(self, ctx: RunContext) -> RunResult:
        if ctx.spec.kind != UI_CAPTURE:
            return self.inner(ctx)
        return self._capture(ctx)

    def _fail(self, ctx: RunContext, detail: str) -> DepartmentOutputError:
        return DepartmentOutputError(ctx.spec.department, ctx.spec.id, detail)

    def _source(self, ctx: RunContext) -> ProductSource:
        raw = ctx.spec.params.get("product")
        if raw is not None:
            return ProductSource.model_validate(raw)
        if self.source is None:
            raise self._fail(ctx, "no product source: create the production with --product")
        return self.source

    def _capture(self, ctx: RunContext) -> RunResult:
        dept = self.departments.get(ctx.spec.department, UICaptureDepartment)
        kinds = {i: PRODUCT for i in ctx.spec.inputs}
        dept.validate_inputs(ctx.spec.id, ctx.inputs, kinds)
        notes = [ProductNotes.model_validate(ctx.inputs[i]) for i in ctx.spec.inputs]
        source = self._source(ctx)
        devices = _devices(ctx.spec.params.get("devices"))
        fps_raw = ctx.spec.params.get("fps", 30)
        fps = int(fps_raw) if isinstance(fps_raw, int | float) else 30
        verify = ctx.spec.params.get("verify_determinism", True) is not False
        mappings = [m for n in notes for m in n.screens if not m.interlude]

        if source.seed_command:
            code = self.run_command(shlex.split(source.seed_command), source.repo)
            if code != 0:
                raise self._fail(ctx, f"seed command exited {code}")
        server: subprocess.Popen[bytes] | None = None
        try:
            base_url = source.run.url
            if source.run.command:
                if not base_url:
                    raise self._fail(ctx, "run.command needs run.url to know where it serves")
                server = self.launcher(shlex.split(source.run.command), source.frontend)
            if base_url:
                self._wait_ready(ctx, base_url, server)
            captures = [
                self._one(ctx, source, mapping, DEVICES[device], base_url, fps, verify)
                for mapping in mappings
                for device in devices
            ]
        finally:
            if server is not None:
                _stop(server)
        output = UICaptures.model_validate(
            {"product_id": source.id, "devices": devices, "captures": captures}
        )
        value = dept.validate_output(ctx.spec.id, output.model_dump(mode="json"))
        dept.check_output(ctx.spec.id, value, ctx.inputs, kinds)
        dept.check_params(ctx.spec.id, value, ctx.spec.params)
        return RunResult(output=value, usage=Usage())

    def _wait_ready(
        self, ctx: RunContext, url: str, server: subprocess.Popen[bytes] | None
    ) -> None:
        deadline = self.now() + self.ready_timeout
        while True:
            if self.probe(url):
                return
            if server is not None and server.poll() is not None:
                raise self._fail(
                    ctx, f"dev server exited {server.returncode} before {url} answered"
                )
            if self.now() >= deadline:
                raise self._fail(ctx, f"{url} did not answer within {self.ready_timeout:.0f}s")
            self.sleep(1.0)

    def _spec(
        self,
        source: ProductSource,
        mapping: ScreenMapping,
        device: DeviceProfile,
        base_url: str,
        fps: int,
    ) -> dict[str, JsonValue]:
        script = steps_to_actions(mapping.steps, base_url)
        spec: dict[str, JsonValue] = {
            "url": _url(base_url, mapping.route),
            "viewport": {"width": device.width, "height": device.height},
            "deviceScaleFactor": device.device_scale_factor,
            "colorScheme": COLOR_SCHEME,
            "fps": fps,
            "script": list(script),
            "clean": {"secretSelectors": ["input[type=password]"]},
            "legibility": False,
            "textSelectors": list(source.demo_data.name_selectors),
        }
        main = _main_selector(mapping.steps)
        if main is not None:
            spec["layers"] = [{"name": "main", "selector": main}]
        setup = login_actions(source, base_url)
        if setup:
            spec["setup"] = list(setup)
        return spec

    def _run_capture(
        self, ctx: RunContext, spec: dict[str, JsonValue], out: Path
    ) -> tuple[Path, list[Path], list[str]]:
        if out.exists():
            shutil.rmtree(out)
        command = self.command or default_capture_command(self.env)
        executor = self.executor_factory(command)
        job = Job(
            id=f"{ctx.spec.id}-{out.parent.name}-{out.name}",
            kind="capture",
            payload={"spec": spec, "out_dir": str(out)},
        )
        try:
            result = executor.run(job)
        except Exception as exc:  # noqa: BLE001 - surfaced as the node's failure
            raise self._fail(ctx, f"capture into {out} failed: {exc}") from exc
        frames = [Path(f) for f in result.frames]
        if not frames:
            raise self._fail(ctx, f"capture into {out} wrote no frames")
        return out, frames, [_sha256_file(f) for f in frames]

    def _one(
        self,
        ctx: RunContext,
        source: ProductSource,
        mapping: ScreenMapping,
        device: DeviceProfile,
        base_url: str,
        fps: int,
        verify: bool,
    ) -> ScreenCapture:
        key = capture_key(mapping)
        name = f"{key} ({device.id})"
        try:
            spec = self._spec(source, mapping, device, base_url, fps)
        except ValueError as exc:
            raise self._fail(ctx, f"{name}: {exc}") from None
        root = self.workdir / _slug(key)
        out, frames, hashes = self._run_capture(ctx, spec, root / device.id)
        text_path = out / "text.json"
        try:
            records = json.loads(text_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise self._fail(ctx, f"{name}: capture left no readable text.json: {exc}") from None
        if not isinstance(records, list):
            raise self._fail(ctx, f"{name}: text.json is not a list")
        login_email = ""
        if source.demo_login is not None:
            login_email = self.env.get(source.demo_login.username_env, "")
        problems = check_demo_data(
            [r for r in records if isinstance(r, dict)], source.demo_data, login_email
        )
        if problems:
            raise self._fail(ctx, f"{name} shows non-demo data: {'; '.join(problems)}")
        if verify:
            _, _, again = self._run_capture(ctx, spec, root / f"{device.id}-verify")
            if again != hashes:
                raise self._fail(ctx, f"{name}: a second capture gave different frames")
        layers: list[CaptureFrame] = []
        layers_dir = out / "layers"
        if layers_dir.is_dir():
            for layer in sorted(p for p in layers_dir.iterdir() if p.is_dir()):
                last = layer / frames[-1].name
                if last.is_file():
                    layers.append(CaptureFrame(path=str(last), sha256=_sha256_file(last)))
        clip = [CaptureFrame(path=str(f), sha256=h) for f, h in zip(frames, hashes, strict=True)]
        canonical = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
        return ScreenCapture(
            key=key,
            proof_point=mapping.proof_point,
            beat=mapping.beat,
            route=mapping.route,
            url=str(spec["url"]),
            device=device.id,
            fps=fps,
            clip=clip,
            still=clip[-1],
            layers=layers,
            manifest=str(out / "manifest.json"),
            events=str(out / "events.json"),
            text=str(text_path),
            provenance=CaptureProvenance(
                product_id=source.id,
                repo_commit=self.commit(source.repo),
                url=str(spec["url"]),
                steps=list(mapping.steps),
                device=device.id,
                spec_sha256=hashlib.sha256(canonical).hexdigest(),
                frames_sha256=hashlib.sha256("\n".join(hashes).encode()).hexdigest(),
                rig=rig_version(),
                demo_check="passed",
                deterministic=verify,
            ),
        )
