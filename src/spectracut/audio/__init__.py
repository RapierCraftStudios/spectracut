# SPDX-License-Identifier: AGPL-3.0-or-later
"""AudioRecipe, session writer, cues, music editing, mix, audio critic and captions.

The public names are resolved lazily from their defining modules, so each name
has exactly one definition site and importing the package stays cheap.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, str] = {
    "AudioRecipe": "recipe",
    "AutomationCurve": "recipe",
    "AutomationPoint": "recipe",
    "Bus": "recipe",
    "Cue": "recipe",
    "Layer": "recipe",
    "Provenance": "recipe",
    "SoundSpec": "recipe",
    "place_cues": "recipe",
    "sound_key": "recipe",
    "ArdourError": "render",
    "ArdourRunner": "render",
    "MixResult": "render",
    "SoundCache": "render",
    "SoundGenerator": "render",
    "ToneGenerator": "render",
    "render_mix": "render",
    "SessionError": "ardour",
    "build_lua": "ardour",
    "reload_export_lua": "ardour",
    "SoundEvent": "events",
    "capture_events": "events",
    "collect_events": "events",
    "edit_events": "events",
    "recipe_events": "events",
    "cue_recipe": "cues",
    "cues_for_events": "cues",
    "CueRequest": "generate",
    "GenerativeSoundGenerator": "generate",
    "Selection": "generate",
    "generate_scene": "generate",
    "BeatGrid": "music_edit",
    "DemucsSeparator": "music_edit",
    "EditPlan": "music_edit",
    "MusicEditError": "music_edit",
    "RubberBandStretcher": "music_edit",
    "analyze": "music_edit",
    "edited_sound": "music_edit",
    "plan_edit": "music_edit",
    "render_edit": "music_edit",
    "render_stems": "music_edit",
    "separate_stems": "music_edit",
    "ProceduralGenerator": "procedural.synth",
    "SuperColliderGenerator": "procedural.supercollider",
}


__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f"spectracut.audio.{module}"), name)


def __dir__() -> list[str]:
    return sorted(_EXPORTS)
