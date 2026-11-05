# SPDX-License-Identifier: AGPL-3.0-or-later
"""Step timeouts, retries and framed Windows stdin (issue #150: setup hung on a lost EOF)."""

from __future__ import annotations

import base64
import hashlib
import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from test_plan import CRED, FACTS, WHEELS, WORKERS, FakeRunner  # type: ignore[import-not-found]

from spectracut.setup.manifest import Step, load_manifest
from spectracut.setup.plan import (
    RETRY_KINDS,
    STEP_TIMEOUTS,
    CommandRunner,
    SetupError,
    SetupState,
    StepTimeout,
    apply_plan,
    build_plan,
    load_state,
    run_with_retry,
)
from spectracut.setup.storage import CONTROL, StorageTarget
from spectracut.setup.transport import (
    HEADER_CHARS,
    LENGTH_DIGITS,
    STEP_INPUT,
    frame_input,
    windows_input_preamble,
)


def _no_sleep(_s: float) -> None:
    return None


def read_frame(read: Callable[[int], str]) -> str:
    """Python mirror of :func:`windows_input_preamble`: exact counts, never EOF."""

    def exact(n: int) -> str:
        got = ""
        while len(got) < n:
            chunk = read(n - len(got))
            if not chunk:
                raise EOFError(f"short read: {len(got)} of {n}")
            got += chunk
        return got

    header = exact(HEADER_CHARS)
    n, want = int(header[:LENGTH_DIGITS]), header[LENGTH_DIGITS:]
    payload = exact(n)
    if hashlib.sha256(payload.encode("ascii")).hexdigest() != want:
        raise ValueError("sha256 mismatch")
    return base64.b64decode(payload).decode("utf-8")


def _reader(data: str, chunk: int = 7) -> Callable[[int], str]:
    pos = 0

    def read(n: int) -> str:
        nonlocal pos
        out = data[pos : pos + min(n, chunk)]
        pos += len(out)
        return out

    return read


def _windows_plan(storage: StorageTarget | None = None):  # type: ignore[no-untyped-def]
    return build_plan(
        load_manifest(), "2026.11", FACTS, WORKERS, SetupState(), CRED, storage, wheels=WHEELS
    )


# --- timeouts -------------------------------------------------------------------


def test_step_kinds_have_their_own_timeouts() -> None:
    assert STEP_TIMEOUTS["copy"] == STEP_TIMEOUTS["write"] == 120
    assert STEP_TIMEOUTS["install"] > STEP_TIMEOUTS["copy"]
    runner = CommandRunner()
    assert runner.timeout_for("copy") == 120 and runner.timeout_for("install") == 1800
    assert CommandRunner(timeout=5).timeout_for("install") == 5  # explicit override


def test_plan_steps_carry_kinds() -> None:
    plan = _windows_plan()
    kinds = {s.id.split(":")[0]: s.kind for s in plan.steps}
    assert kinds["wheel"] == "copy" and kinds["install"] == "install"
    assert kinds["worker-args"] == "write" and kinds["gpu-script"] == "write"
    assert all(s.kind == "copy" for s in plan.steps if s.id.startswith("wheel:"))


def test_timed_out_step_kills_its_process_group_and_reports_the_step(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    step = Step(
        CONTROL,
        "hang",
        "copy the hanging wheel",
        f"sleep 30 & echo $! > {pidfile}; wait",
        kind="copy",
    )
    start = time.monotonic()
    with pytest.raises(StepTimeout) as err:
        CommandRunner(timeout=0.5).run(step)
    assert time.monotonic() - start < 10
    assert "copy the hanging wheel" in str(err.value) and "timed out" in str(err.value)
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(child):
        time.sleep(0.05)
    assert not _alive(child), "the grandchild sleep survived the timeout"


def _alive(pid: int) -> bool:
    """Running and not a zombie (a killed orphan may linger unreaped for a moment)."""
    try:
        os.kill(pid, 0)
        state = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][:1]
    except (ProcessLookupError, FileNotFoundError):
        return False
    return state != "Z"


def test_quick_step_still_returns_its_output() -> None:
    step = Step(CONTROL, "ok", "echo", "printf hi", artifact="x.json", kind="write")
    assert CommandRunner().run(step) == "hi"


