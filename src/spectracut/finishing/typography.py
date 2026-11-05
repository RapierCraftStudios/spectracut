# SPDX-License-Identifier: AGPL-3.0-or-later
"""Motion typography: type presets, the copy gate, layout, recipe steps and reveal events.

Titles, kinetic type, lower thirds, UI callouts and end cards are :class:`TypeCard`
records rendered with a named :class:`TypePreset` from ``templates/type_presets/``.
A preset is 3D (extruded text, optionally the camera's depth-of-field focus) or 2D
(flat, pixel-aligned overlay text), animated by one of the named animations with
an easing curve.

:func:`type_steps` turns cards into declarative ``type.add_2d`` / ``type.add_3d``
recipe steps for the bpy runtime (``host/bpy_runtime/type3d.py``). It runs the
copy gate on every card *first*, so text that fails it never becomes a recipe
step and never reaches a render. Fonts come from the brand kit
(``BrandKit.typography``); a font file, when supplied, must be commercial-safe.

Layout is done here, not on the host: each card's box for the aspect is checked
against the title-safe area (or a platform's safe area) and snapped to whole
pixels of the aspect's resolution, so 2D type sits on the pixel grid and does
not shimmer from frame to frame (:func:`shimmer` measures it on rendered frames).

:func:`reveal_markers` exports each card's reveal and hit as edit markers, which
audio cues anchor on by name (``spectracut.audio.recipe.Cue.marker``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, JsonValue, ValidationError, model_validator

from spectracut.edit.timeline import Marker
from spectracut.formats.safe_areas import RESOLUTIONS, SafeArea, safe_area
from spectracut.harness.imagecmp import decode_png
from spectracut.spec._base import NonEmptyStr, Slug, StrictModel, Version
from spectracut.spec.asset import LicenseRecord
from spectracut.spec.brand import BrandKit
from spectracut.spec.brief import Brief
from spectracut.spec.recipe import RecipeStep
from spectracut.spec.spec import Aspect

TypeKind = Literal["title", "kinetic", "lower_third", "callout", "end_card"]
Dimension = Literal["2d", "3d"]
Animation = Literal["tracking_reveal", "mask_wipe", "glyph_stagger", "match_cut"]
Easing = Literal["linear", "ease_in", "ease_out", "ease_in_out", "expo_out", "back_out"]

KINDS: tuple[str, ...] = ("title", "kinetic", "lower_third", "callout", "end_card")
ANIMATIONS: tuple[str, ...] = ("tracking_reveal", "mask_wipe", "glyph_stagger", "match_cut")
PRESET_ASPECTS: tuple[Aspect, ...] = ("16:9", "9:16", "1:1")
"""Every preset lays out in these aspects."""

PRESET_SUFFIX = ".yaml"
DEFAULT_PRESETS_DIR = Path(__file__).resolve().parents[3] / "templates" / "type_presets"

TITLE_SAFE = SafeArea(top=0.05, bottom=0.05, left=0.05, right=0.05)
"""Broadcast title-safe (90% of the frame), used when no platform is named."""

AVG_ADVANCE = 0.6
"""Average glyph advance as a fraction of the em size (layout fit estimate)."""
LINE_HEIGHT = 1.2
"""Line height as a fraction of the em size."""

SHIMMER_MAX = 0.5 / 255
"""Most mean sample change allowed between consecutive frames of static 2D type."""

FONT_SUFFIXES = (".ttf", ".otf")

Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
_EPS = 1e-9


class TypographyError(ValueError):
    """A preset, card or layout is invalid."""


class CopyGateError(TypographyError):
    """On-screen text failed the copy gate; nothing was built."""

    def __init__(self, verdicts: list[CopyVerdict]) -> None:
        self.verdicts = verdicts
        lines = [f"{v.card_id}: {'; '.join(v.reasons)}" for v in verdicts]
        super().__init__("copy gate refused: " + " | ".join(lines))


class ShimmerError(TypographyError):
    """2D type moves or flickers between frames where it should be static."""


# ---- presets ---------------------------------------------------------------------------


class TypeBox(StrictModel):
    """A box ``x, y, w, h`` as fractions of the frame (origin top-left)."""

    x: Fraction
    y: Fraction
    w: Annotated[float, Field(gt=0.0, le=1.0)]
    h: Annotated[float, Field(gt=0.0, le=1.0)]

    @model_validator(mode="after")
    def _in_frame(self) -> TypeBox:
        if self.x + self.w > 1.0 + _EPS or self.y + self.h > 1.0 + _EPS:
            raise ValueError("box runs off the frame")
        return self


def _inside(area: SafeArea, box: TypeBox) -> bool:
    return (
        box.x >= area.left - _EPS
        and box.y >= area.top - _EPS
        and box.x + box.w <= 1.0 - area.right + _EPS
        and box.y + box.h <= 1.0 - area.bottom + _EPS
    )


class TypePreset(StrictModel):
    """A named, versioned motion typography preset."""

    id: Slug
    version: Version = 1
    kind: TypeKind
    dimension: Dimension
    animation: Animation
    easing: Easing = "ease_out"
    reveal_frames: int = Field(ge=1, le=600)
    stagger_frames: int = Field(default=0, ge=0, le=60)
    font_role: Slug = "display"
    weight: int = Field(default=400, ge=100, le=900)
    size: float = Field(gt=0.0, le=0.5)
    """Em size as a fraction of the frame's shorter side."""
    color: Slug | None = None
    """Palette swatch of the type; white when unset."""
    extrude: float = Field(default=0.0, ge=0.0, le=1.0)
    bevel: float = Field(default=0.0, ge=0.0, le=0.2)
    distance: float = Field(default=1.0, gt=0.0, le=100.0)
    """Distance in front of the camera (scene units); 3D type only."""
    dof: bool = False
    """3D only: the camera focuses on the type."""
    layout: dict[Aspect, TypeBox]

    @model_validator(mode="after")
    def _valid(self) -> TypePreset:
        missing = [a for a in PRESET_ASPECTS if a not in self.layout]
        if missing:
            raise ValueError(f"layout is missing aspect(s) {missing}")
        for aspect, box in self.layout.items():
            if not _inside(TITLE_SAFE, box):
                raise ValueError(f"layout[{aspect}] is outside the title-safe area")
        if self.dimension == "2d" and (self.extrude or self.bevel or self.dof):
            raise ValueError("2d type cannot extrude, bevel or drive depth of field")
        if self.animation == "glyph_stagger" and self.stagger_frames < 1:
            raise ValueError("glyph_stagger needs stagger_frames >= 1")
        return self


