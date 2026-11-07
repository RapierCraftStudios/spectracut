# SPDX-License-Identifier: AGPL-3.0-or-later
"""UI capture department: screen map -> capture specs -> checked, deterministic captures.

The unit tests drive :class:`CaptureRunner` with a fake capture CLI behind the real
:class:`CaptureExecutor`. The e2e tests (``SPECTRACUT_E2E=1``, built
``packages/capture``) serve the static fixture app in ``fixtures/capture-app`` as
the product's dev server and capture it in real headless Chromium.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from spectracut.qa.common import encode_png
from spectracut.queue.executors.capture import CaptureExecutor, CaptureRun
from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.spec.product import DemoData, DemoLogin, ProductRun, ProductSource
from spectracut.studio.department import DepartmentOutputError
from spectracut.studio.departments import STAGE_DEPARTMENTS, all_departments
from spectracut.studio.departments.capture import (
    DEVICES,
    UI_CAPTURE,
    CaptureRunner,
    UICaptureDepartment,
    UICaptures,
    capture_node,
    check_demo_data,
    steps_to_actions,
)
from spectracut.studio.departments.discovery import PRODUCT, ScreenStep
from spectracut.studio.graph import RunContext, RunResult
from spectracut.studio.node import NodeSpec, Tier
from spectracut.studio.producer import Producer, product_capture_nodes
from spectracut.studio.profiles import load_profile

FIXTURE_APP = Path(__file__).resolve().parent / "fixtures" / "capture-app"
REPO_ROOT = Path(__file__).resolve().parents[2]
CLAIM = "Upgrades take one click"
EMAIL_ENV = "FIXTURE_DEMO_EMAIL"
PASSWORD_ENV = "FIXTURE_DEMO_PASSWORD"
SECRET = "pw-not-in-any-file"


def _source(url: str = "http://127.0.0.1:9", command: str = "") -> ProductSource:
    return ProductSource(
        id="fixture-app",
        repo=FIXTURE_APP,
        run=ProductRun(command=command, url=url),
        demo_login=DemoLogin(
            username_env=EMAIL_ENV, password_env=PASSWORD_ENV, route="/login.html"
        ),
        demo_data=DemoData(
            emails=["demo@fixture.test"], names=["Demo Person"], name_selectors=["[data-user-name]"]
        ),
    )


def _notes(route: str = "/index.html") -> dict[str, Any]:
    return {
        "summary": "Fixture app with billing.",
        "sources": [{"id": "docs", "title": "Docs", "ref": "https://example.test/docs"}],
        "features": [{"text": "One-click upgrades", "source_id": "docs"}],
        "screens": [
            {
                "proof_point": CLAIM,
                "route": route,
                "steps": [
                    {"action": "visit", "target": route},
                    {"action": "click", "target": "#upgrade"},
                    {"action": "scroll", "target": "#list", "value": "600"},
                    {"action": "wait", "target": "200ms"},
                ],
            },
            {"beat": "Outro", "interlude": True},
        ],
    }


def _leak_notes() -> dict[str, Any]:
    notes = _notes()
    notes["screens"][0]["route"] = "/leak.html"
    notes["screens"][0]["steps"] = [
        {"action": "visit", "target": "/leak.html"},
        {"action": "wait", "target": "200ms"},
    ]
    return notes


def _ctx(node: NodeSpec, notes: dict[str, Any]) -> RunContext:
    return RunContext(spec=node, tier=Tier.PREVIEW, key="k", inputs={PRODUCT: notes}, attempt=1)


_PNGS: dict[tuple[int, int], bytes] = {}


def _black_png(spec: dict[str, Any]) -> bytes:
    """A black PNG of the chrome page's device-pixel size."""
    scale = spec["deviceScaleFactor"]
    size = (round(spec["viewport"]["width"] * scale), round(spec["viewport"]["height"] * scale))
    if size not in _PNGS:
        _PNGS[size] = encode_png(size[0], size[1], 3, bytes(size[0] * size[1] * 3))
    return _PNGS[size]