# --- retries --------------------------------------------------------------------


def test_idempotent_kinds_are_retried_with_backoff() -> None:
    assert {"copy", "write", "probe", "task"} == set(RETRY_KINDS)
    calls: list[int] = []
    slept: list[float] = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise StepTimeout("legion: copy timed out")
        return "done"

    assert run_with_retry(flaky, "copy", slept.append) == "done"
    assert len(calls) == 3 and slept == [2.0, 4.0]


def test_retries_give_up_after_three() -> None:
    calls: list[int] = []
    slept: list[float] = []

    def broken() -> None:
        calls.append(1)
        raise SetupError("legion: write failed")

    with pytest.raises(SetupError):
        run_with_retry(broken, "write", slept.append)
    assert len(calls) == 4 and slept == [2.0, 4.0, 8.0]


@pytest.mark.parametrize("kind", ["install", "run"])
def test_non_idempotent_kinds_run_once(kind: str) -> None:
    calls: list[int] = []

    def broken() -> None:
        calls.append(1)
        raise StepTimeout("x")

    with pytest.raises(StepTimeout):
        run_with_retry(broken, kind, _no_sleep)  # type: ignore[arg-type]
    assert calls == [1]


class FirstCopyHangs(FakeRunner):  # type: ignore[misc]
    """The first wheel copy really hangs (a sleeping process) and is killed by its timeout."""

    def __init__(self) -> None:
        super().__init__()
        self.hung = False

    def run(self, step: Step) -> str | None:
        if step.kind == "copy" and not self.hung:
            self.hung = True
            CommandRunner(timeout=0.3).run(Step(CONTROL, step.id, step.summary, "sleep 30"))
        return super().run(step)  # type: ignore[no-any-return]


def test_setup_completes_when_the_first_copy_hangs(tmp_path: Path) -> None:
    runner = FirstCopyHangs()
    plan = _windows_plan()
    apply_plan(plan, runner, SetupState(), tmp_path, sleep=_no_sleep)
    assert runner.hung
    assert [s.id for s in runner.ran] == [s.id for s in plan.steps]  # every step, once
    state = load_state(tmp_path)
    assert state.hosts["legion"].components["spectracut-host"] == WHEELS.pin


def test_a_hanging_install_is_not_retried(tmp_path: Path) -> None:
    class InstallHangs(FakeRunner):  # type: ignore[misc]
        def run(self, step: Step) -> str | None:
            if step.kind == "install":
                raise StepTimeout(f"{step.host}: {step.summary} timed out")
            return super().run(step)  # type: ignore[no-any-return]

    runner = InstallHangs()
    with pytest.raises(StepTimeout, match="install"):
        apply_plan(_windows_plan(), runner, SetupState(), tmp_path, sleep=_no_sleep)


# --- framed stdin ---------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "pw-secret\n", "naïve ✓ ünïcode\r\n" * 50])
def test_frame_round_trips_and_ignores_trailing_bytes(text: str) -> None:
    framed = frame_input(text)
    assert framed.isascii()
    assert read_frame(_reader(framed + "TRAILING-GARBAGE")) == text


def test_short_read_and_corrupt_payload_fail() -> None:
    framed = frame_input("x" * 300)
    with pytest.raises(EOFError):
        read_frame(_reader(framed[:-5]))
    flipped = framed[:-1] + ("A" if framed[-1] != "A" else "B")
    with pytest.raises(ValueError, match="sha256"):
        read_frame(_reader(flipped))


def test_preamble_reads_exact_counts_and_carries_no_payload() -> None:
    pre = windows_input_preamble()
    assert "ReadToEnd" not in pre and "ReadLine" not in pre
    assert "[Console]::In.Read($b" in pre and "SHA256" in pre and STEP_INPUT in pre
    assert str(HEADER_CHARS) in pre