def load_type_preset(path: Path) -> TypePreset:
    """Read one ``<id>.yaml`` preset; errors name the file."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TypographyError(f"cannot read type preset {path}: {exc.strerror or exc}") from None
    except yaml.YAMLError as exc:
        raise TypographyError(f"type preset {path} is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise TypographyError(f"type preset {path} must be a YAML mapping")
    try:
        preset = TypePreset.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "<root>"
        raise TypographyError(
            f"type preset {path} is invalid: {exc.error_count()} error(s), "
            f"first: {loc}: {first['msg']}"
        ) from None
    if path.stem != preset.id:
        raise TypographyError(
            f"type preset {path} declares id {preset.id!r}; the file must be {preset.id}.yaml"
        )
    return preset


def load_type_presets(directory: Path = DEFAULT_PRESETS_DIR) -> dict[str, TypePreset]:
    """Every ``*.yaml`` preset in ``directory`` by id."""
    directory = Path(directory)
    if not directory.is_dir():
        raise TypographyError(f"type preset directory {directory} does not exist")
    presets: dict[str, TypePreset] = {}
    for path in sorted(directory.glob(f"*{PRESET_SUFFIX}")):
        preset = load_type_preset(path)
        presets[preset.id] = preset
    return presets


# ---- cards and the copy gate -----------------------------------------------------------


class TypeCard(StrictModel):
    """One piece of on-screen text: a title, kinetic line, lower third, callout or end card."""

    id: Slug
    preset: Slug
    text: NonEmptyStr
    start_frame: int = Field(ge=0)
    """Edit frame where the reveal starts."""
    source_id: Slug | None = None
    """Declared claim source (brief or brand kit) the text cites."""
    match_from: dict[Aspect, TypeBox] = Field(default_factory=dict)
    """match_cut: where the UI text sits on the frame before it becomes the title."""


class CopyVerdict(StrictModel):
    card_id: Slug
    text: str
    ok: bool
    reasons: list[str] = Field(default_factory=list)


_TRIM = " \t\r\n.!?,:;\"'“”‘’()"


def _norm(text: str) -> str:
    return " ".join(text.split()).strip(_TRIM).casefold()


def copy_gate(card: TypeCard, kit: BrandKit, brief: Brief) -> CopyVerdict:
    """Check one card's text against the brand kit and brief.

    Fails when the text contains a forbidden word (whole word, any case), or when
    it neither cites a declared claim source nor is approved copy: the brief's
    CTA, a proof-point claim, the product name or the brand name.
    """
    reasons: list[str] = []
    for word in kit.forbidden_words:
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", card.text, re.IGNORECASE):
            reasons.append(f"forbidden word {word!r}")
    if card.source_id is not None:
        known = {s.id for s in brief.sources} | {s.id for s in kit.claim_sources}
        if card.source_id not in known:
            reasons.append(f"source {card.source_id!r} is not a declared claim source")
    else:
        approved = {_norm(brief.cta), _norm(brief.product), _norm(kit.name)}
        approved |= {_norm(p.claim) for p in brief.proof_points}
        if _norm(card.text) not in approved:
            reasons.append("text is not approved copy and cites no claim source")
    return CopyVerdict(card_id=card.id, text=card.text, ok=not reasons, reasons=reasons)


def gate_cards(cards: Iterable[TypeCard], kit: BrandKit, brief: Brief) -> list[CopyVerdict]:
    """Copy-gate every card; :class:`CopyGateError` listing every failure."""
    verdicts = [copy_gate(card, kit, brief) for card in cards]
    failed = [v for v in verdicts if not v.ok]
    if failed:
        raise CopyGateError(failed)
    return verdicts


# ---- fonts and layout ------------------------------------------------------------------


class FontFile(StrictModel):
    """A brand font file under the host's assets dir, pinned by hash, with its licence."""

    path: NonEmptyStr
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    licence: LicenseRecord

    @model_validator(mode="after")
    def _font_suffix(self) -> FontFile:
        if not self.path.lower().endswith(FONT_SUFFIXES):
            raise ValueError(f"font {self.path!r} must be one of {FONT_SUFFIXES}")
        return self


