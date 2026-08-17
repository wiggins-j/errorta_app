"""Slice 1 §2 — the design_spec ``body_json`` enforcement surface.

The ``design_spec`` governance artifact's machine-readable body is validated here.
``validate_design_body`` is the §8 gate: it returns ``(ok, errors)`` with every
missing/invalid field NAMED, so an invalid draft is bounced to
``changes_requested`` with actionable feedback rather than passing silently.

The ``direction_matrix`` axes exist from Slice 1 (the fields are the anti-sameness
substrate); host-side ENFORCEMENT of "differ on >= 2 axes" is Slice 3 and is NOT
implemented here.

Pure, egress-free (Council inv. 3): no I/O, no imports beyond typing.
"""
from __future__ import annotations

from typing import Any

# The six explicit picks the Designer commits to per axis (spec §2 / §7).
DIRECTION_AXES: tuple[str, ...] = (
    "typography", "color", "density", "shape", "motion", "era_mood")

# The top-level sections the machine-readable body must carry (spec §2).
REQUIRED_SECTIONS: tuple[str, ...] = (
    "direction_matrix", "tokens", "assets", "screens", "components")

# Each screen entry's required keys (spec §2: [{screen, purpose, layout,
# hierarchy, key_states}]).
_SCREEN_KEYS: tuple[str, ...] = (
    "screen", "purpose", "layout", "hierarchy", "key_states")


def validate_design_body(body_json: Any) -> tuple[bool, list[str]]:
    """Validate a design_spec ``body_json``. Returns ``(ok, errors)``.

    ``ok`` is True only when every required section is present and well-formed and
    every direction-matrix axis is committed with a non-empty value. ``errors`` is
    a list of one legible message per problem, each naming the offending field —
    the exact text used to bounce the artifact to ``changes_requested`` (§8).
    """
    errors: list[str] = []
    if not isinstance(body_json, dict):
        return False, ["body_json must be a JSON object with the sections: "
                       + ", ".join(REQUIRED_SECTIONS)]

    for section in REQUIRED_SECTIONS:
        if section not in body_json or not body_json.get(section):
            errors.append(f"missing or empty required section: {section!r}")

    matrix = body_json.get("direction_matrix")
    if isinstance(matrix, dict):
        for axis in DIRECTION_AXES:
            value = matrix.get(axis)
            if not (isinstance(value, str) and value.strip()):
                errors.append(
                    f"direction_matrix is missing a committed pick for axis {axis!r}")
    elif "direction_matrix" in body_json:
        errors.append("direction_matrix must be an object mapping each axis to a pick")

    screens = body_json.get("screens")
    if isinstance(screens, list):
        for i, screen in enumerate(screens):
            if not isinstance(screen, dict):
                errors.append(f"screens[{i}] must be an object")
                continue
            for key in _SCREEN_KEYS:
                if key not in screen or (
                        isinstance(screen.get(key), str) and not screen[key].strip()):
                    errors.append(f"screens[{i}] is missing {key!r}")
    elif "screens" in body_json:
        errors.append("screens must be a list of screen objects")

    return (not errors), errors


def direction_matrix_picks(body_json: Any) -> dict[str, str]:
    """The committed axis->pick mapping from a body (empty on a malformed body).

    Slice 3's cross-project must-differ check consumes this; exposed now so the
    field contract is single-sourced from Slice 1."""
    if not isinstance(body_json, dict):
        return {}
    matrix = body_json.get("direction_matrix")
    if not isinstance(matrix, dict):
        return {}
    return {axis: str(matrix[axis]) for axis in DIRECTION_AXES
            if isinstance(matrix.get(axis), str) and matrix[axis].strip()}


__all__ = ["DIRECTION_AXES", "REQUIRED_SECTIONS", "validate_design_body",
           "direction_matrix_picks"]