def test_powershell_stdin_is_framed_and_kept_off_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    sent: dict[str, object] = {}

    class FakePopen:
        pid = 0
        returncode = 0

        def __init__(self, argv: list[str], **_: object) -> None:
            sent["argv"] = argv

        def communicate(
            self, data: str | None = None, timeout: float | None = None
        ) -> tuple[str, str]:
            sent["stdin"], sent["timeout"] = data, timeout
            return "", ""

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    step = Step("legion", "w", "store the password", "Write-Output 1", "powershell")
    step = Step(**{**step.__dict__, "stdin": "pw-secret-value\n", "kind": "write"})
    CommandRunner().run(step)
    argv = sent["argv"]
    assert isinstance(argv, list)
    script = base64.b64decode(argv[-1].rsplit(" ", 1)[1]).decode("utf-16-le")
    assert windows_input_preamble() in script
    assert sent["stdin"] == frame_input("pw-secret-value\n")
    digest = hashlib.sha256(base64.b64encode(b"pw-secret-value\n")).hexdigest()
    assert digest not in script and "pw-secret-value" not in script
    assert sent["timeout"] == 120


@pytest.mark.parametrize(
    "storage", [None, StorageTarget(kind="host", host="legion")], ids=["rig", "legion"]
)
def test_no_windows_step_waits_for_stdin_eof(storage: StorageTarget | None) -> None:
    plan = _windows_plan(storage)
    win = [s for s in plan.steps if s.shell == "powershell"]
    assert any(s.stdin for s in win)
    for s in win:
        assert "ReadToEnd" not in s.command and "ReadLine" not in s.command, s.id
        if s.stdin is not None:
            assert STEP_INPUT in s.command, s.id


def test_windows_wheel_copy_writes_part_checks_sha256_and_renames() -> None:
    plan = _windows_plan()
    for wheel in WHEELS.files:
        step = next(s for s in plan.steps if s.id == f"wheel:{wheel.name}:legion")
        sha = hashlib.sha256(wheel.read_bytes()).hexdigest().upper()
        cmd = step.command
        assert ".part" in cmd and "Get-FileHash" in cmd and sha in cmd and "Move-Item" in cmd
        assert cmd.index("Get-FileHash") < cmd.index("Move-Item")
        assert "exit 1" in cmd  # a mismatch fails the step
        assert base64.b64decode(step.stdin or "") == wheel.read_bytes()


# --- a stopped worker is never left down ---------------------------------------


class FailsOn(FakeRunner):  # type: ignore[misc]
    def __init__(self, kind: str, *, also_start: bool = False) -> None:
        super().__init__()
        self.kind, self.also_start, self.attempts = kind, also_start, 0

    def run(self, step: Step) -> str | None:
        if (
            step.kind == self.kind
            and step.id.startswith(("install:spectracut-host:", "wheel:"))
            and step.host == "legion"
        ):
            self.attempts += 1
            raise StepTimeout(f"{step.host}: {step.summary} timed out")
        if self.also_start and step.id.startswith("worker:start:"):
            raise SetupError(f"{step.host}: start failed")
        return super().run(step)  # type: ignore[no-any-return]


def test_copies_run_before_the_worker_is_stopped() -> None:
    ids = [s.id for s in _windows_plan().steps]
    stop, start = ids.index("worker:stop:legion"), ids.index("worker:start:legion")
    assert (
        max(i for i, s in enumerate(ids) if s.startswith("wheel:") and s.endswith(":legion"))
        < stop
        < start
    )


def test_stalled_copy_fails_bounded_and_never_stops_the_worker(tmp_path: Path) -> None:
    runner, slept = FailsOn("copy"), []
    with pytest.raises(StepTimeout, match="copy"):
        apply_plan(_windows_plan(), runner, SetupState(), tmp_path, sleep=slept.append)
    assert runner.attempts == 4 and slept == [2.0, 4.0, 8.0]
    assert not any(s.id.startswith("worker:stop:") for s in runner.ran)


def test_failed_install_restarts_the_stopped_worker(tmp_path: Path) -> None:
    runner = FailsOn("install")
    with pytest.raises(StepTimeout, match="install"):
        apply_plan(_windows_plan(), runner, SetupState(), tmp_path, sleep=_no_sleep)
    ids = [s.id for s in runner.ran]
    assert "worker:stop:legion" in ids and ids[-1] == "worker:start:legion"


def test_failed_restart_is_reported_with_the_original_error(tmp_path: Path) -> None:
    runner = FailsOn("install", also_start=True)
    with pytest.raises(SetupError, match=r"install.*restarting the worker failed.*start failed"):
        apply_plan(_windows_plan(), runner, SetupState(), tmp_path, sleep=_no_sleep)