class FakeCli:
    """Stands in for ``spectracut-capture``: writes frames, manifest and text.json."""

    def __init__(self, texts: Sequence[str] = ("Demo", "demo@fixture.test"), drift: bool = False):
        self.texts = list(texts)
        self.drift = drift
        self.specs: list[dict[str, Any]] = []
        self.raw: list[str] = []

    def __call__(self, argv: Sequence[str], timeout: float | None) -> CaptureRun:
        path = Path(argv[-1])
        self.raw.append(path.read_text(encoding="utf-8"))
        spec = json.loads(self.raw[-1])
        self.specs.append(spec)
        out = Path(spec["outDir"])
        (out / "frames").mkdir(parents=True, exist_ok=True)
        salt = str(len(self.specs)) if self.drift else ""
        chrome = spec["url"].startswith("file://")
        frames = []
        for i in range(spec["script"][0]["frames"] if chrome else 3):
            data = _black_png(spec) if chrome else f"{spec['url']}|{i}|{salt}".encode()
            (out / "frames" / f"{i:06d}.png").write_bytes(data)
            frames.append({"index": i})
        (out / "manifest.json").write_text(json.dumps({"fps": spec["fps"], "frames": frames}))
        (out / "events.json").write_text("[]")
        record = {
            "frame": 0,
            "url": spec["url"],
            "texts": self.texts,
            "selectors": {"[data-user-name]": ["Demo Person"]},
        }
        (out / "text.json").write_text(json.dumps([record]))
        return CaptureRun(0, "{}", "")


def _runner(tmp_path: Path, cli: FakeCli, **kw: Any) -> CaptureRunner:
    def inner(ctx: RunContext) -> RunResult:
        raise AssertionError("only ui_capture nodes reach this runner")

    return CaptureRunner(
        inner,
        all_departments(),
        workdir=tmp_path / "capture",
        executor_factory=lambda cmd: CaptureExecutor(command=cmd, runner=cli),
        command=["spectracut-capture"],
        env={EMAIL_ENV: "demo@fixture.test", PASSWORD_ENV: SECRET},
        probe=lambda url: True,
        commit=lambda repo: "",
        **kw,
    )


# ---- registry and node ---------------------------------------------------------


def test_ui_capture_is_a_registered_department() -> None:
    assert STAGE_DEPARTMENTS[UI_CAPTURE] == "ui-capture"
    assert all_departments()["ui-capture"] is UICaptureDepartment
    assert UICaptureDepartment.produces == frozenset({UI_CAPTURE})


def test_devices_match_the_kit_macbook_and_iphone_screens() -> None:
    # #363: 1234x771 at 2.8x fills 1728x1080 under the notch; iPhone 17 Pro 402x874 at 3x.
    laptop, phone = DEVICES["laptop"], DEVICES["phone"]
    assert (laptop.width, laptop.height, laptop.device_scale_factor) == (1234, 771, 2.8)
    assert round(laptop.width * 2.8) == 3455 and round(laptop.height * 2.8) == 2159
    assert (phone.width, phone.height, phone.device_scale_factor) == (402, 874, 3)
    assert phone.height / phone.width == pytest.approx(19.5 / 9, abs=0.01)


def test_capture_node_reads_the_screen_map_and_holds_no_secret() -> None:
    node = capture_node(_source(), PRODUCT)
    assert (node.id, node.kind, node.department, node.inputs) == (
        UI_CAPTURE,
        UI_CAPTURE,
        "ui-capture",
        (PRODUCT,),
    )
    assert node.params["devices"] == ["laptop", "phone"]
    assert SECRET not in json.dumps(node.params)
    with pytest.raises(ValueError, match="devices"):
        capture_node(_source(), devices=("tablet",))


# ---- screen steps -> DSL ---------------------------------------------------------


