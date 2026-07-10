"""F127 Workstream C — model-tier awareness (pure, table-driven, no network).

Errorta's coding teams are heterogeneous on purpose: a STRONG model on the PM
(context delivery, requirements, review synthesis) and slightly weaker/cheaper
models on the workers. When a worker keeps producing unusable turns, the
escalation ladder reassigns its task UP — to the highest-tier idle member of the
role. This module gives the scheduler the coarse notion of "strength" it needs.

Tiers (high -> low): ``strong > mid > light``. The mapping is deliberately coarse
and substring-based off the ``gateway_route_id`` family, with a safe default of
``mid`` (never assume a worker is light or strong without evidence). Members may
override via ``metadata.model_tier``. Provider churns its model names, so this is
guidance, not a contract — see F127 D4."""
from __future__ import annotations

from typing import Any

STRONG = "strong"
MID = "mid"
LIGHT = "light"

_TIER_RANK = {LIGHT: 0, MID: 1, STRONG: 2}

# Order matters: the FIRST family whose token is a substring of the lowercased
# model id wins. `light`/`strong` markers are checked before the broad `mid`
# fallbacks so e.g. `gpt-5.3-codex-low` -> light and `...-high` -> strong.
_LIGHT_MARKERS = ("haiku", "-mini", "-nano", "-low", "-flash", "lite")
_STRONG_MARKERS = ("opus", "-high", "-xhigh", "-thinking", "-max")
_MID_MARKERS = ("sonnet", "gpt-5", "gpt-4", "gemini", "-pro", "grok", "composer", "codex")


def _model_id(route_id: str) -> str:
    rid = (route_id or "").strip().lower()
    return rid.split(".", 1)[1] if "." in rid else rid


def tier_for_route(route_id: str) -> str:
    """Coarse tier for a gateway route id. Default ``mid`` (never assume)."""
    rid = (route_id or "").strip().lower()
    if not rid:
        return MID
    if rid.startswith(("local.", "fake.")):
        return MID
    model = _model_id(route_id)
    # Strong markers win over the broad mid fallbacks (a "-high" gpt is strong),
    # but an explicit light marker (e.g. a "-low" codex) wins over everything.
    if any(m in model for m in _LIGHT_MARKERS):
        return LIGHT
    if any(m in model for m in _STRONG_MARKERS):
        return STRONG
    if any(m in model for m in _MID_MARKERS):
        return MID
    return MID


def tier_rank(tier: str) -> int:
    """Numeric rank for ordering (higher = stronger). Unknown -> mid's rank."""
    return _TIER_RANK.get(tier, _TIER_RANK[MID])


def member_tier(member: dict[str, Any]) -> str:
    """A member's tier: explicit ``metadata.model_tier`` override, else derived
    from ``gateway_route_id``."""
    md = member.get("metadata") if isinstance(member, dict) else None
    if isinstance(md, dict):
        override = str(md.get("model_tier", "") or "").strip().lower()
        if override in _TIER_RANK:
            return override
    return tier_for_route(str((member or {}).get("gateway_route_id", "") or ""))


def member_rank(member: dict[str, Any]) -> int:
    return tier_rank(member_tier(member))


__all__ = [
    "STRONG", "MID", "LIGHT",
    "tier_for_route", "tier_rank", "member_tier", "member_rank",
]
