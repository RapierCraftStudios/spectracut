# SPDX-License-Identifier: AGPL-3.0-or-later
"""release.yml builds every package and image on a vX.Y.Z tag, dry run by default."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
DOCKER = ROOT / "docker"


def load() -> dict[Any, Any]:
    data = yaml.safe_load(WORKFLOW.read_text())
    assert isinstance(data, dict)
    return data


def job(name: str) -> dict[str, Any]:
    jobs = load()["jobs"]
    assert name in jobs, f"release.yml has no {name!r} job"
    value = jobs[name]
    assert isinstance(value, dict)
    return value


def runs(name: str) -> str:
    return "\n".join(step.get("run", "") for step in job(name)["steps"])


def test_triggers_on_semver_tags() -> None:
    # PyYAML parses the bare key `on` as boolean True.
    triggers = load().get(True) or load().get("on")
    assert isinstance(triggers, dict)
    assert triggers["push"]["tags"] == ["v*.*.*"]


def test_top_level_permissions_are_read_only() -> None:
    assert load()["permissions"] == {"contents": "read"}


def test_publishing_is_gated_on_publish_enabled() -> None:
    assert "vars.PUBLISH_ENABLED == 'true'" in load()["env"]["PUBLISH"]
    publish = [s for s in job("python")["steps"] if "uv publish" in s.get("run", "")]
    assert publish and publish[0]["if"] == "env.PUBLISH == 'true'"
    npm = runs("npm")
    assert "--dry-run" in npm and 'if [ "${PUBLISH}" = "true" ]' in npm
    push = next(s for s in job("images")["steps"] if "build-push-action" in s.get("uses", ""))
    assert push["with"]["push"] == "${{ env.PUBLISH == 'true' }}"


def test_verifies_tag_against_package_versions() -> None:
    assert "scripts/check_release_version.py" in runs("verify")
    for name in ("python", "npm", "images"):
        assert job(name)["needs"] == "verify"


def test_builds_both_wheels_and_smoke_tests_with_pipx() -> None:
    text = runs("python")
    assert "uv build" in text and "--package spectracut-host" in text
    assert "pipx install dist/spectracut-*.whl" in text
    assert "spectracut --version" in text
    assert job("python")["permissions"]["id-token"] == "write"


def test_publishes_capture_package() -> None:
    text = runs("npm")
    assert "npm run build --workspace @spectracut/capture" in text
    assert "npm publish --workspace @spectracut/capture" in text


def test_builds_both_ghcr_images() -> None:
    images = job("images")
    files = {m["dockerfile"] for m in images["strategy"]["matrix"]["include"]}
    assert files == {"docker/Dockerfile.api", "docker/Dockerfile.host"}
    assert images["permissions"]["packages"] == "write"
    assert "ghcr.io/" in runs("images")


@pytest.mark.parametrize("name", ["Dockerfile.api", "Dockerfile.host"])
def test_dockerfiles_are_licensed_and_unprivileged(name: str) -> None:
    text = (DOCKER / name).read_text()
    assert text.startswith("# SPDX-License-Identifier: AGPL-3.0-or-later")
    assert 'org.opencontainers.image.licenses="AGPL-3.0-or-later"' in text
    assert "USER spectracut" in text


def build_stage_copies(name: str) -> list[str]:
    """Destinations (relative to WORKDIR) of the COPY lines in the `AS build` stage."""
    copies: list[str] = []
    in_build = False
    for raw in (DOCKER / name).read_text().splitlines():
        line = raw.strip()
        if line.upper().startswith("FROM "):
            in_build = line.lower().endswith(" as build")
            continue
        if in_build and line.upper().startswith("COPY ") and "--from" not in line:
            parts = line.split()[1:]
            sources, dest = parts[:-1], parts[-1]
            if dest in ("./", "."):
                copies.extend(s.rstrip("/") for s in sources)
            else:
                copies.append(dest.removeprefix("./").rstrip("/"))
    return copies


@pytest.mark.parametrize("name", ["Dockerfile.api", "Dockerfile.host"])
def test_build_stage_copies_every_wheel_force_include(name: str) -> None:
    # hatchling fails with "Forced include not found" when a force-include source is
    # missing from the build context, so every source must be copied into the build stage.
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    sources = list(wheel.get("force-include", {}))
    assert sources, "pyproject.toml lost its wheel force-include table"
    copied = build_stage_copies(name)
    for source in sources:
        assert any(source == c or source.startswith(c + "/") for c in copied), (
            f"{name} build stage never copies {source!r}, a wheel force-include"
        )


def test_api_image_never_installs_blender_or_host() -> None:
    lines = [
        line
        for line in (DOCKER / "Dockerfile.api").read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    body = "\n".join(lines).lower()
    for forbidden in ("blender", "bpy", "spectracut-host", "spectracut_host", "copy host "):
        assert forbidden not in body
    assert 'ENTRYPOINT ["spectracut"]' in "\n".join(lines)


def test_host_image_runs_the_host_agent() -> None:
    text = (DOCKER / "Dockerfile.host").read_text()
    assert "spectracut_host-*.whl" in text
    assert 'ENTRYPOINT ["spectracut-host"]' in text


def test_capture_package_is_publishable() -> None:
    manifest = json.loads((ROOT / "packages/capture/package.json").read_text())
    assert "private" not in manifest
    assert manifest["files"] == ["dist"]
    assert manifest["main"] == "dist/index.js"
    assert manifest["types"] == "dist/index.d.ts"
    assert manifest["scripts"]["build"] == "tsc -p tsconfig.build.json"
    build = json.loads((ROOT / "packages/capture/tsconfig.build.json").read_text())
    assert "src/**/*.test.ts" in build["exclude"]


def test_changelog_has_unreleased_section() -> None:
    assert "## [Unreleased]" in (ROOT / "CHANGELOG.md").read_text()