class PixelBox(StrictModel):
    """A box in whole pixels of a frame."""

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(gt=0)
    h: int = Field(gt=0)

    def as_list(self) -> list[int]:
        return [self.x, self.y, self.w, self.h]


class TypeLayout(StrictModel):
    """A card laid out on one aspect: whole-pixel box and em size."""

    card_id: Slug
    aspect: Aspect
    resolution: tuple[int, int]
    box: PixelBox
    size_px: int = Field(gt=0)
    match_from: PixelBox | None = None


def _snap(box: TypeBox, width: int, height: int) -> PixelBox:
    x, y = round(box.x * width), round(box.y * height)
    return PixelBox(
        x=x,
        y=y,
        w=max(1, round((box.x + box.w) * width) - x),
        h=max(1, round((box.y + box.h) * height) - y),
    )


def layout_card(
    card: TypeCard, preset: TypePreset, aspect: Aspect, platform: str | None = None
) -> TypeLayout:
    """Lay ``card`` out on ``aspect`` inside the safe area, snapped to whole pixels."""
    box = preset.layout.get(aspect)
    if box is None:
        raise TypographyError(f"preset {preset.id} has no layout for {aspect}")
    area = TITLE_SAFE if platform is None else safe_area(platform, aspect)
    if not _inside(area, box):
        where = platform or "title-safe"
        raise TypographyError(f"card {card.id}: preset {preset.id} box is outside {where} area")
    width, height = RESOLUTIONS[aspect]
    size_px = max(1, round(preset.size * min(width, height)))
    pixels = _snap(box, width, height)
    lines = card.text.split("\n")
    need_w = AVG_ADVANCE * size_px * max(len(line) for line in lines)
    need_h = LINE_HEIGHT * size_px * len(lines)
    if need_w > pixels.w or need_h > pixels.h:
        raise TypographyError(
            f"card {card.id}: text does not fit preset {preset.id} on {aspect} "
            f"(needs about {need_w:.0f}x{need_h:.0f} px, box is {pixels.w}x{pixels.h})"
        )
    match_from = None
    if preset.animation == "match_cut":
        source = card.match_from.get(aspect)
        if source is None:
            raise TypographyError(f"card {card.id}: match_cut needs match_from for {aspect}")
        match_from = _snap(source, width, height)
    return TypeLayout(
        card_id=card.id,
        aspect=aspect,
        resolution=(width, height),
        box=pixels,
        size_px=size_px,
        match_from=match_from,
    )


