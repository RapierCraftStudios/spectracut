# SPDX-License-Identifier: AGPL-3.0-or-later
"""model_assets: every family covered, every source ungated and pinned, licences set."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from spectracut.providers.local.registry import MODELS
from spectracut.setup import model_assets
from spectracut.setup.model_assets import (
    ASSETS,
    LLAMA_CPP_RELEASE,
    MODEL_ROOT_ENV,
    asset_for,
    default_model_root,
    extras_digest,
    model_version,
)

SHA = re.compile(r"^[0-9a-f]{40}$")
HOST_PYPROJECT = Path(__file__).resolve().parents[2] / "host" / "pyproject.toml"


def test_assets_cover_exactly_the_registry_families() -> None:
    assert set(ASSETS) == {m.family for m in MODELS.values()}
    for spec in MODELS.values():
        assert spec.id in asset_for(spec.id).model_ids
        assert asset_for(spec.id).family == spec.family


def test_every_source_is_ungated_and_pinned_to_a_commit() -> None:
    for asset in ASSETS.values():
        assert asset.gated is False, asset.family
        assert asset.size_gb > 0
        assert asset.weights, asset.family
        for w in asset.weights:
            assert SHA.match(w.revision), (asset.family, w.repo)
        assert (asset.git_repo is None) == (asset.git_commit is None)
        if asset.git_commit is not None:
            assert SHA.match(asset.git_commit), asset.family


def test_registry_source_is_the_first_weights_repo() -> None:
    for spec in MODELS.values():
        assert asset_for(spec.id).weights[0].repo == spec.source


def test_extra_repos_named_by_the_selection_doc_are_pulled() -> None:
    repos = {w.repo for a in ASSETS.values() for w in a.weights}
    assert {
        "google/flan-t5-xl",
        "ACE-Step/acestep-5Hz-lm-0.6B",
        "Tongyi-MAI/Z-Image-Turbo",
    } <= repos
    assert {a.family for a in ASSETS.values() if a.git_repo} == {
        "ezaudio",
        "hunyuan-foley",
        "ace-step",
    }
    assert LLAMA_CPP_RELEASE in " ".join(ASSETS["llama-cpp"].windows_archives)


def test_no_model_is_gated_and_every_licence_is_set() -> None:
    for spec in MODELS.values():
        assert spec.licence.strip(), spec.id
        assert not asset_for(spec.id).gated
    assert MODELS["hunyuan-foley-xl"].licence == "Tencent Hunyuan Community"


def test_hf_token_is_never_read() -> None:
    src = Path(model_assets.__file__).read_text(encoding="utf-8")
    assert 'os.environ.get("HF_TOKEN' not in src and 'environ["HF_TOKEN' not in src


def test_host_extras_match_the_families_with_exact_pins() -> None:
    project = tomllib.loads(HOST_PYPROJECT.read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    assert set(extras) == {a.extra for a in ASSETS.values()} == set(ASSETS)
    for name, reqs in extras.items():
        for req in reqs:
            assert "==" in req, (name, req)
    assert "transformers==5.0.0" in extras["moss-sfx"]


def test_model_version_tracks_the_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    before = model_version("ezaudio-xl")
    assert len(before) == 12 and before != model_version("kokoro-82m")
    old = ASSETS["ezaudio"]
    import dataclasses

    moved = dataclasses.replace(old, git_commit="0" * 40)
    monkeypatch.setitem(ASSETS, "ezaudio", moved)
    assert model_version("ezaudio-xl") != before
    assert len(extras_digest()) == 12


def test_default_model_root_honours_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MODEL_ROOT_ENV, str(tmp_path))
    assert default_model_root() == tmp_path
    monkeypatch.delenv(MODEL_ROOT_ENV)
    assert default_model_root().parts[-2:] == ("spectracut", "models")
