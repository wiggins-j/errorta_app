"""Slice 1 §2 — the design_spec body_json schema (direction_matrix + friends).

The direction_matrix fields exist from Slice 1 (enforcement of anti-sameness is
Slice 3). ``validate_design_body`` is the §8 gate: an invalid/missing-field body is
bounced to ``changes_requested`` with the offending fields NAMED, never a silent
pass.
"""
from __future__ import annotations

from errorta_council.coding import design_spec


def _valid_body() -> dict:
    return {
        "direction_matrix": {
            "typography": "humanist sans", "color": "warm neutral + accent",
            "density": "comfortable", "shape": "soft-rounded", "motion": "subtle",
            "era_mood": "modern editorial",
        },
        "tokens": {"palette": {"bg": "#faf7f2", "fg": "#1a1a1a"}},
        "assets": {"font_family_ids": ["inter"], "icon_set_id": "lucide"},
        "screens": [{"screen": "home", "purpose": "land", "layout": "hero",
                     "hierarchy": "title>cta", "key_states": ["default"]}],
        "components": [{"name": "button", "usage": "primary action"}],
    }


def test_direction_axes_are_the_six_spec_axes() -> None:
    assert design_spec.DIRECTION_AXES == (
        "typography", "color", "density", "shape", "motion", "era_mood")


def test_valid_body_passes() -> None:
    ok, errors = design_spec.validate_design_body(_valid_body())
    assert ok, errors
    assert errors == []


def test_missing_direction_axis_is_named() -> None:
    body = _valid_body()
    del body["direction_matrix"]["motion"]
    ok, errors = design_spec.validate_design_body(body)
    assert not ok
    assert any("motion" in e for e in errors), errors


def test_missing_top_level_section_is_named() -> None:
    body = _valid_body()
    del body["tokens"]
    ok, errors = design_spec.validate_design_body(body)
    assert not ok
    assert any("tokens" in e for e in errors), errors


def test_empty_body_fails_with_all_sections_named() -> None:
    ok, errors = design_spec.validate_design_body({})
    assert not ok
    for section in ("direction_matrix", "tokens", "assets", "screens", "components"):
        assert any(section in e for e in errors), (section, errors)
