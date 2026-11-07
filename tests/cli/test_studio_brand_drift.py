# SPDX-License-Identifier: AGPL-3.0-or-later
"""``studio new --product --brand-kit`` reports brand drift; the stored kit is the corrected one (#356)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from spectracut.cli.app import app
from spectracut.spec.brand import BrandKit, load_brand_kit
from spectracut.studio.departments import art as art_depts
from spectracut.studio.store import ProductionStore

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "brands" / "alterlab" / "brand-kit.yaml"
BRIEF = ROOT / "productions" / "alterlab-launch" / "brief.json"
APP = ROOT / "tests" / "studio" / "fixtures" / "themed-app"

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    monkeypatch.setenv("SPECTRACUT_HOME", str(root))
    monkeypatch.setenv("SPECTRACUT_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("SPECTRACUT_PROVIDERS", "mock")
    return root


@pytest.fixture
def product(tmp_path: Path) -> Path:
    path = tmp_path / "product.yaml"
    doc = {"id": "themed", "repo": str(APP), "run": {"url": "http://localhost:3000"}}
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _kit_file(tmp_path: Path, swatches: dict[str, str]) -> Path:
    raw = load_brand_kit(KIT).model_dump(mode="json")
    raw["palette"] = {"swatches": swatches}
    raw["token_sources"] = {}
    path = tmp_path / "brand-kit.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _new(*extra: str) -> str:
    result = runner.invoke(app, ["studio", "new", str(BRIEF), "--profile", "standard", *extra])
    assert result.exit_code == 0, result.output
    return result.output


def test_new_reports_brand_drift_from_the_product_theme(
    home: Path, tmp_path: Path, product: Path
) -> None:
    kit = _kit_file(tmp_path, {"ink": "#030303", "surface": "#f8f8f8", "accent": "#2f6bff"})
    output = _new("--product", str(product), "--brand-kit", str(kit))
    assert "brand drift: swatch accent #2f6bff is not in the product theme" in output
    assert "brand drift: swatch ink" not in output


def test_new_reports_a_matching_kit(home: Path, tmp_path: Path, product: Path) -> None:
    kit = _kit_file(tmp_path, {"ink": "#030303", "surface": "#f8f8f8", "accent": "#ff0000"})
    output = _new("--product", str(product), "--brand-kit", str(kit), "--force")
    assert "brand kit alterlab matches the product theme" in output
    assert "brand drift" not in output


def test_new_without_product_prints_no_drift(home: Path) -> None:
    assert "brand drift" not in _new("--brand-kit", str(KIT))


def test_unreadable_theme_is_a_warning(home: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    (empty / "app").mkdir(parents=True)
    path = tmp_path / "product.yaml"
    doc = {"id": "empty", "repo": str(empty), "run": {"url": "http://localhost:3000"}}
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    output = _new("--product", str(path), "--brand-kit", str(KIT))
    assert "brand drift: cannot read product theme:" in output


def test_art_direction_reads_corrected_kit(home: Path) -> None:
    output = _new("--brand-kit", str(KIT), "--id", "alterlab-launch")
    assert "production alterlab-launch created" in output
    graph, _ = ProductionStore(home).load("alterlab-launch")
    spec = graph.spec(art_depts.BRAND_KIT)
    stored = BrandKit.model_validate(spec.params[art_depts.BRAND_KIT_PARAM])
    assert stored.palette.swatches == load_brand_kit(KIT).palette.swatches
    assert "#2f6bff" not in stored.palette.swatches.values()
    art = next(s for s in graph.order() if graph.spec(s).kind == art_depts.ART_DIRECTION)
    assert art_depts.BRAND_KIT in graph.spec(art).inputs