def _rgb(kit: BrandKit, swatch: str | None) -> list[float]:
    if swatch is None:
        return [1.0, 1.0, 1.0]
    hexcode = kit.palette.swatches.get(swatch)
    if hexcode is None:
        raise TypographyError(f"palette has no swatch {swatch!r}")
    return [round(int(hexcode[i : i + 2], 16) / 255, 6) for i in (1, 3, 5)]


def glyph_count(text: str) -> int:
    return max(1, sum(1 for c in text if not c.isspace()))


def reveal_span(card: TypeCard, preset: TypePreset) -> int:
    """Frames from the start of the reveal to its hit (the last glyph fully in)."""
    if preset.animation == "glyph_stagger":
        return preset.reveal_frames + preset.stagger_frames * (glyph_count(card.text) - 1)
    return preset.reveal_frames


def _presets_for(
    cards: Sequence[TypeCard], presets: Mapping[str, TypePreset]
) -> list[tuple[TypeCard, TypePreset]]:
    out: list[tuple[TypeCard, TypePreset]] = []
    seen: set[str] = set()
    for card in cards:
        if card.id in seen:
            raise TypographyError(f"duplicate card id {card.id!r}")
        seen.add(card.id)
        preset = presets.get(card.preset)
        if preset is None:
            raise TypographyError(f"card {card.id}: unknown preset {card.preset!r}")
        out.append((card, preset))
    return out


def type_steps(
    cards: Sequence[TypeCard],
    presets: Mapping[str, TypePreset],
    kit: BrandKit,
    brief: Brief,
    aspect: Aspect,
    *,
    platform: str | None = None,
    fonts: Mapping[str, FontFile] | None = None,
    start_index: int = 0,
) -> list[RecipeStep]:
    """Recipe steps rendering ``cards`` on ``aspect``.

    The copy gate runs on every card before anything is built: one failing card
    raises :class:`CopyGateError` and no step is returned. Fonts resolve from
    ``kit.typography`` (role -> family, weight must be one of the kit's weights);
    ``fonts`` maps a family to its pinned, commercial-safe font file.
    """
    pairs = _presets_for(cards, presets)
    gate_cards(cards, kit, brief)
    fonts = fonts or {}
    steps: list[RecipeStep] = []
    for card, preset in pairs:
        family = kit.typography.families.get(preset.font_role)
        if family is None:
            raise TypographyError(
                f"card {card.id}: brand kit has no {preset.font_role!r} font role"
            )
        if preset.weight not in kit.typography.weights:
            raise TypographyError(
                f"card {card.id}: weight {preset.weight} is not one of the kit's weights"
            )
        font: dict[str, JsonValue] = {"family": family, "weight": preset.weight}
        file = fonts.get(family)
        if file is not None:
            if not file.licence.commercial_safe:
                raise TypographyError(f"font {family!r} ({file.path}) is not commercial-safe")
            font.update(path=file.path, sha256=file.sha256)
        lay = layout_card(card, preset, aspect, platform)
        args: dict[str, JsonValue] = {
            "name": f"type_{card.id}"[:63],
            "text": card.text,
            "font": font,
            "size_px": lay.size_px,
            "box": list(lay.box.as_list()),
            "resolution": list(lay.resolution),
            "color": list(_rgb(kit, preset.color)),
            "animation": preset.animation,
            "easing": preset.easing,
            "start": card.start_frame,
            "reveal_frames": preset.reveal_frames,
            "stagger_frames": preset.stagger_frames,
        }
        if lay.match_from is not None:
            args["match_from"] = list(lay.match_from.as_list())
        tool = "type.add_2d"
        if preset.dimension == "3d":
            tool = "type.add_3d"
            args.update(
                distance=preset.distance,
                extrude=preset.extrude,
                bevel=preset.bevel,
                dof=preset.dof,
            )
        steps.append(RecipeStep(index=start_index + len(steps), tool=tool, args=args))
    return steps


