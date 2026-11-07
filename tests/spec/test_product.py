# SPDX-License-Identifier: AGPL-3.0-or-later
"""Product source (#337): product.yaml, the Next.js route scan, no secrets in the repo."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from spectracut.spec.product import (
    ProductSource,
    frontend_pages,
    load_product_source,
    product_routes,
    scan_nextjs_routes,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "studio" / "fixtures"
APP = FIXTURES / "nextjs-app"
ALTERLAB = ROOT / "brands" / "alterlab" / "product.yaml"


def test_route_map_from_a_fixture_nextjs_app() -> None:
    routes = scan_nextjs_routes(APP)
    assert [r.path for r in routes] == [
        "/",
        "/dashboard",
        "/dashboard/billing",
        "/docs",  # (marketing) route group stripped
        "/integrations/[platform]",
        "/legacy",  # pages router index
        "/playground",
    ]  # _lib (private), @modal (slot), api routes, pages/_app and pages/api skipped
    by_path = {r.path: r for r in routes}
    billing = by_path["/dashboard/billing"]
    assert billing.file == "src/app/dashboard/billing/page.tsx"
    assert billing.components == ["./row", "@/components/dashboard/BillingBreakdown"]
    assert by_path["/integrations/[platform]"].dynamic
    assert not by_path["/dashboard"].dynamic
    assert "react" not in by_path["/dashboard"].components


def test_scan_rejects_a_missing_or_empty_front_end(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        scan_nextjs_routes(tmp_path / "nope")
    with pytest.raises(ValueError, match="no app/ or pages/"):
        scan_nextjs_routes(tmp_path)


def test_load_resolves_repo_relative_to_the_yaml() -> None:
    source = load_product_source(FIXTURES / "product.yaml")
    assert source.repo == APP.resolve()
    assert source.demo_login is not None
    assert source.demo_login.password_env == "FIXTURE_DEMO_PASSWORD"
    routes = product_routes(source)
    assert "/playground" in [r.path for r in routes]
    pages = frontend_pages(source, routes)
    assert len(pages) == len(routes)
    assert pages[0]["url"] == "repo:src/app/page.tsx" and "route /" in pages[0]["text"]


def test_routes_override_skips_the_scan(tmp_path: Path) -> None:
    doc = {
        "id": "x",
        "repo": str(tmp_path / "missing"),
        "run": {"url": "http://localhost:3000"},
        "routes": [{"path": "/only", "file": "app/only/page.tsx"}],
    }
    (tmp_path / "product.yaml").write_text(yaml.safe_dump(doc))
    source = load_product_source(tmp_path / "product.yaml")
    assert [r.path for r in product_routes(source)] == ["/only"]


@pytest.mark.parametrize(
    "login",
    [
        {"username_env": "DEMO_EMAIL", "password": "hunter2"},
        {"username_env": "DEMO_EMAIL", "password_env": "hunter2 is not a name"},
    ],
)
def test_demo_login_takes_env_var_names_only(login: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        ProductSource.model_validate(
            {"id": "x", "repo": "/r", "run": {"command": "npm run dev"}, "demo_login": login}
        )


def test_run_needs_a_command_or_url() -> None:
    with pytest.raises(ValueError, match="command or a url"):
        ProductSource.model_validate({"id": "x", "repo": "/r", "run": {}})


def test_alterlab_product_source_has_no_secrets() -> None:
    raw = yaml.safe_load(ALTERLAB.read_text())
    source = ProductSource.model_validate({**raw, "repo": str(Path(raw["repo"]).expanduser())})
    assert source.id == "alterlab" and source.framework == "nextjs"
    assert source.run.command == "npm run dev" and source.seed_command
    assert source.demo_login is not None
    assert source.demo_login.username_env == "ALTERLAB_DEMO_EMAIL"
    assert source.demo_login.password_env == "ALTERLAB_DEMO_PASSWORD"
    assert raw["repo"] == "~/Projects/AlterLab/web"
