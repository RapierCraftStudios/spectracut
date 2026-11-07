# SPDX-License-Identifier: AGPL-3.0-or-later
"""Screen states (#363): live captures composited into notch-aware device chrome."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest

from spectracut.qa.common import encode_png
from spectracut.queue.executors.base import JobResult
from spectracut.queue.queue import Job
from spectracut.studio.departments.capture import CaptureProvenance, ScreenCapture
from spectracut.studio.departments.screens import (
    CHROME_PAGES,
    CHROME_SIZES,
    CaptureFrame,
    Chrome,
    ScreenState,
    check_state,
    compose,
    compose_spec,
    frame_at,
)

_PNGS: dict[tuple[int, int, bool], bytes] = {}


def _png(width: int, height: int, band_white: bool = False) -> bytes:
    key = (width, height, band_white)
    if key not in _PNGS:
        rows = bytearray(width * height * 3)
        if band_white:
            rows[: width * 3] = b"\x01" * (width * 3)  # one lit row at the very top
        _PNGS[key] = encode_png(width, height, 3, bytes(rows))
    return _PNGS[key]


class FakeExecutor:
    """Writes one PNG per ``wait.frames`` at the spec's device-pixel size."""

    def __init__(self, band_white: bool = False) -> None:
        self.band_white = band_white
        self.specs: list[dict[str, Any]] = []

    def run(self, job: Job) -> JobResult:
        spec: Any = job.payload["spec"]
        self.specs.append(spec)
        out = Path(str(job.payload["out_dir"]))
        (out / "frames").mkdir(parents=True, exist_ok=True)
        view, scale = spec["viewport"], spec["deviceScaleFactor"]
        size = (round(view["width"] * scale), round(view["height"] * scale))
        frames = []
        for i in range(spec["script"][0]["frames"]):
            path = out / "frames" / f"{i:06d}.png"
            path.write_bytes(_png(*size, band_white=self.band_white))
            frames.append(str(path))
        return JobResult(output={}, frames=frames)


def _compose(ex: FakeExecutor, raw: ScreenCapture, chrome: str, out: Path) -> ScreenState:
    return compose(cast("Any", ex), raw, cast("Chrome", chrome), out)