# ---- events for sound ------------------------------------------------------------------


def reveal_markers(cards: Sequence[TypeCard], presets: Mapping[str, TypePreset]) -> list[Marker]:
    """Edit markers ``type:<card>:reveal`` (start, spanning the reveal) and ``type:<card>:hit``.

    Audio cues anchor on them by name (``Cue.marker``), so a title's whoosh and
    hit land on the frames the type actually reveals and settles.
    """
    markers: list[Marker] = []
    for card, preset in _presets_for(cards, presets):
        span = reveal_span(card, preset)
        note = f"{preset.kind}:{preset.id}"
        markers.append(
            Marker(
                name=f"type:{card.id}:reveal",
                frame=card.start_frame,
                duration=span,
                color="CYAN",
                note=note,
            )
        )
        markers.append(
            Marker(
                name=f"type:{card.id}:hit", frame=card.start_frame + span, color="CYAN", note=note
            )
        )
    return sorted(markers, key=lambda m: (m.frame, m.name))


# ---- 2D stability (shimmer) ------------------------------------------------------------


def shimmer(frames: Sequence[bytes], box: PixelBox | None = None) -> float:
    """Largest mean sample change between consecutive PNG frames, within ``box``.

    0 means the type is perfectly still; sub-pixel placement or resampling shows
    up as edge pixels changing between frames. Frames of different sizes give 1.
    """
    images = [decode_png(f) for f in frames]
    worst = 0.0
    for a, b in zip(images, images[1:], strict=False):
        if (a.width, a.height, a.channels) != (b.width, b.height, b.channels):
            return 1.0
        x0, y0, x1, y1 = 0, 0, a.width, a.height
        if box is not None:
            x0, y0 = min(box.x, a.width), min(box.y, a.height)
            x1, y1 = min(box.x + box.w, a.width), min(box.y + box.h, a.height)
        total, count, ch = 0, 0, a.channels
        for y in range(y0, y1):
            row = y * a.width * ch
            sa = a.pixels[row + x0 * ch : row + x1 * ch]
            sb = b.pixels[row + x0 * ch : row + x1 * ch]
            total += sum(abs(p - q) for p, q in zip(sa, sb, strict=True))
            count += len(sa)
        if count:
            worst = max(worst, total / (255.0 * count))
    return worst


def check_no_shimmer(
    frames: Sequence[bytes], box: PixelBox | None = None, limit: float = SHIMMER_MAX
) -> float:
    """:func:`shimmer` of settled 2D type, raising :class:`ShimmerError` above ``limit``."""
    value = shimmer(frames, box)
    if value > limit:
        raise ShimmerError(f"2D type shimmers between frames: {value:.5f} > {limit:.5f}")
    return value


def settled_frames(card: TypeCard, preset: TypePreset, last_frame: int) -> range:
    """Edit frames after ``card`` has fully revealed, up to ``last_frame`` inclusive."""
    return range(card.start_frame + reveal_span(card, preset), last_frame + 1)