def test_steps_become_eased_capture_actions_ending_with_a_hold() -> None:
    steps = [
        ScreenStep(action="visit", target="/billing"),
        ScreenStep(action="hover", target="Usage chart"),
        ScreenStep(action="click", target="#upgrade"),
        ScreenStep(action="type", target="input[name=q]", value="max_credits"),
        ScreenStep(action="scroll", target="#list", value="800 px/s"),
        ScreenStep(action="wait", target="1.5s"),
    ]
    actions = steps_to_actions(steps, "http://localhost:3000/")
    assert actions[0] == {"do": "goto", "url": "http://localhost:3000/billing"}
    assert actions[1]["to"] == {"selector": "text=Usage chart"}
    assert actions[1]["easing"] == "easeInOutCubic" and actions[1]["durationMs"] == 600
    assert actions[2]["to"] == {"selector": "#upgrade"} and actions[3] == {"do": "click"}
    assert actions[6] == {"do": "type", "text": "max_credits", "bpm": 120, "subdivision": 4}
    assert actions[7] == {"do": "scroll", "velocity": 800.0, "at": {"selector": "#list"}}
    assert actions[8] == {"do": "wait", "ms": 1500}
    assert actions[-1] == {"do": "wait", "ms": 500}
    with pytest.raises(ValueError, match=r"\[param\]"):
        steps_to_actions(
            [ScreenStep(action="visit", target="/integrations/[platform]")], "http://x"
        )


# ---- demo-data allow-list ----------------------------------------------------------


def test_demo_check_flags_emails_and_names_off_the_allow_list() -> None:
    allow = DemoData(emails=["demo@fixture.test"], names=["Demo Person"], name_selectors=["[n]"])
    ok = [{"texts": ["Signed in as demo@fixture.test"], "selectors": {"[n]": ["Demo Person"]}}]
    assert check_demo_data(ok, allow) == []
    assert check_demo_data([{"texts": ["me@login.test"]}], allow, "me@login.test") == []
    bad = [{"texts": ["Contact jane@customer.example"], "selectors": {"[n]": ["Jane Roe"]}}]
    problems = check_demo_data(bad, allow)
    assert len(problems) == 2
    assert "customer.example" in problems[0] and "jane" not in problems[0]
    assert "Jane Roe" in problems[1]


# ---- runner ---------------------------------------------------------------------


def test_runner_captures_every_mapped_screen_per_device_with_provenance(tmp_path: Path) -> None:
    cli = FakeCli()
    result = _runner(tmp_path, cli)(_ctx(capture_node(_source(), PRODUCT), _notes()))
    out = UICaptures.model_validate(result.output)
    assert sorted((c.key, c.device) for c in out.captures) == [
        (f"proof:{CLAIM}", "laptop"),
        (f"proof:{CLAIM}", "phone"),
    ]
    laptop = next(c for c in out.captures if c.device == "laptop")
    assert len(laptop.clip) == 3 and laptop.still == laptop.clip[-1]
    assert laptop.url == "http://127.0.0.1:9/index.html"
    prov = laptop.provenance
    assert prov.product_id == "fixture-app" and prov.demo_check == "passed"
    assert prov.deterministic is True and prov.rig.startswith("@spectracut/capture@")
    hashes = "\n".join(f.sha256 for f in laptop.clip)
    assert prov.frames_sha256 == hashlib.sha256(hashes.encode()).hexdigest()
    # Each capture ran twice (determinism) and logs in through env-var names only.
    assert len([sp for sp in cli.specs if not sp["url"].startswith("file://")]) == 4
    spec = cli.specs[0]
    assert spec["viewport"] == {"width": 1234, "height": 771} and spec["deviceScaleFactor"] == 2.8
    assert spec["colorScheme"] == "dark"
    typed = [a for a in spec["setup"] if a["do"] == "type"]
    assert [a["textEnv"] for a in typed] == [EMAIL_ENV, PASSWORD_ENV]
    assert spec["textSelectors"] == ["[data-user-name]"]
    assert all(SECRET not in raw for raw in cli.raw)
    assert SECRET not in json.dumps(result.output)


def test_runner_fails_when_a_capture_shows_non_demo_data(tmp_path: Path) -> None:
    cli = FakeCli(texts=["Contact: jane@customer.example"])
    with pytest.raises(DepartmentOutputError, match="customer.example") as err:
        _runner(tmp_path, cli)(_ctx(capture_node(_source(), PRODUCT), _notes()))
    assert "jane@" not in str(err.value)


