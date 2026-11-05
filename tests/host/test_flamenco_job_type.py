# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``spectracut-shot`` Flamenco job type compiles a recipe job into exec tasks.

Runs the job-type script under node's ``vm`` with a stub ``author`` API, the
shape Flamenco's job compiler provides.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import pytest

from spectracut.queue.flamenco import job_type_script

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const [script, settingsJson] = process.argv.slice(1);
const tasks = [];
const author = {
  Task: (name, type) => ({ name, type, commands: [], addCommand(c) { this.commands.push(c); } }),
  Command: (type, params) => ({ type, params }),
};
const job = { settings: JSON.parse(settingsJson), addTask: (t) => tasks.push(t) };
const ctx = vm.createContext({ author });
vm.runInContext(fs.readFileSync(script, "utf8") + "\nthis.JOB_TYPE = JOB_TYPE;", ctx);
try {
  ctx.compileJob(job);
  console.log(JSON.stringify({ jobType: ctx.JOB_TYPE, tasks }));
} catch (e) {
  console.log(JSON.stringify({ error: String(e.message) }));
}
"""


def _compile(settings: dict[str, Any]) -> dict[str, Any]:
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", HARNESS, "--", str(job_type_script()), json.dumps(settings)],
        capture_output=True,
        text=True,
        check=True,
    )
    out: dict[str, Any] = json.loads(result.stdout)
    return out


SETTINGS = {
    "job_file": "/shared/jobs/job-1.json",
    "frames": "1-25",
    "chunk_size": 10,
    "output_dir": "/shared/out/shot-1",
}


def test_compiles_one_exec_task_per_chunk() -> None:
    out = _compile(SETTINGS)
    tasks = out["tasks"]
    assert [t["name"] for t in tasks] == ["render-1-10", "render-11-20", "render-21-25"]
    for task, frames in zip(tasks, ["1-10", "11-20", "21-25"], strict=True):
        assert task["type"] == "blender"
        (cmd,) = task["commands"]
        assert cmd["type"] == "exec"
        assert cmd["params"]["exe"] == "spectracut-host"
        assert cmd["params"]["args"] == [
            "run",
            "--job-file",
            "/shared/jobs/job-1.json",
            "--frames",
            frames,
            "--output",
            "/shared/out/shot-1",
        ]


def test_job_type_declares_settings() -> None:
    keys = [s["key"] for s in _compile(SETTINGS)["jobType"]["settings"]]
    assert keys == ["job_file", "frames", "chunk_size", "output_dir", "assets_dir"]
    (assets,) = [s for s in _compile(SETTINGS)["jobType"]["settings"] if s["key"] == "assets_dir"]
    assert assets["subtype"] == "dir_path" and assets["required"] is False


def test_single_frame() -> None:
    assert [t["name"] for t in _compile({**SETTINGS, "frames": "7"})["tasks"]] == ["render-7-7"]


@pytest.mark.parametrize(
    "change",
    [{"frames": "a-b"}, {"frames": "9-3"}, {"chunk_size": 0}, {"job_file": ""}],
)
def test_rejects_bad_settings(change: dict[str, Any]) -> None:
    assert "error" in _compile({**SETTINGS, **change})


def test_assets_dir_is_passed_to_the_host_when_set() -> None:
    tasks = _compile({**SETTINGS, "assets_dir": "/shared/assets"})["tasks"]
    for task in tasks:
        (cmd,) = task["commands"]
        assert cmd["params"]["args"][-2:] == ["--assets-dir", "/shared/assets"]
    (cmd,) = _compile({**SETTINGS, "assets_dir": ""})["tasks"][0]["commands"]
    assert "--assets-dir" not in cmd["params"]["args"]
