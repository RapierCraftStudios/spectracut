# SPDX-License-Identifier: AGPL-3.0-or-later
"""Motion typography (#56): presets, layout in safe areas, copy gate, fonts, reveal events, shimmer.

The real renders of every preset in every aspect are ``test_typography_e2e.py``
(post-deploy check ``typography-presets-farm-e2e``).
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from spectracut.audio.recipe import AudioRecipe, Cue, Layer, Provenance, SoundSpec, place_cues
from spectracut.edit.timeline import Edit
from spectracut.finishing.typography import (
    ANIMATIONS,
    KINDS,
    PRESET_ASPECTS,
    SHIMMER_MAX,
    TITLE_SAFE,
    CopyGateError,
    FontFile,
    PixelBox,
    ShimmerError,
    TypeBox,
    TypeCard,
    TypographyError,
    check_no_shimmer,
    copy_gate,
    layout_card,
    load_type_preset,
    load_type_presets,
    reveal_markers,
    reveal_span,
    settled_frames,
    shimmer,
    type_steps,
)
from spectracut.formats.safe_areas import RESOLUTIONS
from spectracut.spec.asset import LicenseRecord
from spectracut.spec.brand import BrandKit, load_brand_kit
from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.spec.spec import Aspect

ROOT = Path(__file__).resolve().parents[2]
PRESETS = ROOT / "templates" / "type_presets"
KIT = ROOT / "brands" / "alterlab" / "brand-kit.yaml"


def _brief() -> Brief:
    return Brief(
        id="alterlab-dashboard",
        product="AlterLab dashboard",
        audience=AudienceProfile(personas=[Persona(name="Ops lead", description="runs a lab")]),
        sources=[ClaimSource(id="site", title="AlterLab", ref="https://alterlab.io")],
        proof_points=[ProofPoint(claim="One view of every run", source_id="site")],
        cta="See the dashboard",
    )


def _kit() -> BrandKit:
    return load_brand_kit(KIT)


PROV = Provenance(source="procedural", origin="ToneGenerator", licence="AGPL-3.0", commercial=True)
UI = {a: TypeBox(x=0.3, y=0.6, w=0.2, h=0.05) for a in PRESET_ASPECTS}


def _cards() -> list[TypeCard]:
    return [
        TypeCard(id="title", preset="title-tracking-3d", text="AlterLab", start_frame=0),
        TypeCard(
            id="line",
            preset="kinetic-stagger-2d",
            text="One view",
            start_frame=24,
            source_id="site",
        ),
        TypeCard(
            id="proof", preset="lower-third-wipe-2d", text="One view of every run.", start_frame=48
        ),
        TypeCard(
            id="ui",
            preset="callout-match-cut-2d",
            text="AlterLab dashboard",
            start_frame=72,
            match_from=UI,
        ),
        TypeCard(id="cta", preset="end-card-wipe-3d", text="See the dashboard", start_frame=96),
    ]


# ---- the library -----------------------------------------------------------------------


def test_every_shipped_preset_loads_and_the_library_covers_kinds_and_animations() -> None:
    presets = load_type_presets(PRESETS)
    assert len(presets) == len(list(PRESETS.glob("*.yaml"))) >= 5
    assert {p.kind for p in presets.values()} == set(KINDS)
    assert {p.animation for p in presets.values()} == set(ANIMATIONS)
    assert {p.dimension for p in presets.values()} == {"2d", "3d"}
    assert all(p.easing for p in presets.values())
    assert any(p.dimension == "3d" and p.extrude > 0 and p.dof for p in presets.values())


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("- a list\n", "must be a YAML mapping"),
        ("id: [unclosed\n", "is not valid YAML"),
        ("id: a\nkind: poster\n", "is invalid"),
        (
            "id: b\nkind: title\ndimension: 2d\nanimation: mask_wipe\nreveal_frames: 4\n"
            "size: 0.1\nlayout: {'16:9': {x: 0.1, y: 0.1, w: 0.5, h: 0.5}}\n",
            "is invalid",
        ),
    ],
)
def test_bad_preset_is_refused_naming_the_file(tmp_path: Path, text: str, needle: str) -> None:
    path = tmp_path / "a.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(TypographyError, match=needle) as err:
        load_type_preset(path)
    assert str(path) in str(err.value)


def test_preset_id_must_match_the_file_and_2d_cannot_extrude(tmp_path: Path) -> None:
    raw = (PRESETS / "kinetic-stagger-2d.yaml").read_text(encoding="utf-8")
    wrong = tmp_path / "other.yaml"
    wrong.write_text(raw, encoding="utf-8")
    with pytest.raises(TypographyError, match="the file must be kinetic-stagger-2d.yaml"):
        load_type_preset(wrong)
    flat = tmp_path / "kinetic-stagger-2d.yaml"
    flat.write_text(raw + "extrude: 0.1\n", encoding="utf-8")
    with pytest.raises(TypographyError, match="2d type cannot extrude"):
        load_type_preset(flat)


# ---- layout ----------------------------------------------------------------------------


@pytest.mark.parametrize("aspect", PRESET_ASPECTS)
def test_every_preset_lays_out_inside_the_safe_area_on_whole_pixels(aspect: Aspect) -> None:
    presets = load_type_presets(PRESETS)
    width, height = RESOLUTIONS[aspect]
    for card in _cards():
        preset = presets[card.preset]
        lay = layout_card(card, preset, aspect)
        box = lay.box
        assert all(isinstance(v, int) for v in box.as_list())
        assert lay.resolution == (width, height)
        assert box.x >= TITLE_SAFE.left * width and box.y >= TITLE_SAFE.top * height
        assert box.x + box.w <= (1 - TITLE_SAFE.right) * width
        assert box.y + box.h <= (1 - TITLE_SAFE.bottom) * height


def test_layout_outside_a_platform_safe_area_or_too_long_raises() -> None:
    presets = load_type_presets(PRESETS)
    card = _cards()[0]
    with pytest.raises(TypographyError, match="outside tiktok area"):
        layout_card(card, presets[card.preset], "9:16", platform="tiktok")
    long = card.model_copy(update={"text": "AlterLab " * 8})
    with pytest.raises(TypographyError, match="does not fit"):
        layout_card(long, presets[card.preset], "9:16")


# ---- copy gate and steps ---------------------------------------------------------------


def test_clean_copy_builds_steps_with_the_kit_font() -> None:
    kit, presets = _kit(), load_type_presets(PRESETS)
    steps = type_steps(_cards(), presets, kit, _brief(), "16:9", start_index=7)
    assert [s.index for s in steps] == list(range(7, 12))
    tools = {s.args["name"]: s.tool for s in steps}
    assert tools["type_title"] == "type.add_3d" and tools["type_line"] == "type.add_2d"
    for step in steps:
        assert step.args["font"] == {
            "family": "Inter",
            "weight": presets[
                next(c.preset for c in _cards() if f"type_{c.id}" == step.args["name"])
            ].weight,
        }
        assert all(isinstance(v, int) for v in step.args["box"])  # type: ignore[union-attr]
    title = next(s for s in steps if s.args["name"] == "type_title")
    assert title.args["dof"] is True and title.args["animation"] == "tracking_reveal"
    ui = next(s for s in steps if s.args["name"] == "type_ui")
    assert ui.args["match_from"] == [576, 648, 384, 54]
    assert ui.args["color"] == [round(0xF8 / 255, 6)] * 3  # kit accent #f8f8f8


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"text": "The revolutionary dashboard"}, "forbidden word 'revolutionary'"),
        ({"text": "Guaranteed uptime", "source_id": "site"}, "forbidden word 'guaranteed'"),
        ({"text": "Ten times faster than anyone"}, "not approved copy"),
        ({"text": "Ten times faster", "source_id": "made-up"}, "not a declared claim source"),
    ],
)
def test_failing_copy_raises_before_any_step_is_built(change: dict[str, str], reason: str) -> None:
    cards = _cards()
    cards[2] = cards[2].model_copy(update=change)
    with pytest.raises(CopyGateError, match=reason) as err:
        type_steps(cards, load_type_presets(PRESETS), _kit(), _brief(), "16:9")
    assert [v.card_id for v in err.value.verdicts] == ["proof"]


def test_copy_gate_whole_word_case_insensitive_and_kit_sources() -> None:
    kit, brief = _kit(), _brief()
    card = TypeCard(
        id="c", preset="p", text="Best-in-Class results", start_frame=0, source_id="alterlab-site"
    )
    assert not copy_gate(card, kit, brief).ok
    fine = card.model_copy(update={"text": "Revolutionaryish results"})
    assert copy_gate(fine, kit, brief).ok
    assert copy_gate(
        card.model_copy(update={"text": "see THE dashboard!", "source_id": None}), kit, brief
    ).ok


def test_font_role_weight_and_licence_come_from_the_kit() -> None:
    kit, brief, presets = _kit(), _brief(), load_type_presets(PRESETS)
    card = [_cards()[0]]
    odd = {
        "title-tracking-3d": presets["title-tracking-3d"].model_copy(update={"font_role": "script"})
    }
    with pytest.raises(TypographyError, match="no 'script' font role"):
        type_steps(card, odd, kit, brief, "1:1")
    heavy = {"title-tracking-3d": presets["title-tracking-3d"].model_copy(update={"weight": 900})}
    with pytest.raises(TypographyError, match="weight 900"):
        type_steps(card, heavy, kit, brief, "1:1")

    def font(safe: bool) -> FontFile:
        licence = LicenseRecord(
            source="rsms/inter",
            licence="OFL-1.1" if safe else "proprietary-trial",
            commercial_safe=safe,
        )
        return FontFile(path="fonts/Inter.ttf", sha256="a" * 64, licence=licence)

    steps = type_steps(card, presets, kit, brief, "1:1", fonts={"Inter": font(True)})
    assert steps[0].args["font"] == {
        "family": "Inter",
        "weight": 600,
        "path": "fonts/Inter.ttf",
        "sha256": "a" * 64,
    }
    with pytest.raises(TypographyError, match="not commercial-safe"):
        type_steps(card, presets, kit, brief, "1:1", fonts={"Inter": font(False)})


# ---- reveal events for sound -----------------------------------------------------------


def test_reveal_markers_match_preset_timing_and_anchor_audio_cues() -> None:
    presets = load_type_presets(PRESETS)
    cards = _cards()
    markers = {m.name: m for m in reveal_markers(cards, presets)}
    assert len(markers) == 2 * len(cards)
    title = markers["type:title:reveal"]
    assert (title.frame, title.duration) == (0, 24)
    assert markers["type:title:hit"].frame == 24
    # glyph_stagger: 7 glyphs ("One view"), 8-frame reveal, 2 frames apart -> 8 + 2 * 6
    line = cards[1]
    assert reveal_span(line, presets[line.preset]) == 20
    assert markers["type:line:hit"].frame == 24 + 20

    edit = Edit(id="film", markers=list(markers.values()))
    tone = SoundSpec(kind="tone", duration_seconds=0.1, params={"freq": 440.0}, provenance=PROV)
    recipe = AudioRecipe(
        id="mix",
        duration_frames=240,
        cues=[
            Cue(
                id="whoosh",
                bus="fx",
                marker="type:title:reveal",
                layers=[Layer(id="w", sound=tone)],
            ),
            Cue(id="hit", bus="fx", marker="type:line:hit", layers=[Layer(id="h", sound=tone)]),
        ],
    )
    placed = place_cues(recipe, edit)
    assert placed == {"whoosh": 0, "hit": recipe.frame_sample(44)}


# ---- shimmer ---------------------------------------------------------------------------


def _png(width: int, height: int, lit: set[tuple[int, int]]) -> bytes:
    rows = b"".join(
        b"\x00" + bytes(255 if (x, y) in lit else 0 for x in range(width)) for y in range(height)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def test_shimmer_is_zero_for_static_type_and_above_threshold_for_a_pixel_jitter() -> None:
    glyph = {(x, y) for x in range(4, 10) for y in range(4, 8)}
    still = _png(16, 12, glyph)
    assert shimmer([still, still, still]) == 0.0
    assert check_no_shimmer([still, still]) == 0.0
    moved = _png(16, 12, {(x + 1, y) for x, y in glyph})
    box = PixelBox(x=2, y=2, w=12, h=8)
    assert shimmer([still, moved, still], box) > SHIMMER_MAX
    with pytest.raises(ShimmerError):
        check_no_shimmer([still, moved], box)
    assert shimmer([still, _png(8, 8, set())]) == 1.0
    presets = load_type_presets(PRESETS)
    card = _cards()[1]
    assert settled_frames(card, presets[card.preset], 60) == range(44, 61)