def test_runner_fails_when_a_second_capture_differs(tmp_path: Path) -> None:
    with pytest.raises(DepartmentOutputError, match="different frames"):
        _runner(tmp_path, FakeCli(drift=True))(_ctx(capture_node(_source(), PRODUCT), _notes()))


def test_runner_starts_and_always_stops_the_dev_server(tmp_path: Path) -> None:
    started: list[tuple[list[str], Path]] = []
    seeded: list[list[str]] = []

    class Proc:
        pid = -1
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def send_signal(self, sig: int) -> None:
            self.returncode = -sig

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode or 0

    proc = Proc()

    def launch(argv: list[str], cwd: Path) -> Any:
        started.append((argv, cwd))
        return proc

    def seed(argv: list[str], cwd: Path) -> int:
        seeded.append(argv)
        return 0

    source = _source(command="npm run dev -- --port 4321").model_copy(
        update={"seed_command": "python seed.py --demo"}
    )
    runner = _runner(tmp_path, FakeCli(texts=["jane@customer.example"]), launcher=launch)
    runner.run_command = seed
    with pytest.raises(DepartmentOutputError):
        runner(_ctx(capture_node(source, PRODUCT), _notes()))
    assert seeded == [["python", "seed.py", "--demo"]]
    assert started == [(["npm", "run", "dev", "--", "--port", "4321"], FIXTURE_APP)]
    assert proc.returncode is not None  # stopped even though the capture failed


def test_check_output_requires_every_mapped_screen_on_every_device() -> None:
    notes = _notes()
    empty = {"product_id": "fixture-app", "devices": ["laptop"], "captures": []}
    with pytest.raises(DepartmentOutputError, match="no laptop capture"):
        UICaptureDepartment.check_output(UI_CAPTURE, empty, {PRODUCT: notes}, {PRODUCT: PRODUCT})
    with pytest.raises(DepartmentOutputError, match="phone"):
        UICaptureDepartment.check_params(UI_CAPTURE, empty, {"devices": ["laptop", "phone"]})


# ---- producer and studio new ------------------------------------------------------


def _brief() -> Brief:
    return Brief(
        id="fixture-film",
        product="Fixture",
        audience=AudienceProfile(personas=[Persona(name="Dev", description="ships often")]),
        sources=[ClaimSource(id="docs", title="Docs", ref="https://example.test/docs")],
        proof_points=[ProofPoint(claim=CLAIM, source_id="docs")],
        cta="Try it",
    )


def test_product_ui_asset_becomes_the_capture_node_feeding_shots() -> None:
    profile = load_profile("standard")
    producer = Producer(all_departments())
    plain = producer.breakdown(_brief(), profile, duration_s=60)
    ui = next(a for a in plain.assets if a.id == "product-ui")
    assert ui.node is None and "node" not in ui.model_dump()
    with_product = producer.breakdown(_brief(), profile, duration_s=60, product=_source())
    ui = next(a for a in with_product.assets if a.id == "product-ui")
    assert (ui.node, ui.kind, ui.source) == (UI_CAPTURE, UI_CAPTURE, "product:fixture-app")
    nodes = product_capture_nodes(with_product.nodes, _source(), PRODUCT)
    assert nodes[0].id == UI_CAPTURE
    # Both shot nodes read the captures: the plan and preview renders put them on devices.
    assert sorted(s.kind for s in nodes[1:]) == ["shots", "template_shots"]
    assert all(UI_CAPTURE in s.inputs for s in nodes[1:])


def test_studio_new_with_product_plans_the_capture_node(tmp_path: Path) -> None:
    from spectracut.cli.studio import create_production
    from spectracut.studio.store import ProductionStore

    fixture = Path(__file__).resolve().parent / "fixtures" / "product.yaml"
    store = ProductionStore(tmp_path / "home")
    _, graph = create_production(store, _brief(), profile="standard", product=fixture)
    order = graph.order()
    assert order.index(PRODUCT) < order.index(UI_CAPTURE) < order.index("shots")
    assert UI_CAPTURE in graph.spec("shots").inputs
    assert UI_CAPTURE in graph.spec("template_shots").inputs
    # The storyboard checks its device boards against the product's screen map.
    assert PRODUCT in graph.spec("storyboard").inputs
    assert order.index(PRODUCT) < order.index("storyboard")
    _, plain = create_production(store, _brief(), production_id="plain", profile="standard")
    assert UI_CAPTURE not in plain.order()