def _raw(tmp_path: Path, texts: list[Any] | None = None, device: str = "laptop") -> ScreenCapture:
    frames = []
    for i in range(4):
        path = tmp_path / f"raw{i}.png"
        path.write_bytes(f"frame {i}".encode())
        frames.append(
            CaptureFrame(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        )
    text = tmp_path / "text.json"
    text.write_text(
        json.dumps(
            texts
            if texts is not None
            else [
                {"frame": 0, "url": "u", "texts": ["early"], "selectors": {"#result": ["stale"]}},
                {
                    "frame": 3,
                    "url": "u",
                    "texts": ["Success", "184 ms"],
                    "selectors": {"#result": ["Success", "184 ms"], "[data-user-name]": ["Demo"]},
                },
            ]
        )
    )
    return ScreenCapture(
        key="beat:Result",
        beat="Result",
        route="/playground",
        url="https://product.example/playground",
        device=cast("Any", device),
        fps=30,
        clip=frames,
        still=frames[-1],
        manifest="m",
        events="e",
        text=str(text),
        provenance=CaptureProvenance(
            product_id="p",
            url="https://product.example/playground",
            steps=[],
            device=cast("Any", device),
            spec_sha256="a" * 64,
            frames_sha256="b" * 64,
            rig="rig",
            demo_check="passed",
            deterministic=False,
        ),
    )


def test_compose_fullscreen_top_band_black(tmp_path: Path) -> None:
    raw = _raw(tmp_path)
    state = _compose(FakeExecutor(), raw, "fullscreen", tmp_path / "out")
    assert state.chrome == "fullscreen" and state.content_rect.model_dump() == {
        "x": 0,
        "y": 37,
        "width": 1728,
        "height": 1080,
    }
    assert state.raw_still_sha256 == raw.still.sha256 and state.state == "beat:Result"
    # A lit pixel in the 37 px band (74 device px) is refused.
    with pytest.raises(ValueError, match="not pure black"):
        _compose(FakeExecutor(band_white=True), raw, "fullscreen", tmp_path / "bad")
    windowed = _compose(FakeExecutor(band_white=True), raw, "windowed", tmp_path / "win")
    assert windowed.chrome == "windowed"  # the band rule is the full screen's only


@pytest.mark.parametrize(
    ("chrome", "device", "px"),
    [
        ("fullscreen", "laptop", (3456, 2234)),
        ("windowed", "laptop", (3456, 2234)),
        ("iphone", "phone", (1206, 2622)),
    ],
)
def test_compose_sizes_per_chrome(
    tmp_path: Path, chrome: str, device: str, px: tuple[int, int]
) -> None:
    raw = _raw(tmp_path, device=device)
    ex = FakeExecutor()
    state = _compose(ex, raw, chrome, tmp_path / "out")
    spec = ex.specs[0]
    size = CHROME_SIZES[chrome]
    assert (spec["viewport"]["width"], spec["viewport"]["height"]) == (size.width, size.height)
    assert spec["deviceScaleFactor"] == size.scale
    assert spec["script"] == [{"do": "wait", "frames": 4}]
    assert spec["clean"] is False and spec["legibility"] is False
    assert len(state.clip) == 4 and (size.width * size.scale, size.height * size.scale) == px
    query = parse_qs(urlsplit(spec["url"]).query)
    assert spec["url"].startswith(CHROME_PAGES[chrome].resolve().as_uri() + "?")
    assert query["s"] == ["beat:Result"] and query["fps"] == ["30"]
    assert [Path(urlsplit(u).path) for u in json.loads(query["frames"][0])] == [
        Path(f.path).resolve() for f in raw.clip
    ]
    wrong = state.model_copy(
        update={"size": CHROME_SIZES["iphone" if chrome != "iphone" else "windowed"]}
    )
    with pytest.raises(ValueError, match="chrome needs"):
        check_state(wrong)
    assert compose_spec(raw, cast("Chrome", chrome))["url"] == spec["url"]


def test_frame_at_maps_t(tmp_path: Path) -> None:
    state = _compose(FakeExecutor(), _raw(tmp_path), "windowed", tmp_path / "out")
    n = len(state.clip)
    assert frame_at(state, 0.0) == state.clip[0]
    assert frame_at(state, 0.26) == state.clip[1]
    assert frame_at(state, 0.99) == state.clip[n - 1] and frame_at(state, 1.0) == state.clip[n - 1]
    for bad in (-0.01, 1.01, float("nan")):
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            frame_at(state, bad)


def _html(name: str) -> str:
    return CHROME_PAGES[name].read_text(encoding="utf-8")


def test_chrome_pages_have_notch_and_ios_parts() -> None:
    full, win, phone = _html("fullscreen"), _html("windowed"), _html("iphone")
    assert "1728px" in full and "1117px" in full and "37px" in full and "#000" in full
    assert re.search(r"#band\s*{[^}]*height:\s*37px[^}]*background:\s*#000", full)
    assert re.search(r"#frame\s*{[^}]*top:\s*37px[^}]*height:\s*1080px", full)
    assert "menu" not in full.lower() and "toolbar" not in full.lower()
    # windowed: the notch (centre 200 px) is clear of menu items; dock, shadow and radius.
    assert re.search(r"#notch-clear\s*{[^}]*left:\s*764px[^}]*width:\s*200px", win)
    assert re.search(r"#menubar \.left\s*{[^}]*width:\s*700px", win)  # ends at 718 < 764
    assert "#dock" in win and "box-shadow" in win and "border-radius: 12px" in win
    assert "traffic" in win.lower() or 'class="light r"' in win
    assert "urlpill" in win
    # iphone: status bar, island clearance, URL pill, home indicator.
    assert "402px" in phone and "874px" in phone
    assert re.search(r"#statusbar\s*{[^}]*height:\s*54px", phone)
    assert re.search(r"#island\s*{[^}]*width:\s*126px[^}]*height:\s*37px", phone)
    assert "9:41" in phone and "#urlpill" in phone
    assert re.search(r"#home\s*{[^}]*width:\s*134px[^}]*height:\s*5px", phone)
    for page in (full, win, phone):
        assert "frames" in page and "fps" in page
        assert 'q.get("t")' in page
        low = page.lower()
        assert "" not in page and "接" not in page
        for mark in ("apple", "macbook", "iphone"):
            assert mark not in low


def test_values_come_from_recorded_text_json(tmp_path: Path) -> None:
    state = _compose(FakeExecutor(), _raw(tmp_path), "fullscreen", tmp_path / "out")
    # The final record only: strings under the anchor and the product text selectors.
    assert state.values == ["Success", "184 ms", "Demo"]
    empty = _compose(
        FakeExecutor(),
        _raw(tmp_path, texts=[]),
        "iphone",
        tmp_path / "o2",
    )
    assert empty.values == []
    assert isinstance(state, ScreenState)
