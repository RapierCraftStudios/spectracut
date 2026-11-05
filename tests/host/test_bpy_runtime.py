# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bpy runtime runs only registered declarative tools, only when launched by the guard.

The runtime never runs in the control plane, so these tests drive it through the
fake ``blender`` (a separate interpreter with a recording fake ``bpy``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "host" / "bpy_runtime" / "runner.py"


def _manifest(tmp_path: Path, steps: list[dict[str, Any]], token: str = "tok") -> Path:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "token": token,
                "steps": steps,
                "frame_start": 1,
                "frame_end": 2,
                "output_dir": str(out),
            }
        )
    )
    return path


def _blender(
    fake_bin: Path, manifest: Path, env_token: str | None, tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SPECTRACUT_")}
    env["FAKE_BPY_RECORD"] = str(tmp_path / "bpy.jsonl")
    if env_token is not None:
        env["SPECTRACUT_EXECUTION_TOKEN"] = env_token
    cmd = [str(fake_bin / "blender"), "-b", "--factory-startup", "--python-exit-code", "1"]
    cmd += ["--python", str(RUNNER), "--", str(manifest)]
    return subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)


GOOD_STEPS = [
    {"index": 0, "tool": "scene.clear", "args": {}},
    {"index": 1, "tool": "camera.add", "args": {"name": "Cam", "location": [0, -5, 1]}},
    {"index": 2, "tool": "camera.set_lens", "args": {"mm": 50}},
    {"index": 3, "tool": "render.set_engine", "args": {"engine": "eevee"}},
    {"index": 4, "tool": "render.set_resolution", "args": {"width": 64, "height": 36}},
]


def _calls(tmp_path: Path) -> list[str]:
    record = tmp_path / "bpy.jsonl"
    if not record.exists():
        return []
    calls: list[str] = []
    for line in record.read_text().splitlines():
        calls.extend(json.loads(line))
    return calls


def test_runs_registered_tools_and_renders(fake_bin: Path, tmp_path: Path) -> None:
    result = _blender(fake_bin, _manifest(tmp_path, GOOD_STEPS), "tok", tmp_path)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "frame_0001.png",
        "frame_0002.png",
        "report.json",
    ]
    calls = _calls(tmp_path)
    assert any("cameras.new('Cam')" in c for c in calls)
    assert any("render.render(animation=True)" in c for c in calls)


@pytest.mark.parametrize("env_token", [None, "", "wrong"])
def test_refuses_without_guard_token(fake_bin: Path, tmp_path: Path, env_token: str | None) -> None:
    result = _blender(fake_bin, _manifest(tmp_path, GOOD_STEPS), env_token, tmp_path)
    assert result.returncode == 1
    assert "not launched by the SpectraCut execution guard" in result.stderr
    assert not any("render.render" in c for c in _calls(tmp_path))


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ({"tool": "python.exec", "args": {"code": "print(1)"}}, "unknown tool"),
        ({"tool": "camera.set_lens", "args": {"mm": "35; import os"}}, "finite number"),
        ({"tool": "camera.set_lens", "args": {"mm": 35, "extra": 1}}, "unknown argument"),
        ({"tool": "object.add_primitive", "args": {"kind": "monkey"}}, "kind must be one of"),
        ({"tool": "render.set_resolution", "args": {"width": 1e9, "height": 2}}, "width"),
    ],
)
def test_refuses_unknown_tools_and_bad_args(
    fake_bin: Path, tmp_path: Path, step: dict[str, Any], message: str
) -> None:
    result = _blender(fake_bin, _manifest(tmp_path, [{"index": 0, **step}]), "tok", tmp_path)
    assert result.returncode == 1
    assert message in result.stderr
    assert not any("render.render" in c for c in _calls(tmp_path))


def test_runtime_never_imports_spectracut() -> None:
    for name in ("runner.py", "tools.py"):
        source = (RUNNER.parent / name).read_text()
        assert "import spectracut" not in source and "from spectracut" not in source
        assert "eval(" not in source and "exec(" not in source


def test_runner_usage_errors(tmp_path: Path) -> None:
    code = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from bpy_runtime import runner\n"
        "print(runner.main(['blender']), runner.main(['blender', '--', sys.argv[2]]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(RUNNER.parent.parent), str(tmp_path / "none.json")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.split() == ["1", "1"]


def _chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    import struct

    pos, found = 8, []
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        found.append((kind, data[pos + 8 : pos + 8 + length]))
        pos += 12 + length
    return found


def test_runner_disables_stamp_metadata_and_strips_png_text(
    fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_BPY_PNG", "1")
    frames = []
    for run in ("a", "b"):
        work = tmp_path / run
        work.mkdir()
        result = _blender(fake_bin, _manifest(work, GOOD_STEPS), "tok", work)
        assert result.returncode == 0, result.stderr
        stamps = json.loads((work / "bpy.jsonl.stamps").read_text())
        assert stamps == {
            "use_stamp": False,
            "use_stamp_date": False,
            "use_stamp_render_time": False,
        }
        frames.append((work / "out" / "frame_0001.png").read_bytes())
    kinds = [k for k, _ in _chunks(frames[0])]
    assert kinds == [b"IHDR", b"IDAT", b"IEND"]
    assert frames[0] == frames[1]
