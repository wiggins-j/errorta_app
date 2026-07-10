"""F145 — translate a Wizard/PM *recipe* into concrete autonomy + team config.

A recipe is a coarse intent ("fast_cheap", "highest_quality", "private_offline",
"balanced") the PM infers from the conversation. These pure functions map it to
the real knobs (autonomy overrides) and a grounded team (members assigned only
from routes the live catalog reports available). Shared by the Wizard's
create-on-accept and (later) the control-plane ``assign_models`` action.
"""
from __future__ import annotations

from typing import Any

RECIPES = ("balanced", "fast_cheap", "highest_quality", "private_offline")

_STRONG = ("anthropic", "openai", "google")
_CHEAP = ("local", "claude_cli", "codex_cli", "cursor_cli")


def autonomy_overrides(recipe: str, *, autonomous: bool) -> dict[str, Any]:
    """CodingAutonomyPolicy knob overrides for a recipe. Only the *real* levers
    (never human_code_approval, never gateway budget — see PM_REFERENCE). Note:
    ``block_on_problems`` is a GOVERNANCE field, not autonomy — see
    ``governance_overrides``."""
    # "just build it, don't ask me": no checkpoints. Otherwise pause per milestone.
    return {"checkpoint_cadence": "off" if autonomous else "per_milestone"}


def governance_overrides(recipe: str, *, autonomous: bool) -> dict[str, Any]:
    """GovernanceState overrides for a recipe: F145 uses ``light`` governance and
    pauses on blocking problems unless the user asked for a hands-off run."""
    return {"mode": "light", "block_on_problems": not autonomous}


def _rank(route: dict[str, Any], prefer: tuple[str, ...]) -> int:
    prov = str(route.get("provider_class") or "")
    return prefer.index(prov) if prov in prefer else len(prefer)


def _pick(routes: list[dict[str, Any]], prefer: tuple[str, ...]) -> str | None:
    usable = [r for r in routes if r.get("route_id")]
    if not usable:
        return None
    best = sorted(usable, key=lambda r: (_rank(r, prefer), str(r["route_id"])))[0]
    return str(best["route_id"])


def resolve_team(recipe: str, available_routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A grounded team (2 devs + 1 reviewer + 1 PM) assigned from *available*
    routes by the recipe's tier preference. Returns ``[]`` when nothing is
    available — the caller must warn (grounded-or-refuse: never invent a route)."""
    avail = [r for r in available_routes if r.get("route_id")]
    if recipe == "private_offline":
        avail = [r for r in avail if str(r.get("provider_class")) == "local"]

    cheap_pref = _CHEAP + _STRONG
    strong_pref = _STRONG + _CHEAP
    dev_route = _pick(avail, strong_pref if recipe == "highest_quality" else cheap_pref)
    if dev_route is None:
        return []
    rev_route = _pick(
        avail, strong_pref if recipe in ("highest_quality", "balanced") else cheap_pref,
    ) or dev_route

    def member(mid: str, role: str, route: str) -> dict[str, Any]:
        return {
            "id": mid, "role": "answerer", "enabled": True,
            "metadata": {"coding_role": role},
            "model_mode": "single",  # PM is single-only; keep the team simple + valid
            "gateway_route_id": route,
        }

    return [
        member("pm-1", "pm", rev_route),
        member("dev-1", "dev", dev_route),
        member("dev-2", "dev", dev_route),
        member("reviewer-1", "reviewer", rev_route),
    ]


__all__ = ["RECIPES", "governance_overrides", "autonomy_overrides", "resolve_team"]
