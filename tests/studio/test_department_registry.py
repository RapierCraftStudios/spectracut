# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every profile stage resolves to a registered department, or is a known gap."""

from __future__ import annotations

from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.studio.departments import STAGE_DEPARTMENTS, all_departments, department_for
from spectracut.studio.estimate import breakdown, estimate
from spectracut.studio.graph import ProductionGraph
from spectracut.studio.profiles import available_profiles, load_profile

UNIMPLEMENTED = frozenset(
    {
        "reference_analysis",
        "audience_research",
        "previs",
        "assets",
        "animation_polish",
        "fx",
        "grade",
        "immersive_mix",
    }
)
"""Stages with no department yet (separate work); the estimate reports them."""

DEVICE_SHOT = ("device-shot", "device-shot-comped", "device-shot-graded")
"""Profiles with their own runner, not department-driven."""


def _brief() -> Brief:
    return Brief(
        id="spellwise",
        product="Spellwise",
        audience=AudienceProfile(personas=[Persona(name="Dev", description="ships often")]),
        sources=[ClaimSource(id="docs", title="Docs", ref="https://example.test/docs")],
        proof_points=[ProofPoint(claim="Fast spellcheck", source_id="docs")],
        cta="Try it",
    )


def test_every_stage_of_every_profile_maps_to_a_registered_department_or_a_known_gap() -> None:
    registry = all_departments()
    for name in available_profiles():
        if name in DEVICE_SHOT:
            continue
        for stage in load_profile(name).stages:
            dept = registry.get(department_for(stage))
            if stage in UNIMPLEMENTED:
                assert dept is None, (
                    f"{name}/{stage} now has a department; drop it from the gap set"
                )
            else:
                assert dept is not None, f"{name}/{stage} has no registered department"
                assert stage in dept.produces


def test_stage_map_targets_registered_departments_producing_that_kind() -> None:
    registry = all_departments()
    for stage, dept in STAGE_DEPARTMENTS.items():
        assert stage in registry[dept].produces


def test_standard_estimate_lists_the_missing_departments_before_any_spend() -> None:
    profile = load_profile("standard")
    graph = ProductionGraph("p", profile=profile)
    graph.add_nodes(breakdown(_brief(), profile))
    est = estimate(graph, all_departments())
    assert set(est.unknown_departments) == UNIMPLEMENTED & set(profile.stages)
    assert graph.spec("script").inputs == ("brief", "concept")
    assert graph.spec("style_frames").inputs == ("brief", "concept", "art_direction")
    # Finishing (#252) is wired from its departments' declared inputs; standard has
    # no music stage, so music is not an input here.
    assert graph.spec("edit").inputs == ("animatic", "template_shots", "shots", "voiceover")
    assert graph.spec("render").inputs == ("edit", "mix")
    assert graph.spec("delivery").inputs == ("render", "voiceover", "mix")
