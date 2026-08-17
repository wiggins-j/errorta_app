"""Slice 1 §1/§9 — modality gating: a Designer is seated ONLY for UI modalities.

`cli` / `binary` / `container` projects get no Designer and therefore none of the
design behaviour (spec §1). The untested path is the broken one — this is required.
"""
from __future__ import annotations

from errorta_council.coding import recipes
from errorta_council.coding.topology import coding_role_of

_ROUTES = [{"route_id": "local.qwen", "provider_class": "local"}]


def _roles(members: list[dict]) -> list[str]:
    return [coding_role_of(m) for m in members]


def test_ui_modalities_seat_a_designer() -> None:
    for modality in ("static", "server", "desktop"):
        members = recipes.resolve_team("balanced", _ROUTES, modality=modality)
        assert "designer" in _roles(members), modality
        # exactly one designer
        assert _roles(members).count("designer") == 1, modality


def test_non_ui_modalities_seat_no_designer() -> None:
    for modality in ("cli", "binary", "container"):
        members = recipes.resolve_team("balanced", _ROUTES, modality=modality)
        assert "designer" not in _roles(members), modality


def test_no_modality_is_backwards_compatible_no_designer() -> None:
    members = recipes.resolve_team("balanced", _ROUTES)
    assert "designer" not in _roles(members)


def test_no_designer_when_no_team_assignable() -> None:
    # grounded-or-refuse: empty routes -> empty team, no designer bolted on.
    assert recipes.resolve_team("balanced", [], modality="static") == []
