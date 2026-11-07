# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut-host``: the render host agent next to Blender on GPU hosts.

Commands::

    spectracut-host run --job-file JOB.json [--frames A-B] --output DIR [--assets-dir DIR]
        Flamenco task entry: verify a signed job through the guard and render it.
        ``--assets-dir`` overrides SPECTRACUT_ASSETS_DIR.
    spectracut-host --once --job ID [--jobs-dir DIR] --output DIR
        Pull exactly one signed job (shared storage or the control plane over
        HTTP via SPECTRACUT_CONTROL_URL), run it and exit. Used in computeshare
        sandboxes.
    spectracut-host profile
        Print this host's render profile (Blender version, GPU model, driver).
    spectracut-host tags [--capability NAME ...]
        Print the Flamenco worker tags for this host (GPU model, VRAM, capabilities).
    spectracut-host models-serve
        Serve local model sessions over JSON lines on stdin/stdout; the control
        plane runs it through ssh (``spectracut.providers.local.remote``).
    spectracut-host --version
        Print the installed spectracut-host version (setup checks it equals the
        control plane's).

Exit codes: 0 done, 1 usage, 2 refused by the guard, 3 render failed, 4 pull failed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from spectracut.queue.profile import RenderProfile, frame_cache_key
from spectracut.queue.signing import JobSignatureError, SignedJob, parse_signed_job
from spectracut_host import __version__
from spectracut_host.guard import ExecutionGuard, GuardRefused, load_registration
from spectracut_host.profile import DEFAULT_CAPABILITIES, probe_profile, worker_tags

BLENDER_ENV = "SPECTRACUT_BLENDER"
CONTROL_URL_ENV = "SPECTRACUT_CONTROL_URL"
HOST_KEY_ENV = "SPECTRACUT_HOST_KEY"

EXIT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_RENDER_FAILED, EXIT_PULL_FAILED = 0, 1, 2, 3, 4
_JOB_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


class PullError(Exception):
    """A job could not be fetched from its source."""


class JobSource(Protocol):
    def fetch(self, job_id: str) -> bytes: ...


def _check_job_id(job_id: str) -> str:
    if not _JOB_ID.match(job_id):
        raise PullError(f"invalid job id {job_id!r}")
    return job_id


class DirJobSource:
    """Signed jobs on shared storage: ``<root>/<job_id>.json``."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def fetch(self, job_id: str) -> bytes:
        path = self.root / f"{_check_job_id(job_id)}.json"
        try:
            return path.read_bytes()
        except OSError as exc:
            raise PullError(f"can't read job {job_id}: {exc.strerror or exc}") from None


class HttpJobSource:
    """Signed jobs from the control plane: ``GET <base>/api/hosts/jobs/<job_id>``."""

    def __init__(self, base_url: str, host_id: str, host_key: str | None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.host_id = host_id
        self.host_key = host_key
        self.timeout = timeout

    def fetch(self, job_id: str) -> bytes:
        headers = {"Accept": "application/json", "X-SpectraCut-Host": self.host_id}
        if self.host_key:
            headers["Authorization"] = f"Bearer {self.host_key}"
        url = f"{self.base_url}/api/hosts/jobs/{_check_job_id(job_id)}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body: bytes = resp.read()
                return body
        except urllib.error.HTTPError as exc:
            raise PullError(f"control plane returned HTTP {exc.code} for job {job_id}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise PullError(f"can't reach the control plane: {exc}") from None


def _blender(args: argparse.Namespace) -> str:
    return str(args.blender or os.environ.get(BLENDER_ENV) or "blender")


def result_name(envelope_range: tuple[int, int], frames: tuple[int, int]) -> str:
    """``result.json`` for the whole signed range, ``result-A-B.json`` for a chunk."""
    if frames == envelope_range:
        return "result.json"
    return f"result-{frames[0]}-{frames[1]}.json"


def read_scene_graph_hash(output: Path) -> str | None:
    """The ``scene_graph_hash`` the bpy runtime wrote to ``report.json``, if any."""
    try:
        data = json.loads((output / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("scene_graph_hash") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def read_render_device(output: Path) -> dict[str, Any] | None:
    """The ``render_device`` the bpy runtime wrote to ``report.json``, if any."""
    try:
        data = json.loads((output / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("render_device") if isinstance(data, dict) else None
    if isinstance(value, dict) and isinstance(value.get("device"), str):
        return value
    return None


def execute(
    signed: SignedJob,
    frames: str | None,
    output: Path,
    blender: str,
    assets_dir: str | None = None,
) -> int:
    """Verify, render and write the result manifest; return an exit code."""
    registration = load_registration()
    profile: RenderProfile = probe_profile(blender)
    try:
        guard = ExecutionGuard(registration, profile)
        envelope, chunk = guard.check(signed, frames)
    except GuardRefused as exc:
        print(f"spectracut-host: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (OSError, ValueError) as exc:
        print(f"spectracut-host: bpy runtime unavailable: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    proc = guard.launch(envelope, chunk, output, blender_bin=blender, assets_dir=assets_dir)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").splitlines()[-20:])
        print(f"spectracut-host: Blender exited {proc.returncode}\n{tail}", file=sys.stderr)
        return EXIT_RENDER_FAILED
    result = {
        "job_id": envelope.job_id,
        "host_id": registration.host_id if registration else None,
        "recipe_hash": envelope.recipe_hash,
        "frames": list(chunk),
        "render_profile": profile.model_dump(mode="json"),
        "frame_cache_key": frame_cache_key(envelope.recipe_hash, profile),
        "scene_graph_hash": read_scene_graph_hash(output),
        "render_device": read_render_device(output),
    }
    name = result_name((envelope.frame_start, envelope.frame_end), chunk)
    (output / name).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    try:
        signed = parse_signed_job(Path(args.job_file).read_bytes())
    except OSError as exc:
        print(f"spectracut-host: can't read job file: {exc.strerror or exc}", file=sys.stderr)
        return EXIT_PULL_FAILED
    except JobSignatureError as exc:
        print(f"spectracut-host: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    return execute(signed, args.frames, Path(args.output), _blender(args), args.assets_dir)


def cmd_once(args: argparse.Namespace) -> int:
    source: JobSource
    if args.jobs_dir:
        source = DirJobSource(Path(args.jobs_dir))
    else:
        url = os.environ.get(CONTROL_URL_ENV)
        if not url:
            print(f"spectracut-host: --once needs --jobs-dir or {CONTROL_URL_ENV}", file=sys.stderr)
            return EXIT_USAGE
        registration = load_registration()
        source = HttpJobSource(
            url, registration.host_id if registration else "", os.environ.get(HOST_KEY_ENV)
        )
    try:
        signed = parse_signed_job(source.fetch(args.job))
    except PullError as exc:
        print(f"spectracut-host: {exc}", file=sys.stderr)
        return EXIT_PULL_FAILED
    except JobSignatureError as exc:
        print(f"spectracut-host: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if signed.envelope.job_id != args.job:
        print(
            f"spectracut-host: refused: asked for job {args.job}, got {signed.envelope.job_id}",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    return execute(signed, None, Path(args.output), _blender(args))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spectracut-host", description=__doc__.split("\n")[0])
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--once", action="store_true", help="pull one job, run it and exit")
    parser.add_argument("--job", help="job id for --once")
    parser.add_argument("--jobs-dir", help="shared-storage directory of signed jobs (--once)")
    parser.add_argument("--output", help="output directory (--once)")
    parser.add_argument("--blender", help=f"Blender binary (default ${{{BLENDER_ENV}}} or blender)")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="verify and render a signed job file")
    run.add_argument("--job-file", required=True)
    run.add_argument("--frames", help="frame range to render, within the signed range")
    run.add_argument("--output", required=True)
    run.add_argument("--blender")
    run.add_argument("--assets-dir", help="assets root (overrides SPECTRACUT_ASSETS_DIR)")

    prof = sub.add_parser("profile", help="print this host's render profile")
    prof.add_argument("--blender")

    tags = sub.add_parser("tags", help="print this host's Flamenco worker tags")
    tags.add_argument("--blender")
    tags.add_argument("--capability", action="append", dest="capabilities")

    sub.add_parser("models-serve", help="serve local model sessions over stdin/stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.once:
        if args.command or not args.job or not args.output:
            print("spectracut-host: --once needs --job ID and --output DIR", file=sys.stderr)
            return EXIT_USAGE
        return cmd_once(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "profile":
        print(probe_profile(_blender(args)).model_dump_json(indent=2))
        return EXIT_OK
    if args.command == "tags":
        profile = probe_profile(_blender(args))
        caps = args.capabilities or list(DEFAULT_CAPABILITIES)
        print("\n".join(worker_tags(profile, caps)))
        return EXIT_OK
    if args.command == "models-serve":
        from spectracut_host.models import serve

        return serve(sys.stdin, sys.stdout)
    parser.print_help(sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