# ---- e2e: the fixture app in real Chromium ---------------------------------------

E2E = pytest.mark.skipif(os.environ.get("SPECTRACUT_E2E") != "1", reason="set SPECTRACUT_E2E=1")
CLI = REPO_ROOT / "packages" / "capture" / "dist" / "cli.js"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _e2e_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[CaptureRunner, ProductSource]:
    if not CLI.is_file() or shutil.which("node") is None:
        pytest.fail("build packages/capture first: npm run build -w @spectracut/capture")
    monkeypatch.setenv(EMAIL_ENV, "demo@fixture.test")
    monkeypatch.setenv(PASSWORD_ENV, SECRET)
    port = _free_port()
    command = f"{sys.executable} -m http.server {port} --bind 127.0.0.1"

    def inner(ctx: RunContext) -> RunResult:
        raise AssertionError("unexpected node")

    runner = CaptureRunner(
        inner, all_departments(), workdir=tmp_path / "capture", command=["node", str(CLI)]
    )
    return runner, _source(url=f"http://127.0.0.1:{port}", command=command)


@pytest.mark.e2e
@E2E
def test_e2e_fixture_app_captures_are_deterministic_and_demo_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, source = _e2e_runner(tmp_path, monkeypatch)
    node = capture_node(source, PRODUCT, fps=10)
    out = UICaptures.model_validate(runner(_ctx(node, _notes())).output)
    assert {c.device for c in out.captures} == {"laptop", "phone"}
    for cap in out.captures:
        assert cap.provenance.deterministic is True
        assert Path(cap.still.path).is_file() and len(cap.clip) > 1
        assert cap.layers, "the main selector is captured as a layer"
        records = json.loads(Path(cap.text).read_text())
        texts = [t for r in records for t in r["texts"]]
        assert "demo@fixture.test" in " ".join(texts) and "Plan upgraded" in texts
        assert {"[data-user-name]": ["Demo Person"]} == records[-1]["selectors"]
        spec = (Path(cap.manifest).parent / "spec.json").read_text()
        assert SECRET not in spec and PASSWORD_ENV in spec
    laptop = next(c for c in out.captures if c.device == "laptop")
    assert laptop.still.path.endswith(".png")
    assert Path(laptop.still.path).read_bytes()[16:24] == (3024).to_bytes(4, "big") + (
        1964
    ).to_bytes(4, "big")


@pytest.mark.e2e
@E2E
def test_e2e_capture_fails_on_a_page_with_customer_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, source = _e2e_runner(tmp_path, monkeypatch)
    node = capture_node(source, PRODUCT, devices=("laptop",), fps=10, verify_determinism=False)
    with pytest.raises(DepartmentOutputError, match="customer.example") as err:
        runner(_ctx(node, _leak_notes()))
    assert "jane@" not in str(err.value)


# ---- #363: promo handling, reveal, cache, screen states ------------------------------


def _raw_specs(cli: FakeCli) -> list[dict[str, Any]]:
    return [sp for sp in cli.specs if not sp["url"].startswith("file://")]


def _anchored(anchor: str = "#result", chrome: str = "fullscreen") -> dict[str, Any]:
    notes = _notes()
    notes["screens"][0].update({"anchor": anchor, "chrome": chrome})
    return notes


def _run(tmp_path: Path, cli: FakeCli, source: ProductSource, notes: dict[str, Any]) -> Any:
    return _runner(tmp_path, cli)(_ctx(capture_node(source, PRODUCT), notes))


