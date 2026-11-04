# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/check_release_version.py rejects tags that disagree with package versions."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_release_version", ROOT / "scripts" / "check_release_version.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


crv = load_script()


def write_repo(root: Path, py: str, host: str, npm: str) -> None:
    (root / "host").mkdir()
    (root / "packages" / "capture").mkdir(parents=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "spectracut"\nversion = "{py}"\n')
    (root / "host" / "pyproject.toml").write_text(
        f'[project]\nname = "spectracut-host"\nversion = "{host}"\n'
    )
    (root / "packages" / "capture" / "package.json").write_text(
        json.dumps({"name": "@spectracut/capture", "version": npm})
    )


def test_repo_versions_match_their_own_tag() -> None:
    versions = crv.package_versions()
    assert set(versions) == {"spectracut", "spectracut-host", "@spectracut/capture"}
    assert len(set(versions.values())) == 1
    assert crv.mismatches(f"v{versions['spectracut']}") == []


def test_matching_tag_passes(tmp_path: Path) -> None:
    write_repo(tmp_path, "1.2.3", "1.2.3", "1.2.3")
    assert crv.mismatches("v1.2.3", tmp_path) == []


def test_mismatch_names_each_package(tmp_path: Path) -> None:
    write_repo(tmp_path, "1.2.3", "1.2.2", "1.0.0")
    problems = crv.mismatches("v1.2.3", tmp_path)
    assert len(problems) == 2
    assert any("spectracut-host is 1.2.2" in p for p in problems)
    assert any("@spectracut/capture is 1.0.0" in p for p in problems)


@pytest.mark.parametrize("tag", ["1.2.3", "v1.2", "v1.2.3-rc1", "release"])
def test_rejects_non_semver_tags(tag: str) -> None:
    with pytest.raises(ValueError):
        crv.tag_version(tag)


def test_main_exit_codes() -> None:
    version = crv.package_versions()["spectracut"]
    assert crv.main([f"v{version}"]) == 0
    assert crv.main(["v999.0.0"]) == 1
    assert crv.main([]) == 1
