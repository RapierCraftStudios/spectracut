"""The style-frame gate Critic: blank or off-description frames fail (#281)."""

from __future__ import annotations

from pathlib import Path

from pydantic import JsonValue

from spectracut.critique.critic import CRITIC_INPUT_TOKENS, CRITIC_REQUESTS
from spectracut.critique.scene_verifier import BLANK_STDDEV, is_blank, style_frames_critic
from spectracut.providers.base import VisionScore
from spectracut.providers.mock_image import solid_png
from spectracut.qa.common import encode_png
from spectracut.storyboard.explore import record_image
from spectracut.storyboard.models import ArtDirection
from spectracut.studio.gates import CriticVerdict, Severity
from spectracut.studio.node import NodeSpec
from spectracut.studio.profiles import load_profile

ART = ArtDirection.model_validate(
    {
        "mood_board": [{"ref": "glass product macro", "take": "rim-lit edges"}],
        "palette": {"swatches": {"ink": "#101820", "beam": "#00c2a8"}},
        "look": {"id": "clean"},
    }
)
SPEC = NodeSpec(id="style_frames", kind="style_frames", department="style-frame-artist")
PROFILE = load_profile("standard")


class Judge:
    """Scores by frame file name; records every call."""

    name = "judge"

    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores
        self.calls: list[tuple[Path, str]] = []
        self.last_usage: dict[str, int] = {}

    def score(self, frame_path: Path, rubric: str) -> VisionScore:
        self.calls.append((frame_path, rubric))
        self.last_usage = {"input_tokens": 10, "output_tokens": 2}
        return VisionScore(score=self.scores.get(frame_path.stem, 0.0), notes="no product")


def _busy(path: Path, size: int = 16) -> Path:
    pixels = bytes(255 if (x // 4 + y // 4) % 2 else 0 for y in range(size) for x in range(size))
    path.write_bytes(encode_png(size, size, 1, pixels))
    return path


def _blank(path: Path) -> Path:
    path.write_bytes(solid_png(16, 16, (30, 30, 30)))
    return path


def _frames(*renders: tuple[str, Path | None]) -> JsonValue:
    return {
        "frames": [
            {
                "id": fid,
                "description": f"the {fid} frame: glass slabs and one beam",
                "prompt": "glass slabs",
                **(
                    {"render": record_image(path, "blender-farm").model_dump(mode="json")}
                    if path is not None
                    else {}
                ),
                **({"tier": "final"} if path is not None else {}),
            }
            for fid, path in renders
        ]
    }


def test_is_blank_measures_luma_spread(tmp_path: Path) -> None:
    assert is_blank(_blank(tmp_path / "flat.png"))
    assert not is_blank(_busy(tmp_path / "busy.png"))
    assert is_blank(tmp_path / "missing.png")
    (tmp_path / "frame.exr").write_bytes(b"not a png")
    assert not is_blank(tmp_path / "frame.exr")  # left to the judge
    assert 0 < BLANK_STDDEV < 0.1


def test_blank_frame_fails_the_gate_without_a_judge_call(tmp_path: Path) -> None:
    """Regression: a blank render never passes, however the judge would score it."""
    judge = Judge({"hero": 1.0, "cta": 1.0})
    critic = style_frames_critic(judge, profile=PROFILE, art_for=lambda _s: ART)
    out = _frames(("hero", _blank(tmp_path / "hero.png")), ("cta", _busy(tmp_path / "cta.png")))
    verdict = critic(SPEC, out)
    assert isinstance(verdict, CriticVerdict)
    assert not verdict.passed
    assert [f.key for f in verdict.findings] == ["style-frame-blank"]
    assert verdict.findings[0].severity is Severity.HIGH and "'hero'" in verdict.findings[0].text
    assert [p.stem for p, _ in judge.calls] == ["cta"]


def test_frame_scored_below_pass_score_against_its_description_fails(tmp_path: Path) -> None:
    judge = Judge({"hero": 0.95, "cta": 0.2})
    critic = style_frames_critic(judge, profile=PROFILE, art_for=lambda _s: ART)
    out = _frames(("hero", _busy(tmp_path / "hero.png")), ("cta", _busy(tmp_path / "cta.png")))
    verdict = critic(SPEC, out)
    assert not verdict.passed and verdict.scores == {"overall": 0.2}
    assert [f.key for f in verdict.findings] == ["style-frame-off"]
    assert "no product" in verdict.findings[0].text
    # Each frame is scored against its own description, with the palette.
    assert "the cta frame" in judge.calls[1][1] and "#00c2a8" in judge.calls[1][1]
    assert verdict.usage[CRITIC_REQUESTS] == 2 and verdict.usage[CRITIC_INPUT_TOKENS] == 20


def test_frames_that_depict_their_description_pass(tmp_path: Path) -> None:
    judge = Judge({"hero": 0.9, "cta": 0.8})
    critic = style_frames_critic(lambda: judge, profile=PROFILE, art_for=lambda _s: ART)
    out = _frames(("hero", _busy(tmp_path / "hero.png")), ("cta", _busy(tmp_path / "cta.png")))
    verdict = critic(SPEC, out)
    assert verdict.passed and verdict.scores == {"overall": 0.8} and not verdict.findings


def test_unrendered_or_invalid_frames_fail_and_other_kinds_go_to_inner(tmp_path: Path) -> None:
    judge = Judge({})
    critic = style_frames_critic(judge, profile=PROFILE, art_for=lambda _s: ART)
    unrendered = critic(SPEC, _frames(("hero", None)))
    assert not unrendered.passed and unrendered.findings[0].key == "style-frame-unrendered"
    assert not critic(SPEC, {"frames": []}).passed
    no_art = style_frames_critic(judge, profile=PROFILE, art_for=lambda _s: None)
    assert not no_art(SPEC, _frames(("hero", _busy(tmp_path / "hero.png")))).passed
    assert judge.calls == []
    other = NodeSpec(id="script", kind="script", department="writer")
    assert critic(other, {"x": 1}).passed
    seen: list[str] = []

    def inner(spec: NodeSpec, _out: JsonValue) -> CriticVerdict:
        seen.append(spec.id)
        return CriticVerdict(passed=False)

    wrapped = style_frames_critic(judge, profile=PROFILE, art_for=lambda _s: ART, inner=inner)
    assert not wrapped(other, {"x": 1}).passed and seen == ["script"]