def test_spec_dismisses_promo_and_hides_banners(tmp_path: Path) -> None:
    source = _source().model_copy(update={"dismiss": ["#promo .close"], "hide": [".promo-nav"]})
    cli = FakeCli()
    _run(tmp_path, cli, source, _notes())
    spec = _raw_specs(cli)[0]
    assert spec["clean"] == {
        "cookieBanners": True,
        "bannerSelectors": [".promo-nav"],
        "secretSelectors": ["input[type=password]"],
    }
    setup = spec["setup"]
    # The promo's own close button is clicked after login, unrecorded (setup).
    # optional: a no-op when the promo is absent.
    assert setup[-1] == {"do": "click", "at": {"selector": "#promo .close"}, "optional": True}
    assert [a["textEnv"] for a in setup if a["do"] == "type"] == [EMAIL_ENV, PASSWORD_ENV]
    assert "#promo .close" not in json.dumps(spec["script"])


def test_spec_reveals_anchor_and_gates_legibility(tmp_path: Path) -> None:
    cli = FakeCli()
    _run(tmp_path, cli, _source(), _anchored())
    spec = _raw_specs(cli)[0]
    assert spec["script"][-2:] == [
        {"do": "reveal", "selector": "#result"},
        {"do": "wait", "ms": 500},
    ]
    assert spec["legibility"] == {
        "screenHeightPx": 1080,
        "checks": [
            {"selector": "#result", "minTextHeightPx": 20},
            {"selector": "#result pre, #result code", "minTextHeightPx": 21},
        ],
    }
    plain = FakeCli()
    _run(tmp_path / "plain", plain, _source(), _notes())
    assert _raw_specs(plain)[0]["legibility"] is False


def test_custom_extraction_rejection_fails_node(tmp_path: Path) -> None:
    cli = FakeCli(texts=["Error: Demo does not support custom extraction"])
    with pytest.raises(DepartmentOutputError, match="custom extraction") as err:
        _run(tmp_path, cli, _source(), _notes())
    assert "demo_login" in str(err.value)


def test_run_cache_reuses_frames_without_executor(tmp_path: Path) -> None:
    first = FakeCli()
    _run(tmp_path, first, _source(), _notes())
    assert len(_raw_specs(first)) == 4
    again = FakeCli()
    result = _run(tmp_path, again, _source(), _notes())
    assert _raw_specs(again) == []  # no product run
    assert UICaptures.model_validate(result.output).captures[0].provenance.deterministic is True
    # A tampered frame no longer verifies, so the capture runs again.
    run_dir = next(d for d in (tmp_path / "capture" / "runs").iterdir() if len(d.name) == 64)
    (run_dir / "frames" / "000000.png").write_bytes(b"tampered")
    third = FakeCli()
    _run(tmp_path, third, _source(), _notes())
    assert len(_raw_specs(third)) >= 1


def test_max_runs_budget_skips_verify_and_refuses_overrun(tmp_path: Path) -> None:
    source = _source()
    source = source.model_copy(update={"run": source.run.model_copy(update={"max_runs": 2})})
    cli = FakeCli()
    result = _run(tmp_path, cli, source, _notes())
    assert len(_raw_specs(cli)) == 2  # one per device, no determinism re-capture
    caps = UICaptures.model_validate(result.output).captures
    assert all(c.provenance.deterministic is False for c in caps)
    tight = source.model_copy(update={"run": source.run.model_copy(update={"max_runs": 1})})
    refused = FakeCli()
    with pytest.raises(DepartmentOutputError, match="max_runs"):
        _run(tmp_path / "other", refused, tight, _notes())
    assert refused.specs == []  # failed before any capture


def test_capture_records_screen_state(tmp_path: Path) -> None:
    result = _run(tmp_path, FakeCli(), _source(), _anchored(chrome="windowed"))
    caps = UICaptures.model_validate(result.output).captures
    laptop = next(c for c in caps if c.device == "laptop")
    phone = next(c for c in caps if c.device == "phone")
    assert laptop.screen is not None and phone.screen is not None
    assert (laptop.screen.chrome, phone.screen.chrome) == ("windowed", "iphone")
    assert (
        laptop.screen.state == laptop.key and laptop.screen.raw_still_sha256 == laptop.still.sha256
    )
    assert (phone.screen.size.width, phone.screen.size.height) == (402, 874)
    assert laptop.screen.values == ["Demo Person"]
    assert "screen" in laptop.model_dump(mode="json")
    assert "screen" not in laptop.model_copy(update={"screen": None}).model_dump(mode="json")
